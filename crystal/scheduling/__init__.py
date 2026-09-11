"""How Crystal spends the one budget it cannot avoid rationing.

Symbolic sequence execution is the expensive step, and `derive_state_deltas`
executes a bounded number of hypotheses. On the Lido stonks protocol that meant
150 of 198 — and the 48 that were dropped went unreported.

Measured on that target, 2026-09-11: **175 of the 198 hypotheses carry the same
score, 0.720**. The budget boundary therefore falls deep inside a tie, and what
decided between executing a sequence and discarding it was the tiebreak —
sequence length, then lexicographic order. `Stonks.constructor -> Order.initialize
-> Order.isValidSignature` was dropped because `S` sorts late, and 22 of the 48
discarded sequences already carried a detector signal on their path.

This package does not change what Crystal considers best. Scores are untouched
and remain the primary key. It replaces the arbitrary half of the ordering with
a relational one, and it records what did not run.
"""

from .demand import (
    Allocation,
    Deferred,
    Demand,
    demand_for,
    schedule_sequences,
)

__all__ = [
    "Allocation",
    "Deferred",
    "Demand",
    "demand_for",
    "schedule_sequences",
]
