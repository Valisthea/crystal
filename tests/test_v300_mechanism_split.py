"""Two mechanisms, two scales — because nothing showed they were comparable.

`asymmetric-side-effect` emitted two different shapes on one scale:

    a) a guard asymmetry — two entry points of one contract reach the same
       callee, one behind a revocable condition the other lacks
    b) a missing companion — most equivalent paths call Y beside X, this one
       does not

Shape (a) is graded by coupling, calibrated against evaluated cases on three
protocols. Shape (b) was scored by `count / total` and nothing else, yet both
were rendered on one ranking, where an unevaluated (b) at 0.833 sat above the
only known true defect at 0.770.

The natural analogue of coupling for (b) — does the absent companion touch the
state the primary operation writes? — is usually unanswerable: measured across
two real protocols, the primary operation is an inherited `_burn`, an interface
method with no body, or an ERC-20 outside the scanned tree. So (b) could not be
calibrated the same way, and the two shapes were separated instead.

What (b) can still do honestly is say what its number means, and report whether
coupling was established, refused, or simply unavailable — never conflating the
last two.
"""

from crystal.detectors import DETECTORS, detector_names
from crystal.detectors.asymmetric_companion import detect as detect_companion
from crystal.detectors.asymmetric_side_effect import detect as detect_guard
from crystal.parsers import parse_project

# One contract carrying BOTH shapes at once: `settle` is reached by two entry
# points with an asymmetric guard, and `payOut` is called beside `record` on
# two of three paths.
BOTH_SHAPES = """
pragma solidity ^0.8.20;
interface ILedger {
    function held(address who) external view returns (uint256);
    function settle(address who, uint256 amount) external;
}
contract Ledger is ILedger {
    mapping(address => uint256) private _stored;
    function held(address who) external view override returns (uint256) {
        return _stored[who];
    }
    function settle(address who, uint256 amount) external override {
        uint256 taken = amount < _stored[who] ? amount : _stored[who];
        _stored[who] -= taken;
    }
}
contract Desk {
    error TooLittle(uint256 held);
    ILedger private _ledger;
    mapping(bytes32 => bool) private _done;
    mapping(address => uint256) private _book;
    function closeByOwner(bytes32 id, address who, uint256 amount) external {
        _done[id] = true;
        uint256 stored = _ledger.held(who);
        if (stored < amount) {
            revert TooLittle(stored);
        }
        _ledger.settle(who, amount);
    }
    function closeByAnyone(bytes32 id, address who, uint256 amount) external {
        _done[id] = true;
        _ledger.settle(who, amount);
    }
    function payA(address who, uint256 amount) external {
        transfer(who, amount);
        record(who, amount);
    }
    function payB(address who, uint256 amount) external {
        transfer(who, amount);
        record(who, amount);
    }
    function payC(address who, uint256 amount) external {
        transfer(who, amount);
    }
    function transfer(address who, uint256 amount) internal {
        _book[who] += amount;
    }
    function record(address who, uint256 amount) internal {
        _book[who] += 0;
    }
}
"""

# The companion's primary operation is inherited from outside the scan, so its
# effect cannot be resolved. That must read as "unresolved", never "uncoupled".
UNRESOLVABLE_PRIMARY = """
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) private _seen;
    function pathA(address who, uint256 amount) external {
        _seen[who] += 1;
        _mint(who, amount);
        _checkpoint(who);
    }
    function pathB(address who, uint256 amount) external {
        _seen[who] += 1;
        _mint(who, amount);
        _checkpoint(who);
    }
    function pathC(address who, uint256 amount) external {
        _seen[who] += 1;
        _mint(who, amount);
    }
    function _checkpoint(address who) internal { _seen[who] += 0; }
}
"""


def _contracts(tmp_path, source):
    path = tmp_path / "P.sol"
    path.write_text(source, encoding="utf-8")
    return parse_project([path]).contracts


