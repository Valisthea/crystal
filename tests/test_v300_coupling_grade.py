"""Coupling grade: what a differentiating guard is *about* governs the score.

Measured on three unrelated public protocols, Build 012 ranked the least
coupled case first and the only real defect second:

    0.833  uncoupled deposit preconditions
    0.770  the real defect
    0.650  a deliberate asymmetry

The confidence was assembled from a base plus a boost per corroborating fact,
with coupling emitted as a note beside it rather than governing it — so three
unrelated guards outscored one guard reading the exact slot the callee clamps.

Coupling is now the dominant term, in three non-overlapping bands. The shapes
below are synthetic and share no vocabulary with any of the three targets: the
discriminant is relational, so it must hold on a protocol nobody has read.
"""

from crystal.detectors.asymmetric_side_effect import (
    EFFECT_COUPLED,
    PARTIAL_COUPLED,
    UNCOUPLED,
    detect,
)
from crystal.parsers import parse_project

# The strong shape: the guard relates state the callee WRITES (`stored`, read
# back through the handle the call mutates) to an argument the callee CONSUMES
# (`amount`) — and the callee already clamps that same pair itself, so the
# guard re-decides what the callee decides.
EFFECT = """
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
}
"""

# Argument-only: the guard names a value it passes on, and consults state the
# callee never writes. A precondition of the caller.
ARGUMENT_ONLY = """
pragma solidity ^0.8.20;
interface IRewards {
    function accrue(address market, address user) external;
}
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
        if (_frozenMarket[market]) {
            revert Frozen(market);
        }
        _rewards.accrue(market, user);
    }
    function closePosition(address market, address user) external {
        _seen[market] += 1;
        _rewards.accrue(market, user);
    }
}
"""

# State-only: the guard reads a flag the callee later sets, but constrains none
# of its arguments — ordinary control flow, not a redundant gate. (The callee
# here takes no arguments at all, so the argument half cannot exist.)
STATE_ONLY = """
pragma solidity ^0.8.20;
contract Pool {
    error Halted();
    struct Switches { bool inflowOpen; }
    Switches private _switches;
    uint256 private _cursor;
    function addFlow(uint256 units) external {
        bool open = _switches.inflowOpen;
        if (!open) {
            revert Halted();
        }
        _cursor += units;
        _rebalance();
    }
    function touchFlow() external {
        _cursor += 0;
        _rebalance();
    }
    function _rebalance() internal {
        _switches.inflowOpen = _cursor > 0;
    }
}
"""

# Neither half: the differentiating guards are modifiers and conditions about
# the caller's own state, touching nothing the callee writes or consumes. Three
# of them must not add up to one coupled guard.
UNCOUPLED_MANY = """
pragma solidity ^0.8.20;
interface IClock {
    function stamp() external;
}
contract Clock is IClock {
    uint256 private _ticks;
    function stamp() external override { _ticks += 1; }
}
contract Booth {
    error Closed();
    error TooEarly();
    error NoSeats();
    IClock private _clock;
    bool private _open;
    uint256 private _opensAt;
    uint256 private _seats;
    mapping(address => uint256) private _visits;
    modifier whileOpen() {
        if (!_open) { revert Closed(); }
        _;
    }
    modifier afterBell() {
        if (block.timestamp < _opensAt) { revert TooEarly(); }
        _;
    }
    modifier hasRoom() {
        if (_seats == 0) { revert NoSeats(); }
        _;
    }
    function enterFront(address who) external whileOpen afterBell hasRoom {
        _visits[who] += 1;
        _clock.stamp();
    }
    function enterSide(address who) external {
        _visits[who] += 1;
        _clock.stamp();
    }
}
"""


def _signals(tmp_path, source, name="P.sol"):
    path = tmp_path / name
    path.write_text(source, encoding="utf-8")
    return detect(parse_project([path]).contracts)


def _grade_of(signal):
    for line in signal.evidence:
        if line.startswith("coupling ["):
            return line.split("[", 1)[1].split("]", 1)[0]
    return None


def _only(signals, function):
    found = [s for s in signals if s.function == function]
    assert found, f"no signal on {function}: {[s.function for s in signals]}"
    return found[0]


