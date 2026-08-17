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
                .62,
                [x.evidence]
            ))

    for x in model.oracle_signals:
        out.append(ProtocolInvariant(
            "oracle",
            f"{x.contract}.{x.function} should consume a bounded/fresh price signal",
            .55,
            x.evidence
        ))

    for x in model.fee_signals:
        out.append(ProtocolInvariant(
            "fees",
            f"{x.contract}.{x.function} should not create unaccounted fee value",
            .58,
            x.evidence
        ))

    return out
