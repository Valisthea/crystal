import pytest

from crystal.backends import BACKENDS, backend_names, capabilities, run_backend
from crystal.backends.base import derive_properties
from crystal.parsers import solidity_regex
from crystal.protocol.invariants import derive_protocol_invariants
from crystal.protocol.model import build_protocol_model
from crystal.research.foundry import argument_literal, build_plan, render_harness

VAULT = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public nonce;
    mapping(address => uint256) public balances;

    function deposit(uint256 assets, address to) external returns (uint256) {
        totalAssets += assets;
        totalSupply += assets;
        nonce += 1;
        balances[to] += assets;
        return assets;
    }
    function withdraw(uint256 assets) external { totalAssets -= assets; totalSupply -= assets; }
}
"""

WITH_CONSTRUCTOR = """
pragma solidity ^0.8.20;
contract Owned {
    address public owner;
    uint256 public totalAssets;
    uint256 public totalSupply;
    constructor(address _owner, uint256 seed) { owner = _owner; totalAssets = seed; }
    function poke(uint256 x) external { totalAssets += x; totalSupply += x; }
}
"""

EXOTIC_CONSTRUCTOR = """
pragma solidity ^0.8.20;
contract Exotic {
    uint256 public totalAssets;
    uint256 public totalSupply;
    constructor(Weird w) { }
    function poke(uint256 x) external { totalAssets += x; }
}
"""


def contracts(source, name="Vault.sol", tmp_path=None):
    if tmp_path is not None:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return solidity_regex.parse_text(source, str(path))
    return solidity_regex.parse_text(source, name)


def invariants(parsed):
    return derive_protocol_invariants(build_protocol_model(parsed))


# -- Foundry argument synthesis -------------------------------------------

def test_argument_literals_cover_common_types():
    assert argument_literal("uint256", 0) == "1"
    assert argument_literal("uint256", 1) == "0"
    assert argument_literal("uint256", 2) == "type(uint256).max"
    assert argument_literal("address", 0).startswith("address(")
    assert argument_literal("bool", 0) == "true"
    assert argument_literal("bytes32", 0) == "bytes32(0)"
    assert argument_literal("uint256[]", 0).startswith("new uint256[]")


def test_argument_literal_refuses_unknown_types():
    assert argument_literal("SomeStruct", 0) is None
    assert argument_literal("", 0) is None


def test_foundry_supports_functions_with_parameters(tmp_path):
    parsed = contracts(VAULT, "Vault.sol", tmp_path)
    plan, reason = build_plan(tmp_path, parsed, ["Vault.deposit", "Vault.withdraw"])
    assert plan is not None, reason
    harness = render_harness(plan)
    assert "new Vault()" in harness
    assert "target_Vault.deposit(" in harness
    assert "target_Vault.withdraw(" in harness
    assert "CRYSTAL_STEP" in harness


def test_foundry_derives_constructor_arguments(tmp_path):
    parsed = contracts(WITH_CONSTRUCTOR, "Owned.sol", tmp_path)
    plan, reason = build_plan(tmp_path, parsed, ["Owned.poke"])
    assert plan is not None, reason
    assert "new Owned(address(0xA11CE), 0)" in render_harness(plan)


def test_foundry_refuses_underivable_constructor(tmp_path):
    parsed = contracts(EXOTIC_CONSTRUCTOR, "Exotic.sol", tmp_path)
    plan, reason = build_plan(tmp_path, parsed, ["Exotic.poke"])
    assert plan is None
    assert "constructor argument" in reason


def test_foundry_refuses_missing_source(tmp_path):
    parsed = contracts(VAULT, "Vault.sol")
    plan, reason = build_plan(tmp_path, parsed, ["Vault.deposit"])
    assert plan is None
    assert "source file" in reason


# -- Property derivation ---------------------------------------------------

def test_properties_are_derived_only_from_readable_state():
    parsed = contracts(VAULT)
    properties, unsupported = derive_properties(parsed[0], invariants(parsed))
    names = {p.name for p in properties}
    assert any("monotonic" in name for name in names)
    assert any("coherent" in name for name in names)
    assert any("conservation" in item or "not publicly readable" in item
               for item in unsupported) or unsupported == []


def test_properties_refuse_aggregate_conservation():
    source = """
    pragma solidity ^0.8.20;
    contract B { mapping(address => uint256) public balances; }
    """
    parsed = contracts(source, "B.sol")
    properties, unsupported = derive_properties(parsed[0], invariants(parsed))
    assert not properties
    assert any("will not approximate" in item for item in unsupported)


# -- Backend registry ------------------------------------------------------

def test_backend_registry():
    assert backend_names() == ["echidna", "halmos", "medusa"]
    assert set(BACKENDS) == set(backend_names())


def test_capabilities_never_raise():
    for name, capability in capabilities().items():
        assert capability.name == name
        assert isinstance(capability.available, bool)
        assert capability.reason


def test_unknown_backend_is_rejected():
    with pytest.raises(ValueError, match="unknown backend"):
        run_backend("slither", ".", [], ())


@pytest.mark.parametrize("backend", ["medusa", "echidna", "halmos"])
def test_generate_only_produces_artifacts_without_running(backend, tmp_path):
    parsed = contracts(VAULT, "Vault.sol", tmp_path)
    results = run_backend(
        backend, tmp_path, parsed, invariants(parsed), generate_only=True
    )
    assert results
    result = results[0]
    assert result.status == "GENERATED"
    assert result.backend == backend
    assert result.properties
    assert result.artifacts
    body = "\n".join(result.artifacts.values())
    assert "Vault" in body
    assert not result.reproducible


def test_medusa_config_shape():
    from crystal.backends.medusa import config

    data = config("CrystalMedusaHarness")
    assert data["fuzzing"]["targetContracts"] == ["CrystalMedusaHarness"]
    assert data["fuzzing"]["testing"]["propertyTesting"]["testPrefixes"] == ["property_"]


def test_echidna_config_shape():
    from crystal.backends.echidna import config

    text = config()
    assert "testMode: property" in text
    assert 'prefix: "property_"' in text


def test_halmos_generates_symbolic_signatures(tmp_path):
    parsed = contracts(VAULT, "Vault.sol", tmp_path)
    results = run_backend(
        "halmos", tmp_path, parsed, invariants(parsed), generate_only=True
    )
    source = "\n".join(results[0].artifacts.values())
    assert "function check_" in source
    assert "uint256 assets" in source
    assert "assert(" in source
