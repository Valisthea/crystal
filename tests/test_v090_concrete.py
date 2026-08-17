from crystal.research.concrete import fuzz_hypothesis

SRC="""
pragma solidity ^0.8.20;
contract V {
 uint256 public totalAssets;
 uint256 public totalSupply;
 function donate() external payable { totalAssets += msg.value; }
 function withdraw(uint256 x) external { totalAssets -= x; totalSupply -= x; }
}
"""

def test_concrete_engine_is_deterministic(tmp_path):
    (tmp_path/"V.sol").write_text(SRC)
    from crystal.engine import research
    r=research(tmp_path,use_solc=False)
    assert "concrete_validation" in r
    a=[x for x in r["concrete_validation"]]
    b=[x for x in r["concrete_validation"]]
    assert a == b
    assert all(x.status in {"NO_COUNTEREXAMPLE","COUNTEREXAMPLE"} for x in a)

def test_counterexample_does_not_equal_confirmed():
    from crystal.engine import research
    from pathlib import Path
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        Path(d,"V.sol").write_text(SRC)
        r=research(d,use_solc=False)
        assert r["finding_gate"]["concrete_validation_is_not_confirmation"]
        assert not any(x["status"]=="CONFIRMED" for x in r["finding_gate"]["decisions"])
