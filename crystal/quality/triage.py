from dataclasses import dataclass
from .normalize import stable_id

@dataclass(frozen=True)
class ResearchCandidate:
    id: str
    category: str
    title: str
    confidence: float
    validation_required: bool
    evidence: list[str]
    path: list[str]
    source: str

def _candidate(category, title, confidence, evidence, path, source):
    return ResearchCandidate(
        stable_id(category, title, *evidence, *path),
        category,
        title,
        round(max(0.0, min(1.0, confidence)), 3),
        True,
        sorted(set(evidence)),
        list(path),
        source,
    )

def build_candidates(result):
    out = []

    for h in result["hypotheses"]:
        out.append(_candidate(
            h.category, h.title, h.priority,
            h.evidence, h.path, "core-hypothesis"
        ))

    for h in result["sequence_hypotheses"]:
        out.append(_candidate(
            "SEQUENCE",
            "Validate sequence: " + " -> ".join(h.sequence),
            h.score,
            [h.reason],
            h.sequence,
            "sequence-engine"
        ))

    for inv in result["protocol_invariants"]:
        out.append(_candidate(
            inv.category,
            "Test invariant: " + inv.expression,
            inv.confidence,
            inv.evidence,
            [],
            "protocol-semantics"
        ))

    # Deduplicate by deterministic ID and keep the strongest evidence.
    merged = {}
    for item in out:
        old = merged.get(item.id)
        if old is None or item.confidence > old.confidence:
            merged[item.id] = item

    return sorted(
        merged.values(),
        key=lambda x: (-x.confidence, x.category, x.title)
    )
