"""Campaign results: candidates, state transitions, and evidence."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class CampaignCandidate:
    """A single research candidate produced by a campaign run."""
    candidate_id: str
    campaign_id: str
    category: str
    target_contract: str
    target_functions: tuple[str, ...]
    hypothesis: str
    state_sequence: tuple[str, ...]
    state_before: dict[str, str] = field(default_factory=dict)
    state_after: dict[str, str] = field(default_factory=dict)
    state_delta: dict[str, str] = field(default_factory=dict)
    causal_chain: tuple[str, ...] = ()
    invariant_candidate: str = ""
    order_sensitivity: str = ""
    differential_result: str = ""
    confidence: float = 0.5
    novelty_score: float = 0.0
    impact_surface: str = ""
    exploitability_hint: str = ""
    validation_status: str = "SYMBOLIC_ONLY"
    evidence: tuple[str, ...] = ()
    reproduction: tuple[str, ...] = ()
    suggested_next_action: str = ""
    score: float = 0.0


@dataclass
class CampaignResult:
    """Result of running a single campaign."""
    campaign_id: str
    campaign_name: str
    candidates: list[CampaignCandidate] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    total_sequences_explored: int = 0
    total_sequences_pruned: int = 0
    warning: str = ""

    @property
    def top_candidates(self) -> list[CampaignCandidate]:
        return sorted(self.candidates, key=lambda c: -c.score)[:10]
