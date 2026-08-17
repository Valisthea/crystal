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
}
"""

def test_program_graph_and_sequences(tmp_path):
    (tmp_path / "Vault.sol").write_text(SOURCE)
    result = research(tmp_path)

    assert len(result["program_graph"].nodes) > 0
    assert any(e.kind == "state_dependency" for e in result["program_graph"].edges)
    assert result["sequence_hypotheses"]
    assert result["invariant_candidates"]
