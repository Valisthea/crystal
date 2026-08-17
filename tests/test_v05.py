from crystal.engine import research

SOURCE = """
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public nonce;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        nonce += 1;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
    }

    function quote(uint256 x, uint256 rate) external pure returns (uint256) {
        return x / rate;
    }
}
"""

def test_graph_stack(tmp_path):
    (tmp_path/"Vault.sol").write_text(SOURCE)
    r = research(tmp_path, use_solc=False)
    assert r["cfgs"]
    assert r["storage_graph"]
    assert r["state_graph"].transitions
    assert r["program_graph"].edges
    assert r["sequence_hypotheses"]
    assert r["invariant_candidates"]
    assert r["compiler"]["available"] is False
