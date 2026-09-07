from crystal.engine import research
from crystal.quality.validation import rules

SOURCE = """
pragma solidity ^0.8.20;
interface IFeed {
    function latestRoundData() external view returns (uint80,int256,uint256,uint256,uint80);
}

contract Vault {
    address feed;
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

    // Returns its own storage: not a price source, whatever it is called.
    function getPrice() external view returns (uint256) {
        return price;
    }

    function quote() external view returns (uint256) {
        (, int256 answer,,,) = IFeed(feed).latestRoundData();
        return uint256(answer);
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
    # One signal: the external read in `quote`. Not `getPrice`, which reads
    # local storage and used to qualify on its name alone.
    signals = r["protocol_model"].oracle_signals
    assert [x.function for x in signals] == ["quote"]
    assert len(r["protocol_model"].token_functions) == 0
    assert len(rules()) >= 5
