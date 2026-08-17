from crystal.engine import research

SRC="""
pragma solidity ^0.8.20;
contract C {
 uint256 public totalAssets;
 uint256 public totalSupply;
 function deposit() external payable { totalAssets += msg.value; totalSupply += msg.value; }
 function withdraw(uint256 x) external { totalAssets -= x; totalSupply -= x; }
}
"""
def test_clean_has_no_spurious_three_step_composition(tmp_path):
    (tmp_path/"C.sol").write_text(SRC)
    r=research(tmp_path,use_solc=False)
    assert all(len(x.chain) < 3 for x in r["composition_candidates"])
