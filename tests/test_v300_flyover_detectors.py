"""Build 012 detector regressions, measured against the Flyover target.

Three defects surfaced on `rootstock/lbc/src`:

* `asymmetric-side-effect` was silent on the real sibling asymmetry: two
  entry points of `PegOutContract` reach `slashPegOutCollateral` with the
  same arguments, one behind `if (collateral < quote.penaltyFee) revert`,
  the other behind nothing. The callee was not in a hardcoded name list and
  the guard was an `if (..) revert`, not a `require`.
* `missing-access-control` fired on every `initialize` carrying the
  OpenZeppelin `initializer` modifier.
* the same detector fired repeatedly on the same contract+function, so one
  observation counted five times.

Every fixture below goes through the tree-sitter parser, the same path the
real target takes.
"""

from __future__ import annotations

import dataclasses

from crystal.detectors import collapse_signals, run_detectors
from crystal.detectors import reentrancy
from crystal.detectors.asymmetric_side_effect import _leaf_name
from crystal.detectors.base import DetectorSignal
from crystal.parsers import parse_project
from crystal.symbolic import SymbolicEngine
import pytest
from crystal.parsers import solidity_ts, treesitter_enabled

needs_solidity_treesitter = pytest.mark.skipif(
    not treesitter_enabled() or not solidity_ts.available(),
    reason=(
        "needs the tree-sitter Solidity front-end: the regex fallback does not "
        "resolve imports, `using for` bindings or user-defined value types, so "
        "a receiver's type cannot be identified. See "
        "crystal.parsers.SOLIDITY_REGEX_LIMITATIONS."
    ),
)



