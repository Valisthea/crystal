"""Economic / Payment State pack: oracle, payment token, settlement, refund."""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    CampaignDefinition(
        campaign_id="economic-quote-settlement",
        name="Quote vs Settlement",
        description="Detect price/oracle changes between quote and settlement",
        pack="economic",
        scope=CampaignScope(
            allowed_categories=["balance", "storage"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.BALANCE_TRANSFER,
                description="value operation with oracle dependency",
                priority=0.76,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="settlement amount must not exceed quoted amount "
                          "plus explicitly allowed fee",
                category="economic",
                affected_state=["price", "amount", "fee"],
            ),
        ],
        questions=[
            "Can oracle state change between quote and settlement?",
            "Can registrar and validator disagree?",
        ],
        priority=0.76,
    ),
    CampaignDefinition(
        campaign_id="economic-allowance-mismatch",
        name="Allowance Mismatch",
        description="Detect allowance/permit amount exceeding actual need",
        pack="economic",
        scope=CampaignScope(
            allowed_categories=["balance"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.APPROVAL_ACTION,
                description="allowance/permit with settlement",
                priority=0.72,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="allowance must not exceed the amount required "
                          "by settlement",
                category="economic",
                affected_state=["allowance", "balance"],
            ),
        ],
        priority=0.72,
    ),
]
