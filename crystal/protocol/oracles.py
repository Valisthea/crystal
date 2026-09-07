"""Functions that actually consume an external price.

The previous version matched `price`, `oracle` or `twap` anywhere in a
function's name. On the Lido stonks protocol that produced 27 signals, of which
several were interface declarations with no body and several were setters that
write a threshold and read nothing. It also missed the point: the question is
not what a function is called, it is whether a value Crystal cannot control
reaches something that matters.
"""

from dataclasses import dataclass

from .grounding import analysable_contracts, price_reads

@dataclass(frozen=True)
class OracleSignal:
    contract: str
    function: str
    kind: str
    confidence: float
    evidence: list[str]
    heuristic: bool = False
    line: int = 0
    freshness_checked: bool = False

def detect_oracle_signals(contracts):
    result = []
    for c in analysable_contracts(contracts):
        for read in price_reads(c):
            # An inert read cannot break anything downstream. Saying so is
            # cheaper than a signal a reader has to dismiss by hand.
            if read.effect == "none":
                continue
            # A spot source can be moved inside the transaction that reads it;
            # a feed cannot. An observed freshness guard lowers it either way.
            confidence = .74 if read.source == "spot" else .62
            if read.freshness_checked:
                confidence -= .18
            evidence = [
                f"{read.source} price read `{read.receiver}.{read.callee}` "
                f"at line {read.line}",
                f"result reaches {read.effect}",
                "freshness or deviation check observed on this path"
                if read.freshness_checked
                else "no freshness or deviation check observed on this path",
            ]
            result.append(OracleSignal(
                c.name, read.function, f"{read.source}-read",
                round(confidence, 2), evidence, False, read.line,
                read.freshness_checked,
            ))
    return result
