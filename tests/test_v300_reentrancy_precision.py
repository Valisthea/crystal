"""The reentrancy detector resolves a call's receiver before it counts the
call as a control transfer.

Measured on Flyover, `reentrancy-ordering` fired on `Math.min(..)`, on the
struct literal `Flyover.LiquidityProvider({..})` and on the library routine
`Quotes.checkAgreedAmount(..)`: none of them can put foreign code on the
stack. These fixtures pin the resolution. Library calls, struct literals,
internal calls, user-defined value types and container builtins are not
control transfers. Calls on address- or contract-typed values, low-level
calls, value transfers and libraries that receive a contract are. A receiver
that cannot be resolved keeps its signal at reduced confidence, and the
evidence says so.
"""

from __future__ import annotations

import pytest

from crystal.detectors import reentrancy, run_detectors
from crystal.ir import EXTERNAL_CALL_KINDS
from crystal.parsers import parse_project, solidity_ts, treesitter_enabled
from crystal.symbolic import SymbolicEngine


needs_solidity_treesitter = pytest.mark.skipif(
    not treesitter_enabled() or not solidity_ts.available(),
    reason=(
        "needs the tree-sitter Solidity front-end: the regex fallback does not "
        "resolve imports, `using for` bindings or user-defined value types, so "
        "a receiver's type cannot be identified. See "
        "crystal.parsers.SOLIDITY_REGEX_LIMITATIONS."
    ),
)

pytestmark = pytest.mark.skipif(
    not solidity_ts.available(), reason="tree-sitter Solidity front-end unavailable",
)


def _contracts(tmp_path, text, name="Target.sol"):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return parse_project([path]).contracts


def _signals(tmp_path, text):
    contracts = _contracts(tmp_path, text)
    return run_detectors(contracts, SymbolicEngine(contracts), ["reentrancy"])


def _by_function(signals):
    return {item.function: item for item in signals}


def _resolve(contracts, contract_name, function_name):
    """Every external-kind call of a function, resolved."""
    resolver = reentrancy.CallResolver(contracts)
    contract = next(c for c in contracts if c.name == contract_name)
    function = next(f for f in contract.functions if f.name == function_name)
    calls = [c for c in function.ir.calls() if c.kind in EXTERNAL_CALL_KINDS]
    return [resolver.transfer(call, function, contract) for call in calls]


# ---------------------------------------------------------------------------
# Shapes that are not control transfers
# ---------------------------------------------------------------------------

# The Flyover shape: a library routine between a read and a write.
PROJECT_LIBRARY = """
pragma solidity ^0.8.20;
library Math {
    function min(uint256 a, uint256 b) internal pure returns (uint256) { return a < b ? a : b; }
}
library Quotes {
    struct Quote { address lp; uint256 penaltyFee; }
    function checkAgreedAmount(Quote calldata quote, uint256 amount) internal pure {
        require(amount >= quote.penaltyFee);
    }
}
contract Collateral {
    mapping(address => uint256) private _collateral;
    uint256 private _penalties;
    function slash(Quotes.Quote calldata quote) external {
        uint256 penalty = Math.min(quote.penaltyFee, _collateral[quote.lp]);
        _collateral[quote.lp] -= penalty;
        _penalties += penalty;
    }
    function register(Quotes.Quote calldata quote, uint256 amount) external {
        Quotes.checkAgreedAmount(quote, amount);
        _penalties += amount;
    }
}
"""

IMPORTED_LIBRARY = """
pragma solidity ^0.8.20;
import {Math} from "@openzeppelin/contracts/utils/math/Math.sol";
contract Collateral {
    mapping(address => uint256) private _collateral;
    uint256 private _penalties;
    function slash(address lp, uint256 fee) external {
        uint256 penalty = Math.min(fee, _collateral[lp]);
        _collateral[lp] -= penalty;
        _penalties += penalty;
    }
}
"""

