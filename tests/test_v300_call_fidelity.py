"""Build 012: the struct literal that was reported as a shared transition.

On CapyFi a detector emitted "shared transition: Exp({mantissa:
CToken(cToken).borrowIndex()})". `Exp({...})` builds a struct, `CToken(cToken)`
casts an address; neither transfers control, yet the IR had recorded both as
calls, and the real external call hiding inside them — `borrowIndex()` — was
the one thing it had not recorded.

These tests pin, for both Solidity front-ends:
  - elementary casts, `payable(x)`, `type(T).max` and struct literals are not
    calls;
  - a cast to a contract, interface, library or struct declared anywhere in
    the project is not a call, and the call it wraps is still found;
  - a name the project declares both as a type and as a function stays a
    call (Crystal cannot tell; a lost real call is worse than a spurious one);
  - a name declared nowhere in the project stays a call, for the same reason;
  - external, internal, library, value-transfer, low-level and creation calls
    are all still recorded, and the two front-ends record the same thing.
"""

import pytest

from crystal import ir as I
from crystal.parsers import parse_project, solidity_regex, solidity_ts
from crystal.parsers.solidity_types import TypeCatalog, catalog_from_text

FRONT_ENDS = [pytest.param(solidity_regex, id="regex")]
if solidity_ts.available():
    FRONT_ENDS.append(pytest.param(solidity_ts, id="tree-sitter"))

TYPES = """
pragma solidity ^0.8.20;

struct FileLevel { uint256 a; }

interface IToken {
    function balanceOf(address who) external view returns (uint256);
    function borrowIndex() external view returns (uint256);
}

library MathLib {
    struct Pair { uint256 a; uint256 b; }
    function half(uint256 x) internal pure returns (uint256) { return x / 2; }
}

contract ExponentialNoError {
    struct Exp { uint256 mantissa; }
    struct Double { uint256 mantissa; }
    enum Kind { Zero, One }
}

contract CToken {
    function borrowIndex() external view returns (uint256) { return 1; }
}
"""

COMPTROLLER = """
pragma solidity ^0.8.20;
import "./Types.sol";

contract Comptroller is ExponentialNoError {
    uint256 public total;

    function elementary(uint256 x, address who, bytes memory data) public view returns (uint256) {
        address a = address(uint160(x));
        uint256 u = uint256(uint160(who));
        address payable p = payable(who);
        bytes32 h = bytes32(x);
        int256 i = int256(x);
        string memory s = string(data);
        bytes memory b = bytes(s);
        uint256 m = type(uint256).max;
        bytes memory code = type(Comptroller).creationCode;
        uint256 bal = address(this).balance;
        return u + m + bal;
    }

    function userTypes(address cToken) public pure returns (uint256) {
        Exp memory e = Exp({mantissa: 1});
        Double memory d = Double(2);
        MathLib.Pair memory pair = MathLib.Pair(1, 2);
        MathLib.Pair memory named = MathLib.Pair({a: 1, b: 2});
        FileLevel memory f = FileLevel(3);
        Kind k = Kind(1);
        IToken token = IToken(cToken);
        CToken c = CToken(cToken);
        return e.mantissa + d.mantissa + pair.a + named.b + f.a + uint256(k);
    }

    function hidden(address cToken) public view returns (uint256) {
        Exp memory e = Exp({mantissa: CToken(cToken).borrowIndex()});
        return e.mantissa;
    }

    function allocation(uint256 n) public pure returns (uint256) {
        address[] memory list = new address[](n);
        uint256[] memory nums = new uint256[](n);
        return list.length + nums.length;
    }

    function realCalls(address cToken, address payable to, uint256 amount) public payable returns (uint256) {
        uint256 bal = IToken(cToken).balanceOf(address(this));
        uint256 h = MathLib.half(bal);
        uint256 t = helper(h);
        to.transfer(amount);
        (bool ok, ) = to.call{value: amount}("");
        require(ok);
        CToken made = new CToken();
        total += t;
        return bal;
    }

    function helper(uint256 x) internal pure returns (uint256) { return x + 1; }
}
"""

AMBIGUOUS = """
pragma solidity ^0.8.20;

contract Shapes {
    struct Circle { uint256 r; }
}

contract Factory {
    function Circle(uint256 r) internal pure returns (uint256) { return r; }
    function make() public pure returns (uint256) {
        uint256 x = Circle(1);
        return x;
    }
}
"""

