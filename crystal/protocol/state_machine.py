from dataclasses import dataclass

@dataclass(frozen=True)
class Transition:
    source: str
    target: str
    shared_state: list[str]
    confidence: float

def infer_transitions(contracts):
    out = []
    for c in contracts:
        funcs = c.functions
        for a in funcs:
            for b in funcs:
                if a.name == b.name:
                    continue
                shared = sorted(a.writes & (b.reads | b.writes))
                if shared:
                    out.append(Transition(
                        f"{c.name}.{a.name}",
                        f"{c.name}.{b.name}",
                        shared,
                        .70
                    ))
    return out
