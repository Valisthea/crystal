"""Registry / Identity / Permission pack: generic campaigns for ownership,
approval, operator, role, and resolver patterns."""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    CampaignDefinition(
        campaign_id="registry-resolver-transition",
        name="Resolver Transition",
        description="Detect stale resolver after ownership transfer",
        pack="registry",
        scope=CampaignScope(
            allowed_categories=["ownership", "registry"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.REGISTRY_ACTION,
                description="ownership change followed by resolver action",
                priority=0.74,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="resolver state must follow intended ownership",
                category="registry",
                affected_state=["resolver", "owner", "records"],
            ),
        ],
        questions=[
            "Does resolver state follow intended ownership?",
            "Can previous owner retain resolver control?",
        ],
        priority=0.74,
    ),
    CampaignDefinition(
        campaign_id="registry-approval-authority",
        name="Approval Authority",
        description="Detect unexpected authority from approval/operator grants",
        pack="registry",
        scope=CampaignScope(
            allowed_categories=["ownership", "role"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.APPROVAL_ACTION,
                description="approval grant followed by privileged action",
                priority=0.72,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="approval must not create authority beyond its scope",
                category="authorization",
                affected_state=["approved", "operator", "operators"],
            ),
        ],
        priority=0.72,
    ),
]
