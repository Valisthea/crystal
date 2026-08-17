"""A pipeline stage that moves value the guarding stage does not cover.

    TxExtension = ( .., ReversibleTransactionExtension, .., ChargeTransactionPayment, .. )
                       [7] rejects protected accounts        [9] debits the signer

Both run on every transaction. The guard gates call dispatch; the payment stage
takes a fee. Neither module is wrong on its own, and the defect exists only in
the composition — which is why reading either file finds nothing.

`CheckNonce` and friends are deliberately not guards here. They reject on
protocol metadata, not on authority, and treating them as guards would make
every pipeline report a bypass.
"""

from __future__ import annotations

from ..composition import DECLARED_ONLY, build_composition
from ..composition.stage_classifier import CHECKS_ONLY
from .base import DetectorSignal, signal

DETECTOR = "pipeline-guard-bypass"

FALSIFICATION = (
    "Does the guard stage also gate this mechanism (fees, not just call "
    "dispatch), through a check the parser could not resolve?",
    "Is the debited amount bounded by runtime configuration (a `Max*` "
    "associated type, a governance parameter) outside the parsed modules?",
    "Does a later stage in the pipeline refund what this one takes?",
    "Is the value stage reachable at all on a transaction the guard rejects?",
    "Is the guard's scope actually broader than the calls it inspects?",
)

REFERENCES = ("CWE-284", "CWE-693", "composition-bypass")


class _Anchor:
    """Signals anchor on a function; a pipeline stage anchors on its type."""

    def __init__(self, stage, pipeline):
        self.contract = stage.name
        self.name = pipeline.name
        self.line = stage.line or pipeline.line
        self.path = stage.path or pipeline.path
        self.language = "rust"
        self.is_entry_point = True


def _unbounded_inputs(contracts, engine):
    """Reuse the intra-module signal: an unbounded input makes this worse."""
    from .unbounded_input import detect as detect_unbounded

    found: dict[str, list[str]] = {}
    for item in detect_unbounded(contracts, engine):
        found.setdefault(item.contract, []).append(
            f"{item.function}:{item.line} " + (item.evidence[0] if item.evidence else "")
        )
    return found


def detect(contracts, engine=None, wirings=(), bindings=(), root=".",
           sources=()) -> list[DetectorSignal]:
    unbounded = _unbounded_inputs(contracts, engine) if engine is not None else {}
    model = build_composition(contracts, wirings, bindings, root, sources, unbounded)
    out: list[DetectorSignal] = []

    for crossing in model.crossings:
        guard, mover = crossing.guard, crossing.mover
        pipeline = next(
            (p for p in model.pipelines if p.name == crossing.pipeline), None
        )
        if pipeline is None:
            continue

        checks_only = [s for s in pipeline.stages if s.role == CHECKS_ONLY]
        evidence = [
            f"`{pipeline.name}` composes {len(pipeline.stages)} stages that all run "
            f"on every transaction, in declared order",
            f"stage {guard.index} `{guard.name}` is a GUARD — {guard.scope()}: "
            + (guard.guard_evidence[0] if guard.guard_evidence else "n/a"),
            f"stage {mover.index} `{mover.name}` MOVES-VALUE — {mover.scope()}: "
            + (mover.value_evidence[0] if mover.value_evidence else "n/a"),
            f"boundary: {crossing.boundary}",
        ]
        evidence.extend(crossing.notes)
        if checks_only:
            evidence.append(
                "stages classified CHECKS-ONLY and deliberately not treated as "
                "guards (they reject on protocol metadata, not authority): "
                + ", ".join(f"{s.name}({s.index})" for s in checks_only[:8])
            )
        evidence.append(f"limits: {DECLARED_ONLY}")

        out.append(signal(
            DETECTOR,
            f"`{mover.name}` moves value through {', '.join(mover.mechanisms) or 'a value operation'} "
            f"that `{guard.name}` does not gate in `{pipeline.name}`",
            _Anchor(mover, pipeline), crossing.confidence,
            "two stages of the same composition disagree about what is allowed: "
            "one rejects restricted principals, the other moves value from the "
            "same principal through a mechanism the first does not cover",
            evidence=evidence,
            ordered_trace=tuple(pipeline.render()),
            falsification=FALSIFICATION,
            references=REFERENCES,
            line=mover.line or pipeline.line,
        ))

    merged: dict[str, DetectorSignal] = {}
    for item in out:
        previous = merged.get(item.id)
        if previous is None or item.confidence > previous.confidence:
            merged[item.id] = item
    return sorted(merged.values(),
                  key=lambda x: (-x.confidence, x.contract, x.line))