def _contracts(tmp_path, text, name="Target.sol"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return parse_project([path]).contracts


def _signals(tmp_path, text, only):
    contracts = _contracts(tmp_path, text)
    return run_detectors(contracts, SymbolicEngine(contracts), only)


# ---------------------------------------------------------------------------
# The real shape: a reduced but faithful copy of PegOutContract's two refund
# paths, with the collateral contract they compose with.
# ---------------------------------------------------------------------------

FLYOVER_PEGOUT = """
pragma solidity ^0.8.20;

library Quotes {
    struct PegOutQuote {
        address lbcAddress;
        address lpRskAddress;
        address rskRefundAddress;
        uint256 value;
        uint256 callFee;
        uint256 gasFee;
        uint256 penaltyFee;
        uint256 expireDate;
        uint256 expireBlock;
    }
}

interface ICollateralManagement {
    function getPegOutCollateral(address lp) external view returns (uint256);
    function slashPegOutCollateral(
        address punisher, Quotes.PegOutQuote calldata quote, bytes32 quoteHash
    ) external;
}

contract CollateralManagementContract is ICollateralManagement {
    mapping(address => uint256) private _pegOutCollateral;
    mapping(address => uint256) private _rewards;

    function getPegOutCollateral(address lp) external view override returns (uint256) {
        return _pegOutCollateral[lp];
    }

    function slashPegOutCollateral(
        address punisher, Quotes.PegOutQuote calldata quote, bytes32 quoteHash
    ) external override {
        uint256 available = _pegOutCollateral[quote.lpRskAddress];
        uint256 penalty = quote.penaltyFee < available ? quote.penaltyFee : available;
        _pegOutCollateral[quote.lpRskAddress] -= penalty;
        _rewards[punisher] += penalty;
    }
}

contract PegOutContract {
    error InsufficientCollateral(uint256 collateral);
    error QuoteNotFound(bytes32 quoteHash);
    error QuoteNotExpired(bytes32 quoteHash);

    ICollateralManagement private _collateralManagement;
    mapping(bytes32 => Quotes.PegOutQuote) private _pegOutQuotes;
    mapping(bytes32 => bool) private _completed;
    mapping(address => uint256) private _balances;

    function refundPegOut(
        bytes32 quoteHash, bytes calldata btcTx, bytes32 btcBlockHeaderHash
    ) external nonReentrant {
        Quotes.PegOutQuote memory quote = _validatePegOutTransaction(quoteHash, btcTx);

        delete _pegOutQuotes[quoteHash];
        _completed[quoteHash] = true;

        if (_shouldPenalize(quote, quoteHash, btcBlockHeaderHash)) {
            uint256 collateral = _collateralManagement.getPegOutCollateral(quote.lpRskAddress);
            if (collateral < quote.penaltyFee) {
                revert InsufficientCollateral(collateral);
            }
            _collateralManagement.slashPegOutCollateral(msg.sender, quote, quoteHash);
        }

        uint256 refundAmount = quote.value + quote.callFee + quote.gasFee;
        (bool sent,) = quote.lpRskAddress.call{value: refundAmount}("");
        if (!sent) {
            _increaseBalance(quote.lpRskAddress, refundAmount);
        }
    }

    function refundUserPegOut(bytes32 quoteHash) external nonReentrant {
        Quotes.PegOutQuote memory quote = _pegOutQuotes[quoteHash];

        if (quote.lbcAddress == address(0)) revert QuoteNotFound(quoteHash);
        if (quote.expireDate >= block.timestamp || quote.expireBlock >= block.number) revert QuoteNotExpired(quoteHash);

        uint256 valueToTransfer = quote.value + quote.callFee + quote.gasFee;
        address addressToTransfer = quote.rskRefundAddress;

        delete _pegOutQuotes[quoteHash];
        _completed[quoteHash] = true;

        _collateralManagement.slashPegOutCollateral(msg.sender, quote, quoteHash);

        (bool sent,) = addressToTransfer.call{value: valueToTransfer}("");
        if (!sent) {
            _increaseBalance(addressToTransfer, valueToTransfer);
        }
    }

    function _validatePegOutTransaction(
        bytes32 quoteHash, bytes calldata btcTx
    ) private view returns (Quotes.PegOutQuote memory quote) {
        if (_completed[quoteHash]) revert QuoteNotFound(quoteHash);
        quote = _pegOutQuotes[quoteHash];
        if (quote.lbcAddress == address(0)) revert QuoteNotFound(quoteHash);
        if (quote.lpRskAddress != msg.sender) revert QuoteNotFound(quoteHash);
    }

    function _shouldPenalize(
        Quotes.PegOutQuote memory quote, bytes32 quoteHash, bytes32 blockHash
    ) private view returns (bool) {
        return quote.expireDate < block.timestamp;
    }

    function _increaseBalance(address dest, uint256 amount) private {
        _balances[dest] += amount;
    }
}
"""


def test_the_flyover_sibling_asymmetry_is_reported(tmp_path):
    """Both refund paths, the shared callee, and the asymmetric guard are named."""
    signals = _signals(tmp_path, FLYOVER_PEGOUT, ["asymmetric-side-effect"])
    assert len(signals) == 1, [s.title for s in signals]
    found = signals[0]
    assert found.contract == "PegOutContract"
    assert found.function == "refundUserPegOut"
    assert "refundPegOut" in found.title and "refundUserPegOut" in found.title
    assert "slashPegOutCollateral" in found.title
    assert "collateral < quote.penaltyFee" in found.reason
    evidence = " | ".join(found.evidence)
    assert "collateral < quote.penaltyFee" in evidence
    assert "authorization guard present in 1/2 sites" in evidence
    # The coupling is explained, not asserted: the guard reads the receiver
    # the guarded call mutates.
    assert "_collateralManagement" in evidence and "coupling" in evidence
    assert found.status == "RESEARCH" and found.falsification
    assert found.confidence >= 0.6


def test_the_guarded_flyover_path_is_not_reported(tmp_path):
    signals = _signals(tmp_path, FLYOVER_PEGOUT, ["asymmetric-side-effect"])
    assert not [s for s in signals if s.function == "refundPegOut"]


def test_the_guarded_path_names_its_helper_validation(tmp_path):
    """Guards inside `_validatePegOutTransaction` count for the path that calls it.

    `quote.lbcAddress == address(0)` is checked inline by the user path and
    inside the helper by the LP path; the detector must see both, so that
    check is not listed as something the LP path lacks.
    """
    signals = _signals(tmp_path, FLYOVER_PEGOUT, ["asymmetric-side-effect"])
    evidence = " | ".join(signals[0].evidence)
    lacking = [line for line in signals[0].evidence
               if "that refundPegOut does not place" in line]
    assert lacking, evidence
    assert "quote.lbcAddress == address(0)" not in lacking[0]


# ---------------------------------------------------------------------------
# A second domain with none of the Flyover vocabulary: the shape is what
# fires, not the names.
# ---------------------------------------------------------------------------

MEMBERSHIP = """
pragma solidity ^0.8.20;

library Plans {
    struct Plan {
        address member;
        uint256 dues;
        uint256 renewsAt;
    }
}

interface IDuesLedger {
    function creditOf(address member) external view returns (uint256);
    function chargeDues(address operator, Plans.Plan calldata plan, bytes32 planId) external;
}

contract DuesLedger is IDuesLedger {
    mapping(address => uint256) private _credit;
    mapping(address => uint256) private _operatorFees;

    function creditOf(address member) external view override returns (uint256) {
        return _credit[member];
    }

    function chargeDues(address operator, Plans.Plan calldata plan, bytes32 planId) external override {
        uint256 held = _credit[plan.member];
        uint256 charged = plan.dues < held ? plan.dues : held;
        _credit[plan.member] -= charged;
        _operatorFees[operator] += charged;
    }
}

contract Membership {
    error NotEnoughCredit(uint256 credit);
    error UnknownPlan(bytes32 planId);

    IDuesLedger private _ledger;
    mapping(bytes32 => Plans.Plan) private _plans;
    mapping(bytes32 => bool) private _renewed;

    function renewByOperator(bytes32 planId, bytes calldata attestation) external {
        Plans.Plan memory plan = _loadPlan(planId);
        _renewed[planId] = true;
        if (_isLate(plan)) {
            uint256 credit = _ledger.creditOf(plan.member);
            if (credit < plan.dues) {
                revert NotEnoughCredit(credit);
            }
            _ledger.chargeDues(msg.sender, plan, planId);
        }
    }

    function renewByMember(bytes32 planId) external {
        Plans.Plan memory plan = _plans[planId];
        if (plan.member == address(0)) revert UnknownPlan(planId);
        _renewed[planId] = true;
        _ledger.chargeDues(msg.sender, plan, planId);
    }

    function _loadPlan(bytes32 planId) private view returns (Plans.Plan memory plan) {
        plan = _plans[planId];
        if (plan.member == address(0)) revert UnknownPlan(planId);
    }

    function _isLate(Plans.Plan memory plan) private view returns (bool) {
        return plan.renewsAt < block.timestamp;
    }
}
"""


def test_the_shape_fires_on_an_unrelated_domain(tmp_path):
    lowered = MEMBERSHIP.lower()
    for word in ("refund", "slash", "penalty", "collateral", "pegout", "quote"):
        assert word not in lowered, f"fixture must not share Flyover vocabulary: {word}"
    signals = _signals(tmp_path, MEMBERSHIP, ["asymmetric-side-effect"])
    assert len(signals) == 1, [s.title for s in signals]
    found = signals[0]
    assert (found.contract, found.function) == ("Membership", "renewByMember")
    assert "chargeDues" in found.title and "renewByOperator" in found.title
    assert "credit < plan.dues" in found.reason


def test_symmetric_guards_spelled_differently_stay_quiet(tmp_path):
    """`require(!x)` and `if (x) revert` gate the same thing."""
    source = """
    pragma solidity ^0.8.20;
    contract Escrow {
        error Frozen(address who);
        mapping(address => bool) public frozen;
        mapping(address => uint256) public held;
        function releaseByAgent(address who, uint256 amount) external {
            require(!frozen[who], "frozen");
            _move(who, amount);
        }
        function releaseByOwner(address who, uint256 amount) external {
            if (frozen[who]) revert Frozen(who);
            _move(who, amount);
        }
        function _move(address who, uint256 amount) private {
            held[who] -= amount;
        }
    }
    """
    assert _signals(tmp_path, source, ["asymmetric-side-effect"]) == []


def test_different_arguments_are_different_preconditions(tmp_path):
    """A guard about the other flow's subject is not a forgotten guard.

    On the real target this shape produced six false signals: `deposit`
    credits `msg.sender`, `fulfil` credits `provider` after checking the
    provider's balance covers the order. Same callee, different operands,
    different precondition.
    """
    source = """
    pragma solidity ^0.8.20;
    contract Pool {
        error Underfunded(uint256 have, uint256 need);
        mapping(address => uint256) private _balances;
        function deposit() external payable {
            _increase(msg.sender, msg.value);
        }
        function fulfil(address provider, uint256 need) external payable {
            uint256 have = _balances[provider] + msg.value;
            if (have < need) revert Underfunded(have, need);
            _increase(provider, msg.value);
        }
        function _increase(address who, uint256 amount) private {
            _balances[who] += amount;
        }
    }
    """
    assert _signals(tmp_path, source, ["asymmetric-side-effect"]) == []


COUNCIL = """
pragma solidity ^0.8.20;
contract Council {
    uint256 public seats;
    uint256 public quorum;
    mapping(address => bool) public isMember;
    function admit(address who, uint256 _quorum) external {
        require(!isMember[who], "member");
        isMember[who] = true;
        seats++;
        if (quorum != _quorum) setQuorum(_quorum);
    }
    function expel(address who, uint256 _quorum) external {
        require(seats - 1 >= _quorum, "quorum");
        isMember[who] = false;
        seats--;
        if (quorum != _quorum) setQuorum(_quorum);
    }
    function setQuorum(uint256 _quorum) public {
        require(_quorum <= seats, "too high");
        require(_quorum >= 1, "too low");
        quorum = _quorum;
    }
}
"""


@needs_solidity_treesitter
def test_a_guard_the_callee_re_establishes_is_not_an_asymmetry(tmp_path):
    """`expel` pre-checks what `setQuorum` checks itself; `admit` relies on the callee."""
    assert _signals(tmp_path, COUNCIL, ["asymmetric-side-effect"]) == []


def test_the_same_shape_without_the_callee_check_is_reported(tmp_path):
    bare_callee = COUNCIL.replace(
        '        require(_quorum <= seats, "too high");\n', ""
    )
    assert bare_callee != COUNCIL
    signals = _signals(tmp_path, bare_callee, ["asymmetric-side-effect"])
    assert [(s.contract, s.function) for s in signals] == [("Council", "admit")]
    assert "seats - 1 >= _quorum" in signals[0].reason


def test_leaf_names_are_language_neutral():
    assert _leaf_name("pallet_balances::Pallet::<T>::transfer") == "transfer"
    assert _leaf_name("_collateralManagement.slashPegOutCollateral") == "slashPegOutCollateral"
    assert _leaf_name("token.transfer(to, amount)") == "transfer"


# ---------------------------------------------------------------------------
# Initializer-guarded functions are deployment, not missing access control.
# ---------------------------------------------------------------------------

INITIALIZED = """
pragma solidity ^0.8.20;
contract Config {
    address public owner;
    uint256 public feeRate;
    function initialize(address owner_, uint256 feeRate_) external initializer {
        owner = owner_;
        feeRate = feeRate_;
    }
    function upgradeOwner(address owner_) external reinitializer(2) {
        owner = owner_;
    }
    function setOwner(address owner_) external {
        owner = owner_;
    }
}
"""


def test_initializer_guarded_writes_are_not_missing_access_control(tmp_path):
    signals = _signals(tmp_path, INITIALIZED, ["access-control"])
    flagged = sorted(s.function for s in signals)
    assert flagged == ["setOwner"], flagged


def test_the_flyover_initializers_are_quiet_but_bare_setters_are_not(tmp_path):
    """The same contract without the modifier is reported: the modifier is the reason."""
    unguarded = INITIALIZED.replace(" external initializer {", " external {")
    assert unguarded != INITIALIZED
    signals = _signals(tmp_path, unguarded, ["access-control"])
    assert "initialize" in {s.function for s in signals}


# ---------------------------------------------------------------------------
# One (detector, contract, function) yields one signal.
# ---------------------------------------------------------------------------

def _signal(detector, contract, function, line, confidence, evidence):
    return DetectorSignal(
        id=f"{detector}-{contract}-{function}-{line}", detector=detector,
        title=f"{detector} in {contract}.{function}", contract=contract,
        function=function, path="T.sol", line=line, confidence=confidence,
        reason="r", evidence=tuple(evidence),
    )


def test_collapse_keeps_the_strongest_instance_and_merges_the_rest():
    signals = [
        _signal("reentrancy-ordering", "LBC", "registerPegIn", 589, 0.45, ["a"]),
        _signal("reentrancy-ordering", "LBC", "registerPegIn", 620, 0.59, ["b"]),
        _signal("reentrancy-ordering", "LBC", "registerPegIn", 638, 0.45, ["c", "a"]),
        _signal("reentrancy-ordering", "LBC", "refundPegOut", 700, 0.59, ["d"]),
        _signal("missing-access-control", "LBC", "registerPegIn", 589, 0.62, ["e"]),
    ]
    collapsed = collapse_signals(signals)
    by_key = {(s.detector, s.contract, s.function): s for s in collapsed}
    assert len(collapsed) == 3 == len(by_key)

    merged = by_key[("reentrancy-ordering", "LBC", "registerPegIn")]
    assert merged.confidence == 0.59 and merged.line == 620
    assert merged.id == "reentrancy-ordering-LBC-registerPegIn-620"
    evidence = list(merged.evidence)
    assert evidence[0] == "b"
    assert "3 instances collapsed; lines 589, 620, 638" in evidence
    # Evidence from the folded instances survives, once each.
    assert evidence.count("  a") == 1 and "  c" in evidence
    assert any(line.startswith("also at line 589") for line in evidence)
    # Other functions and other detectors are untouched.
    assert by_key[("reentrancy-ordering", "LBC", "refundPegOut")].evidence == ("d",)
    assert by_key[("missing-access-control", "LBC", "registerPegIn")].evidence == ("e",)


def test_collapse_is_deterministic_on_ties():
    signals = [
        _signal("reentrancy-ordering", "C", "f", 30, 0.5, ["x"]),
        _signal("reentrancy-ordering", "C", "f", 10, 0.5, ["y"]),
    ]
    first = collapse_signals(signals)[0]
    second = collapse_signals(list(reversed(signals)))[0]
    assert first.id == second.id and first.line == 10


TWICE = """
pragma solidity ^0.8.20;
contract Twice {
    mapping(address => uint256) public balances;
    function pull(address first, address second) external {
        uint256 amount = balances[msg.sender];
        (bool one,) = first.call{value: amount}("");
        balances[msg.sender] = 0;
        (bool two,) = second.call{value: amount}("");
        balances[msg.sender] = 1;
    }
}
"""


def test_the_registry_collapses_repeated_statement_signals(tmp_path):
    contracts = _contracts(tmp_path, TWICE)
    raw = reentrancy.detect(contracts)
    assert len([s for s in raw if s.function == "pull"]) == 2, "fixture must fire twice"
    signals = run_detectors(contracts, SymbolicEngine(contracts), ["reentrancy"])
    assert [(s.contract, s.function) for s in signals] == [("Twice", "pull")]
    assert signals[0].confidence == max(s.confidence for s in raw)
    assert any("2 instances collapsed" in line for line in signals[0].evidence)


def test_collapsed_signals_keep_unique_stable_ids(tmp_path):
    contracts = _contracts(tmp_path, TWICE)
    first = run_detectors(contracts, SymbolicEngine(contracts))
    second = run_detectors(contracts, SymbolicEngine(contracts))
    assert [s.id for s in first] == [s.id for s in second]
    assert len({s.id for s in first}) == len(first)
    assert all(dataclasses.is_dataclass(s) for s in first)
