from crystal.engine import research
from crystal.quality.validation import rules

SOURCE = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public price;
    uint256 public fee;

    function deposit() external payable {
        totalAssets += msg.value;
        totalSupply += msg.value;
    }

    function withdraw(uint256 x) external {
        totalAssets -= x;
        totalSupply -= x;
    }

    function getPrice() external view returns (uint256) {
        return price;
    }
}
"""

def test_candidate_quality(tmp_path):
    (tmp_path/"Vault.sol").write_text(SOURCE)
    r = research(tmp_path, use_solc=False)
    c = r["research_candidates"]

    assert c
    assert all(0 <= x.confidence <= 1 for x in c)
    assert all(x.validation_required for x in c)
    assert all(x.id for x in c)
    assert all(x.source for x in c)
    assert len({x.id for x in c}) == len(c)
    assert len(r["protocol_model"].oracle_signals) == 1
    assert len(r["protocol_model"].token_functions) == 0
    assert len(rules()) >= 5
