"""The canonical research proposal, and the one place candidates are built.

Crystal produces proposals from three engines — the core hypothesis pass, the
sequence engine, and the protocol-semantics layer. `ResearchCandidate` is where
they converge, and it is the record a parent system reads.

Until Build 019 it was also the thinnest record in the pipeline. Detector
signals carry a `FALSIFICATION` tuple, `EvidenceRecord` carries `limitations`
and `provenance`, and `models.Hypothesis` carries a `rationale` — the candidate
carried none of them. The rationale was worse than absent: it was computed and
then dropped on the floor by the constructor below.

Two rules govern the new fields, and they are the same rule twice:

* **Nothing is invented.** A producer that supplies no falsification leaves the
  field empty and is named in `missing`. A generic sentence in that slot would
  be worse than the gap, because a reader would stop looking for the real one.
* **Falsification is cited, never written here.** Where a detector already
  reasoned about how its signal could be wrong, the candidate references that
  signal. Composing a new falsification at this layer would be protocol
  interpretation, which Crystal does not do.

Identity is deliberately unchanged. `stable_id` still reads exactly
`(category, title, evidence, path)`, so every candidate id that existed before
this build still exists after it. Adding a field to the identity material would
have silently reissued every id in the corpus.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import stable_id

# Fields a parent system is entitled to expect, and which Crystal reports as
# absent rather than filling in. Named so `missing` cannot drift from reality.
EXPECTED = ("rationale", "falsification", "assumptions", "limitations")


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
    # Build 019. Every one defaults empty: a candidate built by an older caller
    # is still a valid candidate, and reads as one that supplied nothing here.
    rationale: str = ""
    falsification: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    # Which producer, and on what. Never inferred from the current tree.
    provenance: dict = field(default_factory=dict)
    # The subset of EXPECTED this candidate genuinely has nothing for.
    missing: list[str] = field(default_factory=list)

    @property
    def falsifiable(self) -> bool:
        """Whether a reader is told how this could be shown wrong."""
        return bool(self.falsification)


def _missing_fields(rationale, falsification, assumptions, limitations) -> list[str]:
    present = {
        "rationale": bool(rationale),
        "falsification": bool(falsification),
        "assumptions": bool(assumptions),
        "limitations": bool(limitations),
    }
    return [name for name in EXPECTED if not present[name]]


def _candidate(category, title, confidence, evidence, path, source, *,
               rationale="", falsification=(), assumptions=(), limitations=(),
               provenance=None):
    falsification = list(falsification)
    assumptions = list(assumptions)
    limitations = list(limitations)
    return ResearchCandidate(
        # Identity material unchanged since before Build 019, on purpose.
        stable_id(category, title, *evidence, *path),
        category,
        title,
        round(max(0.0, min(1.0, confidence)), 3),
        True,
        sorted(set(evidence)),
        list(path),
        source,
        rationale=rationale,
        falsification=falsification,
        assumptions=assumptions,
        limitations=limitations,
        provenance=dict(provenance or {}),
        missing=_missing_fields(rationale, falsification, assumptions, limitations),
    )


def _falsification_by_function(detectors) -> dict[str, tuple[str, list[str]]]:
    """Each function a detector fired on, and how that detector can be wrong.

    Cited rather than composed. The detector did the reasoning about its own
    premise; this looks it up by the function it fired on.
    """
    out: dict[str, tuple[str, list[str]]] = {}
    for signal in detectors or ():
        contract = getattr(signal, "contract", None)
        function = getattr(signal, "function", None)
        questions = list(getattr(signal, "falsification", ()) or ())
        if not (contract and function and questions):
            continue
        out.setdefault(f"{contract}.{function}",
                       (getattr(signal, "detector", "detector"), questions))
    return out


def _cited_falsification(path, by_function) -> tuple[list[str], list[str]]:
    """Falsification questions inherited from detectors on this path."""
    questions: list[str] = []
    assumptions: list[str] = []
    for name in path:
        entry = by_function.get(name)
        if entry is None:
            continue
        detector, items = entry
        assumptions.append(
            f"inherits the premise of `{detector}` on {name}"
        )
        questions.extend(
            f"[{detector} on {name}] {question}" for question in items
        )
    return questions, assumptions


def build_candidates(result):
    out = []
    by_function = _falsification_by_function(result.get("detectors", ()))
    provenance = dict(result.get("producer_provenance") or {})

    for h in result["hypotheses"]:
        questions, assumptions = _cited_falsification(h.path, by_function)
        out.append(_candidate(
            h.category, h.title, h.priority,
            h.evidence, h.path, "core-hypothesis",
            # Computed by `hypotheses.generate` and discarded here until 019.
            rationale=h.rationale,
            falsification=questions,
            assumptions=assumptions,
            provenance=provenance,
        ))

    for h in result["sequence_hypotheses"]:
        questions, assumptions = _cited_falsification(h.sequence, by_function)
        limitations = []
        if getattr(h, "depth_justification", ""):
            limitations.append(
                "sequence depth beyond the default was justified as: "
                + h.depth_justification
            )
        out.append(_candidate(
            "SEQUENCE",
            "Validate sequence: " + " -> ".join(h.sequence),
            h.score,
            [h.reason],
            h.sequence,
            "sequence-engine",
            rationale=h.reason,
            falsification=questions,
            assumptions=assumptions,
            limitations=limitations,
            provenance=provenance,
        ))

    for inv in result["protocol_invariants"]:
        out.append(_candidate(
            inv.category,
            "Test invariant: " + inv.expression,
            inv.confidence,
            inv.evidence,
            [],
            "protocol-semantics",
            # The expression states the observed relation; it is the reason
            # the invariant is proposed, so it is the rationale.
            rationale=inv.expression,
            provenance=provenance,
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
