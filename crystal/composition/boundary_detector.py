"""Does the guard actually cover what the value stage does?

The question is not "do both stages touch the same account" — in a transaction
pipeline they always do. It is whether the guard's *mechanism* covers the
mover's. A guard that gates call dispatch does not gate fees, so a fee debited
from the account it protects crosses a boundary the guard was believed to close.

Both mechanisms matching is the negative case and must stay silent: a guard on
transfers covering a transfer is the system working.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .stage_classifier import GUARD, MOVES_VALUE

# A guard whose rejection reads the dispatched call gates dispatch, and dispatch
# is not the fee path: fees are taken by the extension pipeline itself.
CALL_DISPATCH = "call-dispatch"


@dataclass(frozen=True)
class BoundaryCrossing:
    pipeline: str
    guard: object
    mover: object
    boundary: str
    guard_mechanisms: tuple[str, ...]
    mover_mechanisms: tuple[str, ...]
    shared_principals: tuple[str, ...]
    confidence: float
    covered: bool = False
    notes: tuple[str, ...] = ()


def _covers(guard, mover) -> tuple[bool, str]:
    """True when the guard's mechanism already covers the mover's."""
    guard_families = set(guard.mechanisms)
    mover_families = set(mover.mechanisms)

    if guard_families & mover_families:
        return True, (
            "the guard and the value operation act through the same mechanism "
            f"({', '.join(sorted(guard_families & mover_families))})"
        )
    if set(guard.consulted_guards) & set(mover.consulted_guards):
        return True, (
            "the value stage consults the same restriction the guard enforces "
            f"({', '.join(sorted(set(guard.consulted_guards) & set(mover.consulted_guards)))})"
        )
    return False, ""


def find_crossings(pipeline, unbounded_by_contract=None) -> list[BoundaryCrossing]:
    unbounded_by_contract = unbounded_by_contract or {}
    crossings: list[BoundaryCrossing] = []

    guards = [stage for stage in pipeline.stages if stage.role == GUARD]
    movers = [stage for stage in pipeline.stages if stage.role == MOVES_VALUE]
    if not guards or not movers:
        return crossings

    for mover in movers:
        for guard in guards:
            if guard.index == mover.index:
                continue
            covered, reason = _covers(guard, mover)
            if covered:
                continue

            shared = tuple(sorted(set(guard.principals) & set(mover.principals)))
            notes = []
            confidence = 0.58

            if mover.index > guard.index:
                confidence += 0.10
                notes.append(
                    f"the value stage runs after the guard (index {mover.index} > "
                    f"{guard.index}): by then the guard has passed on a different "
                    f"question"
                )
            if shared:
                confidence += 0.06
                notes.append(
                    "both stages act on the same principal: " + ", ".join(shared)
                )
            if mover.routes:
                confidence += 0.06
                notes.append(
                    "the debit is performed by the runtime-bound implementation: "
                    + "; ".join(mover.routes[:3])
                    + " — which does not consult the check either"
                )
            if mover.external_routes:
                notes.append(
                    "part of the path leaves the scan: "
                    + "; ".join(mover.external_routes[:3])
                )
            unbounded = unbounded_by_contract.get(mover.name)
            if unbounded:
                confidence += 0.11
                notes.append(
                    "the debited amount includes an unbounded caller input: "
                    + "; ".join(unbounded[:3])
                )
            if pipeline.unresolved:
                notes.append(
                    f"{len(pipeline.unresolved)} stage(s) not parsed, so their "
                    f"behaviour is unknown: "
                    + ", ".join(stage.name for stage in pipeline.unresolved[:8])
                )

            boundary = (
                f"stage {mover.index} debits "
                f"{', '.join(mover.principals) or 'the signer'} through "
                f"{', '.join(mover.mechanisms) or 'a value operation'}, which "
                f"stage {guard.index} does not cover: it gates "
                f"{', '.join(guard.mechanisms) or CALL_DISPATCH}"
            )

            crossings.append(BoundaryCrossing(
                pipeline.name, guard, mover, boundary,
                tuple(guard.mechanisms), tuple(mover.mechanisms), shared,
                round(min(confidence, 0.95), 3), False, tuple(notes),
            ))

    return sorted(crossings, key=lambda x: (-x.confidence, x.mover.index))
