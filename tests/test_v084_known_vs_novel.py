from crystal.research.novelty import NoveltyScore

def test_novelty_score_is_not_confidence():
    x=NoveltyScore(.05,.05,.9,.9,.9,.9)
    assert x.overall > .7
    # High novelty must not imply a confirmed finding.
    assert x.overall < 1.0
