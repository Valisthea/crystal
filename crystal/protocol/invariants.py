"""Protocol invariants, one per observed relation.

Each invariant here inherits the confidence of the relation underneath it
rather than a constant written at this layer. The constants were how a
substring match arrived at 0.55 and a signature match at 0.72: numbers chosen
to look calibrated, applied to evidence that did not vary.

An invariant's expression names the variables and the path, because the reader
who has to decide whether it is worth a fuzzing campaign needs to know what
would break it.
"""

from dataclasses import dataclass

@dataclass(frozen=True)
class ProtocolInvariant:
    category: str
    expression: str
    confidence: float
    evidence: list[str]

def derive_protocol_invariants(model):
    out = []

    for x in model.accounting_relations:
        out.append(ProtocolInvariant(
            "accounting",
            f"{x.left} ↔ {x.right}: {x.relation}",
            min(x.confidence, .85),
            x.evidence
        ))

    for x in model.value_flows:
        if x.operation in {"mint", "burn"}:
            out.append(ProtocolInvariant(
                "supply",
                f"{x.operation} should have a corresponding supply/accounting effect",
                min(x.confidence, .70),
                [x.evidence]
            ))

    for x in model.oracle_signals:
        # A guard was observed on this path, so the invariant it would state is
        # already asserted in the source. Repeating it back is not evidence.
        if x.freshness_checked:
            continue
        out.append(ProtocolInvariant(
            "oracle",
            f"{x.contract}.{x.function} consumes a {x.kind} at line {x.line} "
            f"with no freshness or deviation bound on the path",
            x.confidence,
            x.evidence
        ))

    for x in model.fee_signals:
        out.append(ProtocolInvariant(
            "value-scaling",
            f"{x.contract}.{x.function}: `{x.scalar}` scales `{x.amount}` on a "
            f"value-moving path; the scaled remainder must stay accounted for",
            x.confidence,
            x.evidence
        ))

    return out
