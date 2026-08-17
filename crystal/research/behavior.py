from dataclasses import dataclass

@dataclass(frozen=True)
class BehaviorRelation:
    left: str
    right: str
    relation: str
    confidence: float
    reason: str

def derive_behavior_relations(contracts):
    """
    Find pairs of public/external state-changing functions that touch overlapping
    state. These are candidates for differential and sequence analysis.
    """
    out = []
    for c in contracts:
        funcs = [f for f in c.functions if f.visibility in {"public", "external"}]
        for i, a in enumerate(funcs):
            for b in funcs[i+1:]:
                shared = sorted((a.reads | a.writes) & (b.reads | b.writes))
                if not shared:
                    continue
                if a.writes or b.writes:
                    out.append(BehaviorRelation(
                        f"{c.name}.{a.name}",
                        f"{c.name}.{b.name}",
                        "shared-state-behavior",
                        .58,
                        "both entry points influence overlapping state: " + ", ".join(shared)
                    ))
    return out
