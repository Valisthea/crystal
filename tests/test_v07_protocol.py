from crystal.engine import research

SOURCE = """
pragma solidity ^0.8.20;

interface IFeed {
    function latestRoundData() external view returns (uint80,int256,uint256,uint256,uint80);
}

contract Vault {
    address feed;
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

    // Reads its own storage. Named like an oracle, and not one — this is the
    // case that used to produce a signal, on the strength of the word.
    function getPrice() external view returns (uint256) {
        return price;
    }

    function quote() external view returns (uint256) {
        (, int256 answer,,,) = IFeed(feed).latestRoundData();
        return uint256(answer);
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
    # The external feed read, and only that one. `getPrice` returns local
    # storage: a name, not a price source.
    assert [(x.contract, x.function) for x in pm.oracle_signals] == [("Vault", "quote")]
    assert pm.transitions
    assert r["protocol_invariants"]