SINGLE_FILE = """
pragma solidity ^0.8.20;

interface IDeclaredHere { function ping() external; }

contract Single {
    struct Slot { uint256 v; }
    function f(address a) public pure returns (uint256) {
        Slot memory s = Slot({v: 1});
        IDeclaredHere here = IDeclaredHere(a);
        IOutside outside = IOutside(a);
        return s.v;
    }
}
"""


def _write(tmp_path):
    (tmp_path / "Types.sol").write_text(TYPES)
    (tmp_path / "Comptroller.sol").write_text(COMPTROLLER)
    (tmp_path / "Ambiguous.sol").write_text(AMBIGUOUS)
    return sorted(tmp_path.glob("*.sol"))


def _function(contracts, contract_name, function_name):
    contract = next(c for c in contracts if c.name == contract_name)
    return next(f for f in contract.functions if f.name == function_name)


def _calls(contracts, contract_name, function_name):
    return list(_function(contracts, contract_name, function_name).ir.calls())


def _shape(call):
    return call.callee, call.kind, call.receiver


# -- casts and literals are not calls ------------------------------------

@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_elementary_casts_and_meta_types_are_not_calls(front_end, tmp_path):
    contracts = front_end.parse_sources(_write(tmp_path))
    assert _calls(contracts, "Comptroller", "elementary") == []


@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_struct_literals_and_user_type_casts_are_not_calls(front_end, tmp_path):
    """`Exp`, `Double` and `Kind` come from a base contract in another file,
    `CToken`/`IToken`/`FileLevel` from that other file's top level, `Pair`
    through its library: all resolved through the project's declarations."""
    contracts = front_end.parse_sources(_write(tmp_path))
    assert _calls(contracts, "Comptroller", "userTypes") == []


@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_the_call_a_struct_literal_wraps_is_recorded_instead(front_end, tmp_path):
    """The CapyFi line: `Exp({mantissa: CToken(cToken).borrowIndex()})`.

    Neither `Exp` nor `CToken` is a callee. `borrowIndex` is, and before this
    build the literal masked it entirely."""
    contracts = front_end.parse_sources(_write(tmp_path))
    calls = _calls(contracts, "Comptroller", "hidden")
    assert [_shape(c) for c in calls] == [
        ("borrowIndex", I.EXTERNAL_CALL, "CToken(cToken)"),
    ]


@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_memory_array_allocation_is_not_a_call(front_end, tmp_path):
    contracts = front_end.parse_sources(_write(tmp_path))
    assert _calls(contracts, "Comptroller", "allocation") == []


# -- real calls survive ---------------------------------------------------

@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_every_real_call_kind_is_still_recorded(front_end, tmp_path):
    contracts = front_end.parse_sources(_write(tmp_path))
    calls = _calls(contracts, "Comptroller", "realCalls")
    shapes = {_shape(c) for c in calls}
    assert ("balanceOf", I.EXTERNAL_CALL, "IToken(cToken)") in shapes
    assert ("half", I.EXTERNAL_CALL, "MathLib") in shapes
    assert ("helper", I.INTERNAL_CALL, None) in shapes
    assert ("transfer", I.VALUE_TRANSFER, "to") in shapes
    assert ("call", I.LOW_LEVEL_CALL, "to") in shapes
    transfer = next(c for c in calls if c.callee == "transfer")
    low_level = next(c for c in calls if c.callee == "call")
    assert transfer.value_attached and low_level.value_attached
    # Contract creation runs a constructor: kept, whichever way it is named.
    creation = [c for c in calls if c.callee.endswith("CToken")]
    assert creation and creation[0].kind == I.INTERNAL_CALL
    assert len(calls) == 6


@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_a_name_declared_as_type_and_function_stays_a_call(front_end, tmp_path):
    """`Circle` is a struct in `Shapes` and a function in `Factory`: Crystal
    cannot tell which one `Circle(1)` reaches, so it keeps the call."""
    contracts = front_end.parse_sources(_write(tmp_path))
    assert [_shape(c) for c in _calls(contracts, "Factory", "make")] == [
        ("Circle", I.INTERNAL_CALL, None),
    ]


@pytest.mark.parametrize("front_end", FRONT_ENDS)
def test_a_name_declared_nowhere_stays_a_call(front_end):
    """`IOutside(a)` is almost certainly a cast, but nothing in the parsed
    sources says so; without evidence the call is kept rather than guessed
    away. `IDeclaredHere` and `Slot` are declared in the same file and go."""
    contracts = front_end.parse_text(SINGLE_FILE, "Single.sol")
    assert [_shape(c) for c in _calls(contracts, "Single", "f")] == [
        ("IOutside", I.INTERNAL_CALL, None),
    ]