STRUCT_LITERAL = """
pragma solidity ^0.8.20;
library Flyover {
    struct LiquidityProvider { uint256 id; address providerAddress; string name; }
}
contract Discovery {
    uint256 public lastProviderId;
    mapping(uint256 => Flyover.LiquidityProvider) private _providers;
    function register(string calldata name) external payable returns (uint256) {
        ++lastProviderId;
        uint256 providerId = lastProviderId;
        _providers[providerId] = Flyover.LiquidityProvider({
            id: providerId, providerAddress: msg.sender, name: name
        });
        return providerId;
    }
}
"""

INTERNAL_SHAPES = """
pragma solidity ^0.8.20;
type UD60x18 is uint256;
library Math {
    function min(uint256 a, uint256 b) internal pure returns (uint256) { return a < b ? a : b; }
}
abstract contract Base {
    uint256 internal counter;
    function bump() internal { counter += 1; }
    function tally() public virtual returns (uint256) { return counter; }
}
contract Derived is Base {
    using Math for uint256;
    UD60x18 public rate;
    uint256 public total;
    address[] public members;
    function viaBase(uint256 amount) external { Base.bump(); total += amount; }
    function viaThis(uint256 amount) external { this.tally(); total += amount; }
    function viaWrap(uint256 x) external { rate = UD60x18.wrap(x); total += 1; }
    function viaUsingFor(uint256 x) external { uint256 capped = x.min(total); total -= capped; }
    function viaPush(address who) external { members.push(who); total += 1; }
    function tally() public override returns (uint256) { return counter; }
}
"""


def test_project_library_call_is_not_a_control_transfer(tmp_path):
    contracts = _contracts(tmp_path, PROJECT_LIBRARY)
    assert _signals(tmp_path, PROJECT_LIBRARY) == []

    (verdict,) = _resolve(contracts, "Collateral", "slash")
    assert verdict.verdict == reentrancy.NO_TRANSFER
    assert "Math.min" in verdict.detail
    assert "body makes no external call" in verdict.detail

    (verdict,) = _resolve(contracts, "Collateral", "register")
    assert verdict.verdict == reentrancy.NO_TRANSFER
    assert "Quotes.checkAgreedAmount" in verdict.detail


@needs_solidity_treesitter
def test_imported_library_with_value_arguments_is_not_a_control_transfer(tmp_path):
    contracts = _contracts(tmp_path, IMPORTED_LIBRARY)
    assert _signals(tmp_path, IMPORTED_LIBRARY) == []
    (verdict,) = _resolve(contracts, "Collateral", "slash")
    assert verdict.verdict == reentrancy.NO_TRANSFER
    assert "outside this project" in verdict.detail
    assert "every argument is value-typed" in verdict.detail


def test_struct_literal_is_not_a_control_transfer(tmp_path):
    """Two independent layers now agree, and either one is enough.

    The parser stopped recording a struct literal as a call at all, so the
    resolver is usually never asked. When it is asked — a front-end that still
    records one, or a type the catalog could not see — it must still answer
    NO_TRANSFER. Assert the outcome, and the verdict only when there is one.
    """
    contracts = _contracts(tmp_path, STRUCT_LITERAL)
    assert _signals(tmp_path, STRUCT_LITERAL) == []
    for verdict in _resolve(contracts, "Discovery", "register"):
        assert verdict.verdict == reentrancy.NO_TRANSFER
        assert "struct constructor" in verdict.detail


@needs_solidity_treesitter
def test_internal_udvt_using_for_and_container_calls_are_not_control_transfers(tmp_path):
    contracts = _contracts(tmp_path, INTERNAL_SHAPES)
    assert _signals(tmp_path, INTERNAL_SHAPES) == []
    # Same convergence: a shape the parser now drops outright leaves nothing to
    # resolve. Each one that survives to the resolver must still be explained.
    expected = {
        "viaBase": "inherited",
        "viaThis": "self-call",
        "viaWrap": "user-defined value type",
        "viaUsingFor": "Math.min",
        "viaPush": "builtin",
    }
    explained = 0
    for name, token in expected.items():
        for verdict in _resolve(contracts, "Derived", name):
            assert verdict.verdict == reentrancy.NO_TRANSFER, name
            assert token in verdict.detail, (name, verdict.detail)
            explained += 1
    assert explained, "no shape reached the resolver at all"


