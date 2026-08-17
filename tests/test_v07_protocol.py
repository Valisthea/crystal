from crystal.engine import research

SOURCE = """
pragma solidity ^0.8.20;

contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public fee;
    uint256 public price;

    function transfer(address to, uint256 amount) external {
        balances[msg.sender] -= amount;
        balances[to] += amount;
    }

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }

    function withdraw(uint256 amount) external {
        balances[msg.sender] -= amount;
        totalAssets -= amount;
        totalSupply -= amount;
    }

    function getPrice() external view returns (uint256) {
        return price;
    }
}
"""

def test_protocol_model(tmp_path):
    (tmp_path/"Vault.sol").write_text(SOURCE)
    r = research(tmp_path, use_solc=False)
    pm = r["protocol_model"]

    assert any(x.kind == "transfer" for x in pm.token_functions)
    assert pm.value_flows
    assert pm.accounting_relations
    assert pm.oracle_signals
    assert pm.transitions
    assert r["protocol_invariants"]
