from dataclasses import dataclass

@dataclass(frozen=True)
class FeeSignal:
    contract: str
    function: str
    confidence: float
    evidence: list[str]

def detect_fee_signals(contracts):
    result = []
    for c in contracts:
        for f in c.functions:
            names = set(f.reads) | set(f.writes)
            hits = [x for x in names if "fee" in x.lower()]
            if "fee" in f.name.lower() or hits:
                result.append(FeeSignal(
                    c.name, f.name, .72,
                    [f"state:{x}" for x in sorted(hits)] or [f"function:{f.name}"]
                ))
    return result
