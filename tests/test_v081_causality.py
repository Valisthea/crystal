from crystal.engine import research

SRC = """
pragma solidity ^0.8.20;
contract V {
    mapping(address=>uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public price;
    uint256 public fee;

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
    function updatePrice(uint256 p) external { price = p; }
}
"""

def test_causal_chain_and_relevance(tmp_path):
    (tmp_path/"V.sol").write_text(SRC)
    r=research(tmp_path,use_solc=False)
    chains=[x for x in r["composition_candidates"]]
    target=next(x for x in chains if x.chain == ["V.deposit","V.donate","V.withdraw"])
    assert "totalAssets" in target.relevant_state
    assert "totalSupply" in target.relevant_state
    assert all(e["consumed"] for e in target.causal_edges)
    assert all(x.chain != ["V.updatePrice","V.withdraw","V.deposit"] for x in chains)
    assert all(x.chain != ["V.withdraw","V.deposit","V.updatePrice"] for x in chains)
    assert any(set(e["consumed"]) == {"V::totalAssets"} for e in target.causal_edges)
