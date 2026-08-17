from dataclasses import dataclass

@dataclass(frozen=True)
class OracleSignal:
    contract: str
    function: str
    kind: str
    confidence: float
    evidence: list[str]
    heuristic: bool = True

WORDS = {
    "oracle": "oracle",
    "price": "price",
    "latestanswer": "oracle-read",
    "getprice": "oracle-read",
    "getlatestprice": "oracle-read",
    "twap": "twap",
}

def detect_oracle_signals(contracts):
    result = []
    for c in contracts:
        for f in c.functions:
            low = f.name.lower()
            for word, kind in WORDS.items():
                if word in low:
                    result.append(OracleSignal(
                        c.name, f.name, kind, .55,
                        [f"heuristic:function-name:{word}"],
                        True,
                    ))
                    break
    return result
