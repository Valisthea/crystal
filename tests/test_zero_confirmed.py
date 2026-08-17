from crystal.engine import research

SRC = """
pragma solidity ^0.8.20;
contract V {
    uint256 public totalAssets;
    uint256 public totalSupply;
    function deposit() external payable {
        totalAssets += msg.value;
    }
    function donate() external payable {
        totalAssets += msg.value;
    }
}
"""

def test_heuristics_are_not_findings(tmp_path):
    (tmp_path/"V.sol").write_text(SRC)
    r = research(tmp_path, use_solc=False)
    assert r["finding_gate"]["policy"] == "zero-false-positive-confirmed"
    assert r["finding_gate"]["decisions"] == [] or all(
        x["status"] != "CONFIRMED" for x in r["finding_gate"]["decisions"]
    )
