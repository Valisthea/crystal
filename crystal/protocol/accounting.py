"""Ledger relations, taken from how state variables move rather than from their names.

The previous version grouped state variables by name family — `totalAssets`
with `totalSupply`, `debt` with `assets` — and asserted a relation between
them. That is a guess about a protocol dressed as a finding: it holds for the
vaults the table was written from and says nothing anywhere else, while a
protocol that names its two totals something unforeseen gets no relation at
all.

Two totals that always move in the same direction, in the same function, are a
ledger. That is visible in the writes, it is the reason the invariant holds,
and it is what a fuzzer would have to break. These pairs feed
`backends.base.derive_properties`, so grounding them is what decides whether
the property handed to Medusa or Halmos was earned or assumed.
"""

from dataclasses import dataclass

from .grounding import analysable_contracts, co_movements

@dataclass(frozen=True)
class AccountingRelation:
    left: str
    right: str
    relation: str
    confidence: float
    evidence: list[str]

def infer_accounting(contracts):
    out = []
    for c in analysable_contracts(contracts):
        for left, right, writes in co_movements(c):
            functions = sorted({w.function for w in writes})
            # One function moving two totals together is a coincidence a
            # reviewer can dismiss in a second. Several is a convention the
            # protocol is keeping on purpose, and worth stating as one.
            confidence = .58 if len(functions) == 1 else min(.84, .58 + .13 * len(functions))
            out.append(AccountingRelation(
                f"{c.name}.{left}",
                f"{c.name}.{right}",
                "written in the same direction on every observed path; "
                "a path that moves one without the other breaks the ledger",
                round(confidence, 2),
                [
                    f"{w.function} line {w.line}: {w.variable} {w.operator}"
                    for w in sorted(writes, key=lambda w: (w.function, w.line))[:8]
                ],
            ))
    return out
