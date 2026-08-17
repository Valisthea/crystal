from crystal.engine import research

SOURCE = """
pragma solidity ^0.8.20;

contract Base {
    uint256 public value;
}

contract ProxyLike is Base {
    address public implementation;

    modifier onlyAdmin() {
        _;
    }

    function upgradeTo(address x) external onlyAdmin {
        implementation = x;
    }

    function setValue(uint256 x) external {
        value = x;
    }

    function readValue() external view returns (uint256) {
        return value;
    }
}
"""

def test_semantic_graphs(tmp_path):
    (tmp_path / "ProxyLike.sol").write_text(SOURCE)
    r = research(tmp_path, use_solc=False)

    assert r["inheritance_graph"]
    assert r["modifier_graph"]
    assert r["proxy_signals"]
    assert r["dataflow"]
    assert r["cfgs"]

def test_semantic_model_absent_solc_is_clean(tmp_path):
    (tmp_path / "Empty.sol").write_text("pragma solidity ^0.8.20; contract Empty {}")
    from crystal.semantics.solc_ast import parse_solc_ast
    model = parse_solc_ast(tmp_path)
    assert model.available in (True, False)