@pytest.mark.skipif(not solidity_ts.available(), reason="tree-sitter-solidity not installed")
def test_the_function_call_name_list_follows_the_ir(tmp_path):
    contracts = solidity_ts.parse_sources(_write(tmp_path))
    assert _function(contracts, "Comptroller", "userTypes").calls == []
    # `<low-level-call>` is the long-standing marker for "has an external-kind
    # call"; the names beside it are what changed.
    hidden = _function(contracts, "Comptroller", "hidden").calls
    assert [name for name in hidden if not name.startswith("<")] == ["borrowIndex"]
    real = _function(contracts, "Comptroller", "realCalls").calls
    assert "<low-level-call>" in real and "IToken" not in real


# -- the two front-ends agree ---------------------------------------------

@pytest.mark.skipif(not solidity_ts.available(), reason="tree-sitter-solidity not installed")
def test_both_front_ends_record_the_same_calls(tmp_path):
    paths = _write(tmp_path)
    by_regex = solidity_regex.parse_sources(paths)
    by_tree = solidity_ts.parse_sources(paths)

    def normalised(contracts, name):
        # Creation naming is the one known difference: `CToken` vs `new CToken`.
        return sorted(
            (c.callee.replace("new ", ""), c.kind, c.receiver, c.value_attached)
            for c in _calls(contracts, "Comptroller", name)
        )

    for name in ("elementary", "userTypes", "hidden", "allocation", "realCalls"):
        assert normalised(by_regex, name) == normalised(by_tree, name), name


def test_parse_project_resolves_types_across_files(tmp_path, monkeypatch):
    """The public entry point builds one catalog per language group, so the
    file that casts sees the file that declares."""
    paths = _write(tmp_path)
    for mode in ("", "1"):
        monkeypatch.setenv("CRYSTAL_NO_TREESITTER", mode)
        contracts = parse_project(paths).contracts
        assert _calls(contracts, "Comptroller", "userTypes") == []
        assert [c.callee for c in _calls(contracts, "Comptroller", "hidden")] == ["borrowIndex"]


# -- the catalog itself ---------------------------------------------------

def test_catalog_reads_declarations_not_comments():
    catalog = catalog_from_text(
        "// struct Ghost { uint a; }\n"
        "/* contract Phantom {} */\n"
        "contract Real { struct Slot { uint256 v; } enum Mode { A } "
        "function go() public {} modifier only() { _; } }\n"
        "type Wad is uint256;\n"
    )
    assert catalog.contracts == {"Real"}
    assert catalog.structs == {"Slot", "Mode"}
    assert catalog.functions == {"go", "only"}
    assert catalog.value_types == {"Wad"}
    assert catalog.is_conversion("Slot") and catalog.is_conversion("Real")
    assert not catalog.is_conversion("go")
    assert catalog.is_qualified_conversion("Real", "Slot")
    assert catalog.is_qualified_conversion("Wad", "wrap")
    assert not catalog.is_qualified_conversion("Real", "go")


def test_catalog_treats_elementary_types_and_type_as_conversions():
    empty = TypeCatalog()
    for name in ("address", "payable", "uint256", "uint", "int8", "bytes32",
                 "bytes", "string", "bool", "ufixed128x18", "type"):
        assert empty.is_conversion(name), name
    for name in ("transfer", "balanceOf", "helper", "Exp", "IToken"):
        assert not empty.is_conversion(name), name
    # A project that declares a function named like a type keeps the call.
    guarded = TypeCatalog(structs=frozenset({"Exp"}), functions=frozenset({"Exp"}))
    assert not guarded.is_conversion("Exp")


def test_regex_front_end_collects_struct_and_enum_names():
    contracts = solidity_regex.parse_text(TYPES, "Types.sol")
    exponential = next(c for c in contracts if c.name == "ExponentialNoError")
    assert {(t.name, t.kind) for t in exponential.types} == {
        ("Exp", "struct"), ("Double", "struct"), ("Kind", "enum"),
    }
    exp = next(t for t in exponential.types if t.name == "Exp")
    assert exp.members == ["uint256 mantissa"]
    library = next(c for c in contracts if c.name == "MathLib")
    assert [t.name for t in library.types] == ["Pair"]
