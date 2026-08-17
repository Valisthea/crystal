"""DeFi protocol pack: campaigns for lending, AMM, staking, and vault patterns."""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    CampaignDefinition(
        campaign_id="defi-oracle-settlement",
        name="Oracle / Settlement Mismatch",
        description="Detect price changes between quote and settlement",
        pack="defi",
        scope=CampaignScope(
            allowed_categories=["balance", "storage"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.BALANCE_TRANSFER,
                description="oracle-dependent value operation",
                priority=0.76,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="amount_authorized >= amount_required at settlement",
                category="economic",
                affected_state=["price", "oracle", "rate"],
            ),
            CampaignInvariant(
                statement="amount_pulled <= amount_required + allowed_fee",
                category="economic",
                affected_state=["balance", "allowance"],
            ),
        ],
        questions=[
            "Can oracle state change between quote and settlement?",
            "Does every component agree on accepted payment token?",
            "Can more tokens be pulled than required?",
        ],
        priority=0.76,
    ),
    CampaignDefinition(
        campaign_id="defi-share-inflation",
        name="Share Inflation / First Depositor",
        description="Detect donation-path share inflation attacks",
        pack="defi",
        scope=CampaignScope(
            allowed_categories=["balance"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.BALANCE_TRANSFER,
                description="deposit/mint asymmetry",
                priority=0.80,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="share issuance must not be skewable by early depositor",
                category="share-accounting",
                affected_state=["totalSupply", "totalAssets", "shares"],
            ),
        ],
        priority=0.80,
    ),
    CampaignDefinition(
        campaign_id="defi-permit-funding",
        name="Permit / Funding / Settlement",
        description="Detect permit-amount vs settlement-need mismatch",
        pack="defi",
        scope=CampaignScope(
            allowed_categories=["balance"],
            max_sequence_length=4,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.APPROVAL_ACTION,
                description="approval/permit followed by settlement",
                priority=0.72,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="permit amount must not exceed settlement need",
                category="economic",
                affected_state=["allowance", "balance"],
            ),
        ],
        questions=[
            "Can permit amount exceed settlement need?",
            "Can balance/quote mismatch create extraction?",
        ],
        priority=0.72,
    ),
]
