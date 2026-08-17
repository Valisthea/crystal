
from crystal.research.unknown_behavior import discover_unknown_behaviors
from crystal.research.novelty import NovelBehavior, NoveltyScore

def test_known_behavior_is_not_unknown():
    x=NovelBehavior(
        ["V.deposit","V.withdraw"], ["totalAssets","totalSupply"],
        NoveltyScore(.80,.80,.90,.90,.90,.90), "known"
    )
    assert discover_unknown_behaviors([x]) == []

def test_novelty_never_confirms():
    x=NovelBehavior(
        ["V.a","V.b","V.c"], ["x"],
        NoveltyScore(.01,.01,.99,.99,.99,.99), "novel"
    )
    assert discover_unknown_behaviors([x])
