"""Sequence hypothesis generation with causal prioritisation.

Sequences are scored by the semantic category of their shared state, not just
by length. write→read of a balance scores higher than write→read of a debug
counter. The depth defaults to 3 and rises to 4-5 only when the causal chain
justifies it (multi-category edges, authorization chains).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class SequenceHypothesis:
    sequence: list[str]
    reason: str
    score: float
    categories: tuple[str, ...] = ()
    edge_kinds: tuple[str, ...] = ()
    depth_justification: str = ""

    @property
    def qualified_names(self) -> list[str]:
        return list(self.sequence)


# Categories that justify extending depth beyond 3.
DEEP_CHAIN_CATEGORIES = frozenset({
    "ownership", "role", "nonce", "temporal", "proxy",
})

# Edge kinds that are highest priority.
PRIORITY_EDGE_KINDS = {
    "ownership-action": 0.82,
    "role-action": 0.76,
    "auth-action": 0.78,
    "nonce-auth": 0.74,
    "temporal-action": 0.72,
    "balance-transfer": 0.70,
    "registry-action": 0.68,
    "write-write": 0.55,
    "write-read": 0.62,
}


def generate_sequences(state_sequences, invariant_candidates,
                        state_graph=None):
    """Generate scored sequence hypotheses from causal paths.

    When a state_graph is provided, edge metadata enriches scoring.
    Otherwise falls back to the v1 length-based scoring.
    """
    out: list[SequenceHypothesis] = []
    invariant_bonus = min(len(invariant_candidates) * 0.02, 0.10)
    seen: set[tuple[str, ...]] = set()

    # Build edge lookup from state graph if available.
    edge_meta: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {}
    if state_graph is not None:
        for edge in state_graph.causal_edges:
            key = (edge.source, edge.target)
            edge_meta[key] = (
                getattr(edge, "edge_kind", "write-read"),
                getattr(edge, "categories", ()),
            )

    for seq in state_sequences:
        seq_key = tuple(seq)
        if seq_key in seen:
            continue
        seen.add(seq_key)

        # Collect edge metadata along the sequence.
        kinds: list[str] = []
        categories: set[str] = set()
        for i in range(len(seq) - 1):
            pair = (seq[i], seq[i + 1])
            if pair in edge_meta:
                ek, cats = edge_meta[pair]
                kinds.append(ek)
                categories.update(cats)

        # Score based on edge kinds.
        if kinds:
            base_score = max(
                PRIORITY_EDGE_KINDS.get(k, 0.55) for k in kinds
            )
        elif len(seq) == 2:
            base_score = 0.62
        else:
            base_score = 0.72

        # Category bonus.
        priority_bonus = 0.04 * len(categories & DEEP_CHAIN_CATEGORIES)

        # Length-based reasoning.
        if len(seq) == 2:
            reason = "writer-to-reader state dependency"
        elif len(seq) == 3:
            reason = "multi-step state dependency chain"
        elif len(seq) >= 4:
            reason = "deep causal chain"
        else:
            reason = "state dependency"

        # Add edge kind context to reason.
        if "ownership-action" in kinds:
            reason = f"ownership change → privileged action ({reason})"
        elif "role-action" in kinds:
            reason = f"role change → privileged action ({reason})"
        elif "nonce-auth" in kinds:
            reason = f"nonce change → authorization ({reason})"
        elif "temporal-action" in kinds:
            reason = f"expiry/deadline → action ({reason})"
        elif "auth-action" in kinds:
            reason = f"authorization → action ({reason})"

        # Depth justification for long chains.
        depth_just = ""
        if len(seq) > 3:
            if categories & DEEP_CHAIN_CATEGORIES:
                depth_just = (
                    f"depth {len(seq)} justified by "
                    f"{', '.join(sorted(categories & DEEP_CHAIN_CATEGORIES))}"
                )
            else:
                depth_just = f"depth {len(seq)} from causal chain"

        final_score = round(
            min(base_score + invariant_bonus + priority_bonus, 0.95), 3
        )

        out.append(SequenceHypothesis(
            sequence=list(seq),
            reason=reason,
            score=final_score,
            categories=tuple(sorted(categories)),
            edge_kinds=tuple(kinds),
            depth_justification=depth_just,
        ))

    return sorted(out, key=lambda x: (-x.score, len(x.sequence), x.sequence))
