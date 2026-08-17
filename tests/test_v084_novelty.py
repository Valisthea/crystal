from crystal.engine import research

SRC="""
pragma solidity ^0.8.20;
contract V {
    mapping(address=>uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public price;
    function deposit() external payable {
        balances[msg.sender]+=msg.value;
        totalAssets+=msg.value;
        totalSupply+=msg.value;
    }
    function donate() external payable { totalAssets+=msg.value; }
    function withdraw(uint256 x) external {
        balances[msg.sender]-=x;
        totalAssets-=x;
        totalSupply-=x;
    }
}
"""

def test_novelty_is_separate_from_confirmation(tmp_path):
    (tmp_path/"V.sol").write_text(SRC)
    r=research(tmp_path,use_solc=False)
    assert r["novel_behaviors"]
    assert r["unknown_behavior_candidates"]
    assert r["finding_gate"]["policy"]=="zero-false-positive-confirmed"
    assert not any(x["status"] == "CONFIRMED" for x in r["finding_gate"]["decisions"])
