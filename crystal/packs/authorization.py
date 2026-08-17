"""Authorization / Session pack: nonce, session, revocation, replay protection."""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    CampaignDefinition(
        campaign_id="auth-stale-authorization",
        name="Stale Authorization",
        description="Detect replay via stale authorization after context change",
        pack="authorization",
        scope=CampaignScope(
            allowed_categories=["nonce", "temporal", "ownership"],
            max_sequence_length=4,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.NONCE_AUTH,
                description="authorize -> context change -> execute",
                priority=0.76,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="authorize -> revoke -> execute must fail",
                category="authorization",
                affected_state=["nonce", "authorization", "session"],
            ),
            CampaignInvariant(
                statement="authorize -> owner change -> execute must fail",
                category="authorization",
                affected_state=["owner", "signer", "executor"],
            ),
        ],
        questions=[
            "Can stale authorization replay?",
            "Does nonce invalidation work?",
            "Does validUntil work at boundaries?",
            "Can calldata mutation bypass policy?",
        ],
        priority=0.76,
    ),
    CampaignDefinition(
        campaign_id="auth-revocation",
        name="Revocation",
        description="Detect incomplete or bypassable revocation",
        pack="authorization",
        scope=CampaignScope(
            allowed_categories=["nonce", "role", "ownership"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.ROLE_ACTION,
                description="revocation followed by action",
                priority=0.74,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="after revocation, the revoked entity must not act",
                category="authorization",
                affected_state=["revoked", "authorized", "role"],
            ),
        ],
        priority=0.74,
    ),
]
