"""Order-sensitivity engine: detect when function ordering changes security state.

For a sequence A → B → C sharing causal state, test permutations (B → A → C,
etc.) and report when the final symbolic state differs. This finds the class of
bug where the same operations in a different order produce a different security
outcome — e.g. detachAuthority → transfer vs transfer → detachAuthority.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..symbolic import SymbolicEngine


@dataclass(frozen=True)
class OrderSensitiveResult:
    """Two orderings of the same functions produce different final state."""
    sequence_a: tuple[str, ...]
    sequence_b: tuple[str, ...]
    differing_state: tuple[str, ...]
    affected_storage: dict[str, tuple[str, str]]
    confidence: float
    replay_a: tuple[str, ...]
    replay_b: tuple[str, ...]


def _execute_sequence(engine: SymbolicEngine, sequence: list[str]):
    try:
        return engine.execute_sequence(sequence)
    except (RecursionError, Exception):
        return None


def _state_diff(effect_a, effect_b) -> dict[str, tuple[str, str]]:
    """Compare the final symbolic state of two sequence effects."""
    if effect_a is None or effect_b is None:
        return {}

    diffs: dict[str, tuple[str, str]] = {}
    all_keys = set(effect_a.deltas) | set(effect_b.deltas)
    for key in sorted(all_keys):
        delta_a = effect_a.deltas.get(key, "0")
        delta_b = effect_b.deltas.get(key, "0")
        if delta_a != delta_b:
            diffs[key] = (delta_a, delta_b)
    return diffs


def detect_order_sensitivity(
    contracts,
    state_graph,
    engine: SymbolicEngine | None = None,
    max_sequences: int = 100,
    max_permutations: int = 6,
) -> list[OrderSensitiveResult]:
    """Find sequences where reordering changes the final state."""
    engine = engine or SymbolicEngine(contracts)
    results: list[OrderSensitiveResult] = []
    seen: set[frozenset[str]] = set()

    # Only test sequences with shared causal state.
    edges = state_graph.causal_edges
    functions_with_shared_state: list[tuple[str, ...]] = []

    for edge in edges[:max_sequences]:
        pair = (edge.source, edge.target)
        key = frozenset(pair)
        if key in seen:
            continue
        seen.add(key)
        functions_with_shared_state.append(pair)

    # For 3-step chains, collect from candidate_sequences.
    outgoing: dict[str, list[str]] = {}
    for edge in edges:
        outgoing.setdefault(edge.source, []).append(edge.target)

    for source, targets in outgoing.items():
        for t1 in targets:
            for t2 in outgoing.get(t1, []):
                if t2 != source:
                    triple = (source, t1, t2)
                    key = frozenset(triple)
                    if key not in seen:
                        seen.add(key)
                        functions_with_shared_state.append(triple)

    tested = 0
    for sequence in functions_with_shared_state:
        if tested >= max_sequences:
            break

        # Execute the original order.
        original = list(sequence)
        original_effect = _execute_sequence(engine, original)
        if original_effect is None:
            continue

        # Test permutations (limit to avoid explosion).
        perms = list(itertools.permutations(sequence))
        # Remove the original ordering.
        perms = [p for p in perms if list(p) != original]
        perms = perms[:max_permutations]

        for perm in perms:
            alt = list(perm)
            alt_effect = _execute_sequence(engine, alt)
            if alt_effect is None:
                continue

            diffs = _state_diff(original_effect, alt_effect)
            if not diffs:
                continue

            confidence = min(0.85, 0.55 + 0.08 * len(diffs))
            if original_effect.branch_dependent or alt_effect.branch_dependent:
                confidence = min(confidence, 0.70)

            results.append(OrderSensitiveResult(
                sequence_a=tuple(original),
                sequence_b=tuple(alt),
                differing_state=tuple(sorted(diffs.keys())),
                affected_storage=diffs,
                confidence=round(confidence, 3),
                replay_a=tuple(original),
                replay_b=tuple(alt),
            ))

        tested += 1

    results.sort(key=lambda r: (-r.confidence, -len(r.differing_state)))
    return results[:200]
