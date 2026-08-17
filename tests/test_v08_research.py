from crystal.engine import research

SOURCE = """
pragma solidity ^0.8.20;

contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public price;
    uint256 public fee;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
        totalSupply -= amount;
    }

    function getPrice() external view returns (uint256) {
        return price;
    }

    function transfer(address to, uint256 amount) external {
        balances[msg.sender] -= amount;
        balances[to] += amount;
    }
}
"""

def test_behavioral_research_stack(tmp_path):
    (tmp_path/"Vault.sol").write_text(SOURCE)
    r = research(tmp_path, use_solc=False)

    assert r["behavior_relations"]
    assert r["differential_candidates"]
    assert r["mutations"]
    assert r["impact_paths"]
    assert r["composition_candidates"]

    for c in r["composition_candidates"]:
        assert c.validation_required
        assert 0 <= c.score <= 1
