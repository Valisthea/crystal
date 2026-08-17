
def test_crystal_is_evidence_only():
    from crystal.research.evidence import make_evidence
    x=make_evidence(status="EVIDENCE", hypothesis=["Vault.a"])
    assert x.tool == "crystal"
    assert x.status == "EVIDENCE"
    assert not hasattr(x, "severity")
    assert not hasattr(x, "submission")

def test_no_final_finding_contract():
    import json
    from pathlib import Path
    p=Path("ARCHITECTURE.md").read_text()
    assert "does not replace Arcadia" in p
    assert "does not act as an LLM" in p
    assert "Arcadia + Claude" in p
    assert "evidence, not conclusions" in p
