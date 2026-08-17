"""Campaign runner: executes a scoped analysis against the research pipeline.

The runner does NOT replace the existing research pipeline. It wraps it:
filter contracts by scope, generate sequences within limits, score, deduplicate,
and return top-K candidates per campaign.
"""

from __future__ import annotations

from ..quality.normalize import stable_id
from .definition import CampaignDefinition
from .result import CampaignCandidate, CampaignResult
from .scoring import score_candidate


def run_campaign(
    campaign: CampaignDefinition,
    result: dict,
) -> CampaignResult:
    """Run a single campaign against a completed research result."""
    scope = campaign.scope
    contracts = result.get("contracts", [])
    state_deltas = result.get("state_deltas", [])
    differential_candidates = result.get("differential_candidates", [])
    novel_behaviors = result.get("novel_behaviors", [])
    state_graph = result.get("state_graph")
    detectors = result.get("detectors", [])

    # Filter contracts by scope.
    scoped_contracts = [
        c for c in contracts if scope.accepts_contract(c.name)
    ]
    scoped_names = {c.name for c in scoped_contracts}
    if not scoped_names and scope.allowed_contracts:
        return CampaignResult(
            campaign_id=campaign.campaign_id,
            campaign_name=campaign.name,
            warning="no contracts matched the campaign scope",
        )

    candidates: list[CampaignCandidate] = []
    seen_sequences: set[tuple[str, ...]] = set()
    explored = 0
    pruned = 0

    # Mine candidates from state deltas.
    for delta in state_deltas:
        seq = tuple(delta.sequence)
        explored += 1

        if len(seq) > scope.max_sequence_length:
            pruned += 1
            continue

        fn_contracts = {s.split(".")[0] for s in seq if "." in s}
        if scoped_names and not fn_contracts & scoped_names:
            pruned += 1
            continue

        if seq in seen_sequences:
            pruned += 1
            continue
        seen_sequences.add(seq)

        if not delta.changed:
            continue

        # Match against campaign transitions.
        edge_kinds: list[str] = []
        categories: list[str] = []
        if state_graph:
            for edge in state_graph.causal_edges:
                if edge.source in seq and edge.target in seq:
                    edge_kinds.append(edge.edge_kind)
                    categories.extend(edge.categories)

        if campaign.transitions and not any(
            campaign.matches_transition(ek) for ek in edge_kinds
        ):
            if edge_kinds:
                pruned += 1
                continue

        # Find matching novelty.
        novelty = 0.0
        for nb in novel_behaviors:
            if tuple(nb.sequence) == seq:
                score = nb.novelty
                if hasattr(score, "overall"):
                    score = score.overall
                novelty = max(novelty, float(score))
                break

        # Find matching detector signals.
        relevant_signals = []
        for sig in detectors:
            qualified = f"{sig.contract}.{sig.function}"
            if qualified in seq:
                relevant_signals.append(sig)

        # Score.
        components = score_candidate(
            sequence=seq,
            changed_state=delta.changed,
            categories=tuple(sorted(set(categories))),
            edge_kinds=tuple(sorted(set(edge_kinds))),
            novelty=novelty,
            confidence=delta.confidence,
            validation_status="SYMBOLIC_ONLY",
        )

        # Build evidence.
        evidence = []
        for key, val in sorted(delta.delta.items()):
            if val != "0":
                evidence.append(f"delta({key}) = {val}")
        for sig in relevant_signals:
            evidence.append(f"detector:{sig.detector} on {sig.function} "
                            f"(conf {sig.confidence})")
        for inv in campaign.invariants:
            for state in inv.affected_state:
                if state in delta.changed or any(
                    state.lower() in c.lower() for c in delta.changed
                ):
                    evidence.append(f"invariant-candidate: {inv.statement}")
                    break

        # Hypothesis text.
        hypothesis = _build_hypothesis(campaign, delta, edge_kinds, categories)

        candidates.append(CampaignCandidate(
            candidate_id=stable_id(campaign.campaign_id, *seq),
            campaign_id=campaign.campaign_id,
            category=categories[0] if categories else "storage",
            target_contract=fn_contracts.pop() if fn_contracts else "",
            target_functions=seq,
            hypothesis=hypothesis,
            state_sequence=seq,
            state_before=delta.before,
            state_after=delta.after,
            state_delta=delta.delta,
            causal_chain=tuple(edge_kinds),
            confidence=delta.confidence,
            novelty_score=novelty,
            evidence=tuple(evidence),
            suggested_next_action="Arcadia: investigate exploitability",
            score=components.final,
        ))

    # Sort and limit.
    candidates.sort(key=lambda c: -c.score)
    top = candidates[:scope.max_candidates]
    deferred = [
        f"{c.candidate_id}: {' -> '.join(c.state_sequence)}"
        for c in candidates[scope.max_candidates:]
    ]

    # Deduplicate by invariant + delta shape.
    top = _deduplicate(top)

    return CampaignResult(
        campaign_id=campaign.campaign_id,
        campaign_name=campaign.name,
        candidates=top,
        deferred=deferred,
        total_sequences_explored=explored,
        total_sequences_pruned=pruned,
    )


def run_campaigns(
    campaigns: list[CampaignDefinition],
    result: dict,
    top_global: int = 3,
) -> list[CampaignResult]:
    """Run multiple campaigns and produce a global top-K."""
    campaign_results = []
    for campaign in campaigns:
        if not campaign.enabled:
            continue
        campaign_results.append(run_campaign(campaign, result))

    # Global top-K across campaigns.
    all_candidates = []
    for cr in campaign_results:
        all_candidates.extend(cr.candidates)
    all_candidates.sort(key=lambda c: -c.score)

    return campaign_results


def _build_hypothesis(campaign, delta, edge_kinds, categories) -> str:
    parts = []
    if "ownership-action" in edge_kinds:
        parts.append("ownership transition may leave stale authority")
    elif "role-action" in edge_kinds:
        parts.append("role change may not invalidate prior permissions")
    elif "nonce-auth" in edge_kinds:
        parts.append("nonce/session state may allow replay")
    elif "temporal-action" in edge_kinds:
        parts.append("temporal boundary may be incorrectly enforced")
    elif "balance-transfer" in edge_kinds:
        parts.append("value transfer may create accounting asymmetry")
    else:
        parts.append("state transition may violate expected invariant")

    if delta.changed:
        parts.append(f"affected state: {', '.join(delta.changed[:5])}")

    return "; ".join(parts)


def _deduplicate(candidates: list[CampaignCandidate]) -> list[CampaignCandidate]:
    """Group candidates that violate the same invariant with the same delta shape."""
    groups: dict[str, CampaignCandidate] = {}
    for candidate in candidates:
        delta_shape = tuple(sorted(
            (k, "+" if not v.startswith("-") else "-")
            for k, v in candidate.state_delta.items()
            if v != "0"
        ))
        key = (candidate.invariant_candidate, delta_shape)
        group_key = stable_id(*[str(x) for x in key])
        if group_key not in groups or candidate.score > groups[group_key].score:
            groups[group_key] = candidate
    return sorted(groups.values(), key=lambda c: -c.score)
