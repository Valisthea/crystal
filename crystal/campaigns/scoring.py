"""Candidate scoring: novelty × state significance × causal depth × exploitability.

Penalties suppress duplicates, known behaviours, low confidence, unreachable
sequences, and cosmetic differences. The result is a single float in [0, 1].
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ScoreComponents:
    novelty: float = 0.0
    state_delta_significance: float = 0.0
    causal_depth: float = 0.0
    authorization_relevance: float = 0.0
    order_sensitivity: float = 0.0
    exploitability: float = 0.0
    penalty: float = 0.0

    @property
    def raw(self) -> float:
        return (
            self.novelty * 0.20
            + self.state_delta_significance * 0.25
            + self.causal_depth * 0.15
            + self.authorization_relevance * 0.15
            + self.order_sensitivity * 0.10
            + self.exploitability * 0.15
        )

    @property
    def final(self) -> float:
        return max(0.0, min(1.0, self.raw - self.penalty))


PRIORITY_CATEGORIES = frozenset({
    "ownership", "role", "balance", "nonce", "temporal", "proxy",
})

SIGNIFICANT_EDGE_KINDS = frozenset({
    "ownership-action", "role-action", "auth-action",
    "nonce-auth", "temporal-action",
})


def score_candidate(
    *,
    sequence: tuple[str, ...],
    changed_state: list[str],
    categories: tuple[str, ...] = (),
    edge_kinds: tuple[str, ...] = (),
    novelty: float = 0.0,
    has_order_sensitivity: bool = False,
    confidence: float = 0.5,
    is_duplicate: bool = False,
    is_known: bool = False,
    validation_status: str = "SYMBOLIC_ONLY",
) -> ScoreComponents:

    # State delta significance: more changed state with priority categories.
    state_sig = min(1.0, len(changed_state) * 0.15)
    priority_count = sum(1 for c in categories if c in PRIORITY_CATEGORIES)
    state_sig = min(1.0, state_sig + priority_count * 0.12)

    # Causal depth: longer justified chains score higher.
    depth = min(1.0, len(sequence) * 0.25)

    # Authorization relevance.
    auth = 0.0
    for ek in edge_kinds:
        if ek in SIGNIFICANT_EDGE_KINDS:
            auth = max(auth, 0.7)
    if priority_count > 0:
        auth = max(auth, 0.4)

    # Order sensitivity.
    order = 0.6 if has_order_sensitivity else 0.0

    # Exploitability estimate.
    exploit = confidence * 0.6
    if validation_status == "CONCRETE_REPRODUCED":
        exploit = min(1.0, exploit + 0.3)
    if any(c in ("balance", "ownership") for c in categories):
        exploit = min(1.0, exploit + 0.1)

    # Penalties.
    penalty = 0.0
    if is_duplicate:
        penalty += 0.5
    if is_known:
        penalty += 0.3
    if confidence < 0.4:
        penalty += 0.2

    return ScoreComponents(
        novelty=novelty,
        state_delta_significance=state_sig,
        causal_depth=depth,
        authorization_relevance=auth,
        order_sensitivity=order,
        exploitability=exploit,
        penalty=penalty,
    )
