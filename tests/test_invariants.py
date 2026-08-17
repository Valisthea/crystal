from crystal.discovery import discover
from crystal.parser import parse_sources
from crystal.invariants import discover_invariants

def test_invariant_candidates(tmp_path):
    (tmp_path / "V.sol").write_text("""
    pragma solidity ^0.8.20;
    contract V {
        uint256 public totalAssets;
        uint256 public totalSupply;
        uint256 public nonce;
    }
    """)
    contracts = parse_sources(discover(tmp_path))
    candidates = discover_invariants(contracts)
    assert any(x.category == "asset/share consistency" for x in candidates)
    assert any(x.category == "monotonicity" for x in candidates)
