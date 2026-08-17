from crystal.research.composition import compose

def test_no_arbitrary_composition():
    result = {
        "sequence_hypotheses": [],
        "protocol_invariants": [],
        "impact_paths": [],
    }
    assert compose(result) == []
