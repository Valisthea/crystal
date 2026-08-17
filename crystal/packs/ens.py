"""ENS preset: campaign definitions for the ENS protocol family.

This pack is activated explicitly — it is not loaded by the generic discovery.
It defines 7 campaigns covering the ENS competition surface:

    A1  Migration × Fuse × Roles
    A2  Transfer × Resolver × Roles
    A3  HCA × Session × Nonce
    B1  Registration × Payment × Oracle
    B2  Commit × Reveal × Price
    B3  Permit × Funding × Settlement
    B4  Expiry × Premium

These campaigns use generic concepts (owner, resolver, nonce, expiry) — they
do NOT hardcode ENS contract names. The scope is defined by the caller.
"""

from crystal.campaigns.definition import (
    CampaignDefinition,
    CampaignInvariant,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
)

CAMPAIGNS = [
    # ── A1: Migration × Fuse × Roles ────────────────────────────────────
    CampaignDefinition(
        campaign_id="ens-A1-migration-fuse-roles",
        name="Migration × Fuse × Roles",
        description="Detect security restriction loss during V1→V2 migration",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["ownership", "role", "registry"],
            max_sequence_length=4,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.MIGRATION_ACTION,
                description="migration followed by role-gated action",
                priority=0.82,
            ),
            CampaignTransition(
                kind=TransitionKind.ROLE_ACTION,
                description="fuse/role change enabling forbidden operation",
                priority=0.80,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="migration must preserve security restrictions "
                          "(fuses, permissions)",
                category="migration",
                affected_state=["owner", "fuses", "role", "resolver", "expiry"],
                policy="exact",
            ),
            CampaignInvariant(
                statement="a forbidden V1 capability must not become an "
                          "allowed V2 capability",
                category="migration",
                affected_state=["fuses", "role", "permissions"],
                policy="forbidden-extra",
            ),
            CampaignInvariant(
                statement="stale permissions must be invalidated after migration",
                category="migration",
                affected_state=["role", "approved", "operator"],
            ),
        ],
        questions=[
            "Does migration preserve security restrictions?",
            "Can dangerous roles appear unexpectedly?",
            "Can a forbidden V1 capability become an allowed V2 capability?",
            "Are stale permissions invalidated?",
        ],
        priority=0.85,
    ),

    # ── A2: Transfer × Resolver × Roles ─────────────────────────────────
    CampaignDefinition(
        campaign_id="ens-A2-transfer-resolver-roles",
        name="Transfer × Resolver × Roles",
        description="Detect stale authority after name transfer",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["ownership", "registry", "role"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.OWNERSHIP_ACTION,
                description="ownership transfer followed by resolver action",
                priority=0.82,
            ),
            CampaignTransition(
                kind=TransitionKind.REGISTRY_ACTION,
                description="resolver manipulation after ownership change",
                priority=0.78,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="previous owner must not retain authority after "
                          "transfer",
                category="ownership",
                affected_state=["owner", "approved", "operator"],
            ),
            CampaignInvariant(
                statement="resolver state must follow intended ownership",
                category="registry",
                affected_state=["resolver", "records"],
            ),
            CampaignInvariant(
                statement="role regeneration must happen correctly after "
                          "transfer",
                category="role",
                affected_state=["role", "operator"],
            ),
        ],
        questions=[
            "Can previous owner retain authority?",
            "Does resolver state follow intended ownership?",
            "Does role regeneration happen correctly?",
            "Does approval create unexpected authority?",
        ],
        priority=0.83,
    ),

    # ── A3: HCA × Session × Nonce ───────────────────────────────────────
    CampaignDefinition(
        campaign_id="ens-A3-hca-session-nonce",
        name="HCA × Session × Nonce",
        description="Detect stale authorization replay via session/nonce "
                    "manipulation",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["nonce", "temporal", "ownership"],
            max_sequence_length=4,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.NONCE_AUTH,
                description="nonce/session invalidation followed by replay "
                            "attempt",
                priority=0.80,
            ),
            CampaignTransition(
                kind=TransitionKind.TEMPORAL_ACTION,
                description="validUntil boundary with action",
                priority=0.76,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="stale authorization must not replay after nonce "
                          "increment",
                category="nonce",
                affected_state=["nonce", "session", "authorization"],
            ),
            CampaignInvariant(
                statement="validUntil must correctly enforce at boundary",
                category="temporal",
                affected_state=["validUntil", "expiry"],
            ),
            CampaignInvariant(
                statement="calldata mutation must not bypass policy",
                category="authorization",
                affected_state=["calldata", "selector"],
            ),
        ],
        questions=[
            "Can stale authorization replay?",
            "Does nonce invalidation work?",
            "Does validUntil work at boundaries?",
            "Can calldata mutation bypass policy?",
        ],
        priority=0.80,
    ),

    # ── B1: Registration × Payment × Oracle ─────────────────────────────
    CampaignDefinition(
        campaign_id="ens-B1-registration-payment-oracle",
        name="Registration × Payment × Oracle",
        description="Detect payment token and oracle disagreements in "
                    "registration flow",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["balance", "storage"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.BALANCE_TRANSFER,
                description="registration payment with oracle dependency",
                priority=0.76,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="every component must agree on accepted payment "
                          "token",
                category="economic",
                affected_state=["paymentToken", "token", "price"],
            ),
            CampaignInvariant(
                statement="oracle state must not change between quote and "
                          "settlement",
                category="economic",
                affected_state=["price", "oracle", "rate"],
            ),
        ],
        questions=[
            "Does every component agree on accepted payment token?",
            "Can oracle state change between quote and settlement?",
            "Can registrar and validator disagree?",
        ],
        priority=0.76,
    ),

    # ── B2: Commit × Reveal × Price ─────────────────────────────────────
    CampaignDefinition(
        campaign_id="ens-B2-commit-reveal-price",
        name="Commit × Reveal × Price",
        description="Detect economic state changes between commit and reveal",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["balance", "nonce", "temporal"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.BALANCE_TRANSFER,
                description="price-dependent operation between commit/reveal",
                priority=0.74,
            ),
            CampaignTransition(
                kind=TransitionKind.TEMPORAL_ACTION,
                description="timing boundary in commit/reveal window",
                priority=0.72,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="economic state must not change between commit "
                          "and reveal",
                category="economic",
                affected_state=["price", "commitment", "secret"],
            ),
            CampaignInvariant(
                statement="boundaries must be correct in commit/reveal window",
                category="temporal",
                affected_state=["commitTime", "revealDeadline"],
            ),
        ],
        questions=[
            "Can economic state change between commit and reveal?",
            "Are boundaries correct?",
            "Can parameters differ between commit and reveal?",
        ],
        priority=0.74,
    ),

    # ── B3: Permit × Funding × Settlement ───────────────────────────────
    CampaignDefinition(
        campaign_id="ens-B3-permit-funding-settlement",
        name="Permit × Funding × Settlement",
        description="Detect permit/funding amount exceeding settlement need",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["balance"],
            max_sequence_length=4,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.APPROVAL_ACTION,
                description="permit/funding followed by settlement",
                priority=0.74,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="permit amount must not exceed settlement need",
                category="economic",
                affected_state=["allowance", "amount", "fee"],
            ),
            CampaignInvariant(
                statement="pulled tokens must not exceed required amount",
                category="economic",
                affected_state=["balance", "pulled", "required"],
            ),
        ],
        questions=[
            "Can more tokens be pulled than required?",
            "Can permit amount exceed settlement need?",
            "Can balance/quote mismatch create extraction?",
        ],
        priority=0.74,
    ),

    # ── B4: Expiry × Premium ────────────────────────────────────────────
    CampaignDefinition(
        campaign_id="ens-B4-expiry-premium",
        name="Expiry × Premium",
        description="Detect off-by-one in expiry boundaries and premium "
                    "calculation inconsistencies",
        pack="ens",
        scope=CampaignScope(
            allowed_categories=["temporal", "balance"],
            max_sequence_length=3,
            max_candidates=10,
        ),
        transitions=[
            CampaignTransition(
                kind=TransitionKind.TEMPORAL_ACTION,
                description="expiry boundary with premium calculation",
                priority=0.72,
            ),
        ],
        invariants=[
            CampaignInvariant(
                statement="expiry boundaries must be correct (x-1, x, x+1)",
                category="temporal",
                affected_state=["expiry", "premium"],
            ),
            CampaignInvariant(
                statement="premium must be calculated from the same state "
                          "used for settlement",
                category="economic",
                affected_state=["premium", "price", "settlement"],
            ),
        ],
        questions=[
            "Are expiry boundaries correct?",
            "Is premium calculated from the same state used for settlement?",
            "Can old owner/new buyer obtain an unintended state?",
        ],
        priority=0.72,
    ),
]
