from crystal.parsers import solidity_regex
from crystal.symbolic import SymbolicEngine, boundary_values, domain_for
from crystal.symbolic.algebra import ExpressionReader, SymExpr

SOURCE = """
pragma solidity ^0.8.20;
contract V {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }
    function donate() external payable { totalAssets += msg.value; }
    function withdraw(uint256 x) external {
        balances[msg.sender] -= x;
        totalAssets -= x;
        totalSupply -= x;
    }
    function transfer(address to, uint256 a) external {
        balances[msg.sender] -= a;
        balances[to] += a;
    }
    function mintShares(uint256 assets) external returns (uint256 shares) {
        if (totalSupply == 0) { shares = assets; } else { shares = assets * totalSupply / totalAssets; }
        totalSupply += shares;
        totalAssets += assets;
    }
    function _credit(uint256 amount) internal { totalAssets += amount; }
    function depositVia(uint256 amount) external { _credit(amount); totalSupply += amount; }
    function loop3() external { for (uint256 i = 0; i < 3; i++) { totalAssets += 1; } }
    function guarded(uint256 x) external {
        require(x > 0);
        if (x > 100) { revert("too big"); }
        totalAssets += x;
    }
    function unbounded(uint256 n) external { while (totalAssets < n) { totalAssets += 1; } }
}
"""


def engine():
    return SymbolicEngine(solidity_regex.parse_text(SOURCE, "V.sol"))


def effect(name):
    symbolic = engine()
    function = symbolic.functions[name]
    return symbolic.execute_function(function)


# -- algebra ---------------------------------------------------------------

def test_algebra_is_canonical():
    a = SymExpr.symbol("ARG:x")
    b = SymExpr.symbol("ARG:y")
    assert (a + b).render() == (b + a).render()
    assert (a - a).is_zero
    assert (a - a).render() == "0"
    assert (a + a).render() == "2*ARG:x"
    assert (a * b).render() == (b * a).render()


def test_algebra_division_and_opacity():
    a = SymExpr.const(10)
    assert a.divide(SymExpr.const(2)).constant_value == 5
    symbol = SymExpr.symbol("ARG:x")
    assert symbol.divide(symbol).constant_value == 1
    opaque = symbol.divide(SymExpr.symbol("S0:totalSupply"))
    assert "DIV(" in opaque.render()


def test_algebra_substitution():
    expression = SymExpr.symbol("ARG:x") * SymExpr.const(3)
    concrete = expression.substitute({"ARG:x": SymExpr.const(7)})
    assert concrete.constant_value == 21


def test_expression_reader_handles_units_and_calls():
    reader = ExpressionReader(lambda path: SymExpr.symbol(f"S:{path}"))
    assert reader.read("1 ether").constant_value == 10 ** 18
    assert reader.read("2 + 3 * 4").constant_value == 14
    assert reader.read("uint256(a)").render() == "S:a"
    assert "RET:" in reader.read("foo(1)").render()


# -- engine ----------------------------------------------------------------

def test_simple_deltas():
    assert effect("V.deposit").deltas == {
        "balances": "ARG:msg.value",
        "totalAssets": "ARG:msg.value",
        "totalSupply": "ARG:msg.value",
    }
    assert effect("V.withdraw").deltas["totalAssets"] == "-ARG:x"


def test_internal_transfer_conserves_state():
    assert effect("V.transfer").deltas == {"balances": "0"}


def test_branches_are_explored_separately():
    result = effect("V.mintShares")
    assert result.branch_dependent
    assert len(result.paths) == 2
    assert "PHI(" in result.deltas["totalSupply"]
    assert any("DIV(" in path.deltas["totalSupply"] for path in result.paths)


def test_internal_calls_are_inlined():
    assert effect("V.depositVia").deltas == {
        "totalAssets": "ARG:amount", "totalSupply": "ARG:amount",
    }


def test_bounded_loop_is_unrolled():
    assert effect("V.loop3").deltas["totalAssets"] == "3"


def test_unbounded_loop_is_reported_not_guessed():
    result = effect("V.unbounded")
    assert any("unbounded loop" in note for note in result.unsupported)


def test_reverting_path_is_pruned():
    result = effect("V.guarded")
    assert result.deltas["totalAssets"] == "ARG:x"
    assert all(path.feasible for path in result.paths)


def test_guards_are_recorded_never_invented():
    kinds = {c.kind for c in effect("V.guarded").constraints}
    assert "require" in kinds
    assert not effect("V.donate").constraints


# -- sequences -------------------------------------------------------------

def test_sequence_accumulates_per_step_symbols():
    result = engine().execute_sequence(["V.deposit", "V.donate"])
    assert result.deltas["V::totalAssets"] == "ARG:msg.value#1 + ARG:msg.value#2"
    assert result.deltas["V::totalSupply"] == "ARG:msg.value#1"


def test_sequence_detects_symmetry():
    result = engine().execute_sequence(["V.deposit", "V.withdraw"])
    assert result.deltas["V::totalAssets"] == result.deltas["V::totalSupply"]


def test_sequence_before_after_are_symbolic():
    result = engine().execute_sequence(["V.donate"])
    assert result.before["V::totalAssets"].startswith("S0:")
    assert "ARG:msg.value#1" in result.after["V::totalAssets"]


def test_single_function_effects_stay_unqualified():
    """A function effect lives in one contract, so it keeps the bare names the
    rest of that contract's model is keyed by. Only shared state is namespaced."""
    assert set(effect("V.deposit").deltas) == {
        "balances", "totalAssets", "totalSupply",
    }


def test_unknown_function_yields_none():
    assert engine().execute_sequence(["V.nope"]) is None


def test_constraint_system_uses_declared_types():
    system = engine().constraint_system(["V.withdraw"])
    assert system.parameter_ranges["V.withdraw"] == (0, 2 ** 16 - 1)
    assert system.parameter_ranges["V.withdraw.x"] == (0, 2 ** 256 - 1)
    assert system.parameter_types["V.withdraw.x"] == "uint256"


# -- constraints -----------------------------------------------------------

def test_domains_and_boundaries():
    assert domain_for("uint8") == (0, 255)
    assert domain_for("bool") == (0, 1)
    assert domain_for("address")[1] == 2 ** 160 - 1
    assert boundary_values("bool") == (0, 1)
    assert 0 in boundary_values("uint256")
    assert 2 ** 256 - 1 in boundary_values("uint256")
