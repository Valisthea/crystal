"""Generic campaigns applicable to any smart contract protocol."""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    CampaignDefinition(
        campaign_id="generic-ownership-transition",
        name="Ownership Transition",
        description="Detect stale authority after ownership changes",
        pack="generic",
        scope=CampaignScope(
            allowed_categories=["ownership", "role"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.OWNERSHIP_ACTION,
                description="ownership transfer followed by privileged action",
                priority=0.8,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="After ownership transfer, previous owner must not "
                          "retain privileged authority",
                category="ownership",
                affected_state=["owner", "admin", "operator"],
                policy="exact",
            ),
        ],
        questions=[
            "Can the previous owner retain authority after transfer?",
            "Does role regeneration happen correctly?",
            "Does approval create unexpected authority?",
        ],
        priority=0.8,
    ),
    CampaignDefinition(
        campaign_id="generic-role-transition",
        name="Role Transition",
        description="Detect permission changes that leave stale access",
        pack="generic",
        scope=CampaignScope(
            allowed_categories=["role", "ownership"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.ROLE_ACTION,
                description="role change followed by privileged action",
                priority=0.75,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="A role revocation must invalidate stale authority",
                category="role",
                affected_state=["role", "roles", "hasRole"],
            ),
        ],
        questions=[
            "Does a role change invalidate existing permissions?",
            "Can a revoked role still access restricted functions?",
        ],
        priority=0.75,
    ),
    CampaignDefinition(
        campaign_id="generic-nonce-replay",
        name="Nonce / Replay Protection",
        description="Detect replay attacks via nonce manipulation",
        pack="generic",
        scope=CampaignScope(
            allowed_categories=["nonce"],
            max_sequence_length=4,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.NONCE_AUTH,
                description="nonce invalidation followed by authorised action",
                priority=0.74,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="After nonce invalidation, previously authorized "
                          "session must not execute",
                category="nonce",
                affected_state=["nonce", "nonces"],
            ),
        ],
        questions=[
            "Can a stale authorization replay?",
            "Does nonce invalidation work?",
        ],
        priority=0.74,
    ),
    CampaignDefinition(
        campaign_id="generic-temporal-boundary",
        name="Temporal Boundary",
        description="Detect off-by-one in expiry/deadline enforcement",
        pack="generic",
        scope=CampaignScope(
            allowed_categories=["temporal"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.TEMPORAL_ACTION,
                description="expiry/deadline boundary with action",
                priority=0.72,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="An expired authorization must not execute",
                category="temporal",
                affected_state=["expiry", "deadline", "validUntil"],
            ),
        ],
        questions=[
            "Does validUntil work at boundaries (x-1, x, x+1)?",
            "Are expiry comparisons strict or non-strict?",
        ],
        priority=0.72,
    ),
    CampaignDefinition(
        campaign_id="generic-balance-accounting",
        name="Balance / Accounting Integrity",
        description="Detect accounting asymmetries in value operations",
        pack="generic",
        scope=CampaignScope(
            allowed_categories=["balance"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.BALANCE_TRANSFER,
                description="value operation with potential asymmetry",
                priority=0.70,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="Aggregate user balances should remain bounded by "
                          "protocol totals",
                category="conservation",
                affected_state=["balance", "balances", "totalSupply"],
            ),
        ],
        questions=[
            "Do deposit and withdraw conserve total value?",
            "Can a rounding path create extractable surplus?",
        ],
        priority=0.70,
    ),
]
