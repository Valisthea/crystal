"""A guard in one pipeline stage that a value-moving stage never consults.

Signal 2 of the F5 benchmark. Neither module is wrong on its own, which is why
reading them separately finds nothing:

    TxExtension = ( .., ReversibleTransactionExtension, .., ChargeTransactionPayment, .. )
                       index 7: rejects calls for protected accounts
                                                    index 9: debits the signer

Both run on every transaction. The guard covers the call it inspects; the
payment stage moves value on a different path and never asks. What the guard
protects can therefore be drained through the stage that does not consult it.
"""

from __future__ import annotations

from ..semantics.modules import build_module_graph
from .base import DetectorSignal, signal

DETECTOR = "pipeline-guard-bypass"

FALSIFICATION = (
    "Does the guarded stage also cover the value path, through a check the "
    "parser could not resolve (a trait default, a macro, a config hook)?",
    "Is the value the second stage moves bounded by something the guard "
    "already constrains?",
    "Does ordering save it — can the guard reject before the value stage runs, "
    "for every reachable input?",
    "Is the value stage reachable at all on a transaction the guard rejects?",
    "Is the amount so small that bypassing the guard has no economic meaning?",
)

REFERENCES = ("CWE-284", "CWE-693", "composition-bypass")


class _Anchor:
    """Signals anchor on a function; a pipeline stage anchors on the type."""

    def __init__(self, stage, pipeline):
        self.contract = stage.name
        self.name = pipeline.name
        self.line = stage.line or pipeline.line
        self.path = stage.path or pipeline.path
        self.language = "rust"
        self.is_entry_point = True


def detect(contracts, engine=None, wirings=()) -> list[DetectorSignal]:
    graph = build_module_graph(contracts, wirings)
    out: list[DetectorSignal] = []

    for pipeline in graph.pipelines:
        guards = pipeline.guards
        movers = pipeline.movers
        if not guards or not movers:
            continue

        for mover in movers:
            if mover.is_guard:
                continue
            unconsulted = [
                guard for guard in guards
                if not (set(guard.consulted_guards) & set(mover.consulted_guards))
            ]
            if not unconsulted:
                continue
            guard = unconsulted[0]

            confidence = 0.58
            evidence = [
                f"`{pipeline.name}` composes {len(pipeline.stages)} stages that all "
                f"run on every transaction, in declared order",
                f"stage {guard.index} `{guard.name}` rejects on a restriction check: "
                + (guard.guard_evidence[0] if guard.guard_evidence else "n/a"),
                f"stage {mover.index} `{mover.name}` moves value: "
                + (mover.value_evidence[0] if mover.value_evidence else "n/a"),
                f"`{mover.name}` consults none of the checks "
                f"`{guard.name}` enforces ({', '.join(guard.consulted_guards) or 'n/a'})",
            ]
            if mover.index > guard.index:
                confidence += 0.10
                evidence.append(
                    f"the value stage runs after the guard (index {mover.index} > "
                    f"{guard.index}), so the guard has already passed on a "
                    f"different question by the time value moves"
                )
            if len(mover.value_evidence) > 1:
                confidence += 0.04
                evidence.append(
                    f"{len(mover.value_evidence)} value-bearing operations in "
                    f"`{mover.name}`"
                )
            if not all(stage.resolved for stage in pipeline.stages):
                unresolved = [s.name for s in pipeline.stages if not s.resolved]
                evidence.append(
                    "stages not resolved to parsed code (their behaviour is "
                    "unknown to Crystal): " + ", ".join(unresolved[:8])
                )

            out.append(signal(
                DETECTOR,
                f"`{mover.name}` moves value without the check `{guard.name}` "
                f"enforces in `{pipeline.name}`",
                _Anchor(mover, pipeline), confidence,
                "two stages of the same composition disagree about what is "
                "allowed: one rejects restricted callers, the other moves value "
                "without asking, so the restriction is bypassable through the "
                "second stage",
                evidence=evidence,
                ordered_trace=tuple(
                    f"[{stage.index}] {stage.name}"
                    + (" GUARD" if stage.is_guard else "")
                    + (" MOVES-VALUE" if stage.moves_value else "")
                    + ("" if stage.resolved else " (unresolved)")
                    for stage in pipeline.stages
                ),
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
