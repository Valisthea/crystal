from pathlib import Path
from crystal.discovery import discover
from crystal.parser import parse_sources
from crystal.analysis import analyze
from crystal.hypotheses import generate

SOURCE = '''
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
    }

    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
    }

    function quote(uint256 x, uint256 rate) external pure returns (uint256) {
        return x / rate;
    }
}
'''

def test_core_analysis(tmp_path: Path):
    src = tmp_path / "Vault.sol"
    src.write_text(SOURCE)
    contracts = parse_sources(discover(tmp_path))
    assert len(contracts) == 1
    assert len(contracts[0].functions) == 3
    observations = analyze(contracts)
    assert any(x.kind == "CROSS_FUNCTION_STATE" for x in observations)
    assert any(x.kind == "EXTERNAL_CALL_SURFACE" for x in observations)
    assert any(x.kind == "ARITHMETIC_SURFACE" for x in observations)
    hypotheses = generate(observations)
    assert any(x.category == "STATE_DEPENDENCY" for x in hypotheses)