# ---------------------------------------------------------------------------
# Shapes that are control transfers, and must keep firing
# ---------------------------------------------------------------------------

CONTROL = """
pragma solidity ^0.8.20;
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
interface IVault {
    function withdrawFor(address who, uint256 amount) external;
    function balance(address who) external view returns (uint256);
}
library Puller {
    function pull(IERC20 token, address from, uint256 amount) internal {
        token.transferFrom(from, address(this), amount);
    }
}
contract Ledger {
    mapping(address => uint256) public balances;
    uint256 public total;
    IVault public vault;
    IERC20 public token;
    mapping(address => IERC20) public tokens;

    function lowLevel(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
    }
    function viaInterface(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        vault.withdrawFor(msg.sender, amount);
        balances[msg.sender] -= amount;
    }
    function viaOpaqueLibrary(address to, uint256 amount) external {
        SafeERC20.safeTransfer(token, to, amount);
        balances[to] += amount;
    }
    function viaProjectLibrary(uint256 amount) external {
        Puller.pull(token, msg.sender, amount);
        balances[msg.sender] += amount;
    }
    function viaCast(address t) external {
        uint256 got = IERC20(t).balanceOf(address(this));
        total = got;
    }
    function viaLocal(address v) external {
        IVault local = IVault(v);
        local.withdrawFor(msg.sender, 1);
        total += 1;
    }
    function viaMapping(address who) external {
        tokens[who].transferFrom(who, address(this), 1);
        total += 1;
    }
    function viaView() external {
        uint256 b = vault.balance(msg.sender);
        total = b;
    }
    function safe(uint256 amount) external {
        balances[msg.sender] -= amount;
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
    }
}
"""

TRANSFERRING = {
    "lowLevel", "viaInterface", "viaOpaqueLibrary", "viaProjectLibrary",
    "viaCast", "viaLocal", "viaMapping", "viaView",
}


@needs_solidity_treesitter
def test_true_control_transfers_still_fire(tmp_path):
    signals = _by_function(_signals(tmp_path, CONTROL))
    assert set(signals) == TRANSFERRING
    assert "safe" not in signals
    assert signals["lowLevel"].confidence > 0.8
    assert signals["lowLevel"].ordered_trace[0].startswith("L")
    for item in signals.values():
        assert item.status == "RESEARCH"
        assert item.validation_required
        assert any("receiver" in question for question in item.falsification)


def test_interface_typed_receiver_names_its_declaration(tmp_path):
    signals = _by_function(_signals(tmp_path, CONTROL))
    item = signals["viaInterface"]
    assert item.confidence >= 0.76
    assert any("declared `IVault`" in line and "interface in this project" in line
               for line in item.evidence)


@needs_solidity_treesitter
def test_imported_library_receiving_a_contract_is_a_control_transfer(tmp_path):
    signals = _by_function(_signals(tmp_path, CONTROL))
    item = signals["viaOpaqueLibrary"]
    assert any("SafeERC20.safeTransfer" in line and "outside this project" in line
               and "receives `token`" in line for line in item.evidence)
    assert any("confidence lowered by" in line and "library" in line for line in item.evidence)
    assert 0.3 < item.confidence < signals["viaInterface"].confidence


def test_project_library_that_calls_out_is_followed_into_its_body(tmp_path):
    signals = _by_function(_signals(tmp_path, CONTROL))
    item = signals["viaProjectLibrary"]
    assert any("`Puller.pull` reaches `token.transferFrom" in line for line in item.evidence)
    assert item.confidence >= 0.62


@needs_solidity_treesitter
def test_cast_local_and_mapping_receivers_resolve_to_contract_types(tmp_path):
    signals = _by_function(_signals(tmp_path, CONTROL))
    assert any("casts an address to `IERC20`" in line
               for line in signals["viaCast"].evidence)
    assert any("declared `IVault`" in line for line in signals["viaLocal"].evidence)
    assert any("`tokens[who]` is `IERC20`" in line
               for line in signals["viaMapping"].evidence)


