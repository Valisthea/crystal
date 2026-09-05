from crystal.engine import research

SRC = """
pragma solidity ^0.8.20;
contract V {
    mapping(address=>uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }
    function donate() external payable {
        totalAssets += msg.value;
    }
    function withdraw(uint256 x) external {
        balances[msg.sender] -= x;
        totalAssets -= x;
        totalSupply -= x;
    }
}
"""

def test_state_delta_finds_asset_share_asymmetry(tmp_path):
    (tmp_path/"V.sol").write_text(SRC)
    r=research(tmp_path,use_solc=False)
    assert r["state_deltas"]
    assert r["delta_anomalies"]
    assert any(x.kind == "asset-share-asymmetry" for x in r["delta_anomalies"])
    target=next(x for x in r["delta_anomalies"]
                 if x.kind == "asset-share-asymmetry")
    assert "totalAssets" in target.sequence or target.state == "V::totalAssets"
    assert "ARG" in target.delta