def _evidence(signal):
    return " | ".join(signal.evidence)


# -- the split -------------------------------------------------------------

def test_the_two_mechanisms_are_separate_detectors():
    names = detector_names()
    assert "asymmetric-side-effect" in names
    assert "asymmetric-companion" in names
    assert DETECTORS["asymmetric-side-effect"] is not DETECTORS["asymmetric-companion"]


def test_each_detector_emits_only_its_own_shape(tmp_path):
    contracts = _contracts(tmp_path, BOTH_SHAPES)
    guard = detect_guard(contracts)
    companion = detect_companion(contracts)
    assert guard, "the guard shape did not fire on a fixture carrying it"
    assert companion, "the companion shape did not fire on a fixture carrying it"
    assert {s.detector for s in guard} == {"asymmetric-side-effect"}
    assert {s.detector for s in companion} == {"asymmetric-companion"}
    # No signal is produced twice, once under each name.
    overlap = {(s.contract, s.function, s.line) for s in guard} & {
        (s.contract, s.function, s.line) for s in companion
    }
    assert not overlap, overlap


def test_neither_detector_swallows_the_other_shape(tmp_path):
    """Splitting must not lose signals — the totals are what an operator sees."""
    contracts = _contracts(tmp_path, BOTH_SHAPES)
    assert len(detect_guard(contracts)) + len(detect_companion(contracts)) >= 2


# -- the companion shape states what its number means ----------------------

def test_a_companion_signal_declares_its_scale(tmp_path):
    signals = detect_companion(_contracts(tmp_path, BOTH_SHAPES))
    assert signals
    blob = _evidence(signals[0])
    assert "convention ratio, not a defect confidence" in blob
    assert "how consistent the surrounding code is" in blob


def test_an_unresolvable_primary_operation_is_not_called_uncoupled(tmp_path):
    """`unresolved` and `uncoupled` are different claims.

    Saying a companion is uncoupled asserts that it touches nothing the
    operation writes. When the operation is inherited from outside the scan,
    Crystal knows nothing of the kind, and must say so instead.
    """
    signals = detect_companion(_contracts(tmp_path, UNRESOLVABLE_PRIMARY))
    assert signals, "expected a companion signal on the unresolvable fixture"
    blob = _evidence(signals[0])
    assert "coupling [unresolved]" in blob
    assert "coupling [uncoupled]" not in blob
    assert "cannot be established" in blob


def test_a_resolvable_companion_reports_its_coupling(tmp_path):
    signals = detect_companion(_contracts(tmp_path, BOTH_SHAPES))
    assert signals
    assert any("coupling [" in line
               for line in signals[0].evidence), signals[0].evidence


# -- the guard shape names what failed to couple ---------------------------

def test_a_demoted_guard_names_the_operand_that_did_not_couple(tmp_path):
    """A score that drops without a visible reason cannot be contested."""
    source = """
pragma solidity ^0.8.20;
interface IRewards { function accrue(address market, address user) external; }
contract Rewards is IRewards {
    mapping(address => uint256) private _earned;
    function accrue(address market, address user) external override {
        _earned[user] += 1;
    }
}
contract Gate {
    error Frozen(address market);
    IRewards private _rewards;
    mapping(address => bool) private _frozenMarket;
    mapping(address => uint256) private _seen;
    function openPosition(address market, address user) external {
        _seen[market] += 1;
        if (_frozenMarket[market]) { revert Frozen(market); }
        _rewards.accrue(market, user);
    }
    function closePosition(address market, address user) external {
        _seen[market] += 1;
        _rewards.accrue(market, user);
    }
}
"""
    signals = [s for s in detect_guard(_contracts(tmp_path, source))
               if s.function == "closePosition"]
    assert signals
    blob = _evidence(signals[0])
    # The uncoupled subject is named, not merely the coupled argument.
    assert "_frozenMarket" in blob
    assert "neither read nor written by" in blob
    assert "precondition of the calling function" in blob
