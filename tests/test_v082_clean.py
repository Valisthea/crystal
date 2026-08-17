from crystal.engine import research

SRC = """
pragma solidity ^0.8.20;
contract C {
 uint256 public totalAssets;
 uint256 public totalSupply;
 function deposit() external payable { totalAssets += msg.value; totalSupply += msg.value; }
 function withdraw(uint256 x) external { totalAssets -= x; totalSupply -= x; }
}
"""

def test_clean_does_not_emit_asymmetry(tmp_path):
    (tmp_path/"C.sol").write_text(SRC)
    r=research(tmp_path,use_solc=False)
    assert not any(x.kind == "asset-share-asymmetry" for x in r["delta_anomalies"])