# -- the three grades ------------------------------------------------------

def test_a_guard_relating_written_state_to_a_consumed_argument_is_the_signal(tmp_path):
    signal = _only(_signals(tmp_path, EFFECT), "closeByAnyone")
    assert _grade_of(signal) == EFFECT_COUPLED
    assert signal.confidence >= 0.7
    blob = " ".join(signal.evidence)
    assert "re-decides what the callee already decides" in blob


def test_naming_an_argument_it_passes_on_is_not_a_guard_on_the_effect(tmp_path):
    signal = _only(_signals(tmp_path, ARGUMENT_ONLY), "closePosition")
    assert _grade_of(signal) == PARTIAL_COUPLED
    assert signal.confidence < 0.5
    assert "coupled only to the argument" in " ".join(signal.evidence)


def test_reading_a_flag_the_callee_sets_is_ordinary_control_flow(tmp_path):
    signal = _only(_signals(tmp_path, STATE_ONLY), "touchFlow")
    assert _grade_of(signal) == PARTIAL_COUPLED
    assert signal.confidence < 0.5
    assert "ordinary control flow" in " ".join(signal.evidence)


def test_a_guard_touching_neither_half_is_uncoupled(tmp_path):
    signal = _only(_signals(tmp_path, UNCOUPLED_MANY), "enterSide")
    assert _grade_of(signal) == UNCOUPLED
    assert signal.confidence < 0.4
    assert "a precondition of the caller" in " ".join(signal.evidence)


# -- the properties the bands exist to guarantee ---------------------------

def test_guards_are_not_summed(tmp_path):
    """Three uncoupled guards must not outrank one coupled guard.

    This is the exact inversion measured on the public targets: the case with
    the most differentiating conditions and the least coupling ranked first.
    """
    many = _only(_signals(tmp_path, UNCOUPLED_MANY), "enterSide")
    one = _only(_signals(tmp_path, EFFECT), "closeByAnyone")
    assert len([e for e in many.evidence if e.startswith("guard on ")]) >= 3
    assert len([e for e in one.evidence if e.startswith("guard on ")]) == 1
    assert one.confidence > many.confidence + 0.15


def test_the_bands_do_not_overlap(tmp_path):
    coupled = _only(_signals(tmp_path, EFFECT), "closeByAnyone").confidence
    argument = _only(_signals(tmp_path, ARGUMENT_ONLY), "closePosition").confidence
    state = _only(_signals(tmp_path, STATE_ONLY), "touchFlow").confidence
    none = _only(_signals(tmp_path, UNCOUPLED_MANY), "enterSide").confidence
    assert coupled > argument >= none
    assert coupled > state >= none
    assert coupled - max(argument, state) >= 0.15


def test_every_asymmetry_is_still_emitted(tmp_path):
    """Ranking is corrected, recall is not. A researcher wants all of them.

    A detector that reports only what it can prove is worth nothing to someone
    looking for what nobody has proved yet.
    """
    for source, function in (
        (EFFECT, "closeByAnyone"),
        (ARGUMENT_ONLY, "closePosition"),
        (STATE_ONLY, "touchFlow"),
        (UNCOUPLED_MANY, "enterSide"),
    ):
        assert _only(_signals(tmp_path, source), function) is not None


def test_a_weak_grade_says_so_in_the_evidence(tmp_path):
    for source, function in (
        (ARGUMENT_ONLY, "closePosition"),
        (STATE_ONLY, "touchFlow"),
        (UNCOUPLED_MANY, "enterSide"),
    ):
        signal = _only(_signals(tmp_path, source), function)
        assert any("nothing ties the differentiating condition" in line
                   for line in signal.evidence), function


def test_the_discriminant_names_no_protocol_vocabulary():
    """The rule is relational, so it must carry no target's words."""
    import pathlib
    source = pathlib.Path(
        "crystal/detectors/asymmetric_side_effect.py"
    ).read_text(encoding="utf-8")
    code = "\n".join(
        line for line in source.splitlines()
        if not line.lstrip().startswith("#")
    )
    for token in ("borrowGuardian", "penaltyFee", "shortfall", "pegOut",
                  "collateral", "borrowAllowed", "maxDeposit"):
        assert token not in code, token