@needs_solidity_treesitter
def test_view_callee_lowers_confidence_and_says_why(tmp_path):
    signals = _by_function(_signals(tmp_path, CONTROL))
    item = signals["viaView"]
    assert item.confidence < 0.4
    assert any("STATICCALL" in line and "IVault.balance" in line for line in item.evidence)
    assert "read-only" in item.reason


# ---------------------------------------------------------------------------
# What cannot be resolved is kept, lowered, and said
# ---------------------------------------------------------------------------

UNRESOLVED = """
pragma solidity ^0.8.20;
import "./Vendor.sol";
contract Client is Vendor {
    uint256 public total;
    function poke() external {
        registry.poke();
        total += 1;
    }
}
"""


def test_unresolved_receiver_keeps_signal_at_reduced_confidence(tmp_path):
    signals = _by_function(_signals(tmp_path, UNRESOLVED))
    item = signals["poke"]
    assert item.confidence == round(0.62 - reentrancy.UNRESOLVED_PENALTY, 3)
    assert any("`registry` is not declared" in line for line in item.evidence)
    assert any("signal kept" in line for line in item.evidence)
    assert "could not be resolved" in item.reason


CONSTRUCTOR = """
pragma solidity ^0.8.20;
interface IRegistry { function register() external returns (uint256); }
contract Strategy {
    uint256 public id;
    uint256 public scale;
    constructor(address registry) {
        id = IRegistry(registry).register();
        scale = id * 2;
    }
}
"""


def test_constructor_callback_cannot_reenter(tmp_path):
    signals = _by_function(_signals(tmp_path, CONSTRUCTOR))
    item = signals["constructor"]
    assert item.confidence == round(0.62 - reentrancy.CONSTRUCTOR_PENALTY, 3)
    assert any("constructor body" in line for line in item.evidence)


CHAIN = """
pragma solidity ^0.8.20;
interface IStrategy {
    function getSupportedTokens() external view returns (address[] memory);
    function rebalance() external;
}
interface ICDO { function strategy() external view returns (IStrategy); }
contract Depositor {
    mapping(address => bool) public tranches;
    function addCdo(ICDO cdo) external {
        cdo.strategy().getSupportedTokens();
        tranches[address(cdo)] = true;
    }
    function poke(ICDO cdo) external {
        cdo.strategy().rebalance();
        tranches[address(cdo)] = true;
    }
}
"""


@needs_solidity_treesitter
def test_receiver_chain_follows_view_hops_to_the_final_callee(tmp_path):
    signals = _by_function(_signals(tmp_path, CHAIN))
    quiet, loud = signals["addCdo"], signals["poke"]
    assert any("`cdo.strategy()` is `IStrategy`" in line for line in quiet.evidence)
    assert any("IStrategy.getSupportedTokens" in line and "STATICCALL" in line
               for line in quiet.evidence)
    assert quiet.confidence < 0.4
    assert loud.confidence >= 0.62
    assert not any("STATICCALL" in line for line in loud.evidence)


# ---------------------------------------------------------------------------
# The Flyover call that is a real transfer keeps its full shape
# ---------------------------------------------------------------------------

CALL_FOR_USER = """
pragma solidity ^0.8.20;
library Quotes {
    struct PegInQuote { address contractAddress; uint256 gasLimit; uint256 value; bytes data; }
}
contract PegIn {
    mapping(bytes32 => uint8) private _processed;
    function callForUser(Quotes.PegInQuote calldata quote, bytes32 h) external payable returns (bool) {
        require(_processed[h] == 0);
        (bool success,) = quote.contractAddress.call{gas: quote.gasLimit, value: quote.value}(quote.data);
        _processed[h] = 1;
        return success;
    }
}
"""


def test_flyover_call_for_user_shape_still_fires(tmp_path):
    signals = _by_function(_signals(tmp_path, CALL_FOR_USER))
    item = signals["callForUser"]
    assert item.confidence > 0.8
    assert any("low-level `call` on `quote.contractAddress`" in line for line in item.evidence)
    assert any("read before the call and written after: _processed" in line
               for line in item.evidence)
