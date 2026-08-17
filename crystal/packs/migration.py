"""Migration / State Translation pack: compare old state vs new state."""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    CampaignDefinition(
        campaign_id="migration-permission-preservation",
        name="Migration Permission Preservation",
        description="Detect permissions lost, added, or changed during migration",
        pack="migration",
        scope=CampaignScope(
            allowed_categories=["ownership", "role", "registry"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.MIGRATION_ACTION,
                description="migration followed by privileged action",
                priority=0.78,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="migration must preserve security restrictions",
                category="migration",
                affected_state=["owner", "role", "resolver", "expiry"],
                policy="exact",
            ),
            CampaignInvariant(
                statement="migration must not introduce dangerous permissions",
                category="migration",
                affected_state=["role", "authority", "operator"],
                policy="forbidden-extra",
            ),
        ],
        questions=[
            "Does migration preserve security restrictions?",
            "Can dangerous roles appear unexpectedly?",
            "Can a forbidden V1 capability become an allowed V2 capability?",
            "Are stale permissions invalidated?",
        ],
        priority=0.78,
    ),
]
