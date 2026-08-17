from crystal.research.finding_gate import ProofChecklist, evaluate, anti_finding_checks

def test_incomplete_candidate_never_becomes_confirmed():
    proof = ProofChecklist(True, True, True, True, True, False, False, False)
    decision = evaluate(proof)
    assert decision.status == "VALIDATION"
    assert decision.status != "CONFIRMED"

def test_confirmed_requires_every_gate():
    proof = ProofChecklist(True, True, True, True, True, True, True, True)
    assert evaluate(proof).status == "CONFIRMED"

def test_anti_finding_checks_exist():
    assert len(anti_finding_checks(None)) >= 5
