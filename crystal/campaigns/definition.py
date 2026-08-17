"""Campaign definitions: scope, transitions, invariants, and gating rules."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum


class TransitionKind(Enum):
    WRITE_READ = "write-read"
    WRITE_WRITE = "write-write"
    AUTH_ACTION = "auth-action"
    OWNERSHIP_ACTION = "ownership-action"
    ROLE_ACTION = "role-action"
    NONCE_AUTH = "nonce-auth"
    TEMPORAL_ACTION = "temporal-action"
    REGISTRY_ACTION = "registry-action"
    BALANCE_TRANSFER = "balance-transfer"
    APPROVAL_ACTION = "approval-action"
    MIGRATION_ACTION = "migration-action"
    UPGRADE_ACTION = "upgrade-action"


@dataclass(frozen=True)
class CampaignTransition:
    """A dangerous state transition the campaign watches for."""
    kind: TransitionKind
    description: str
    source_states: tuple[str, ...] = ()
    target_states: tuple[str, ...] = ()
    priority: float = 0.7


@dataclass
class CampaignScope:
    """What a campaign is allowed to look at."""
    allowed_contracts: list[str] = field(default_factory=list)
    allowed_functions: list[str] = field(default_factory=list)
    allowed_categories: list[str] = field(default_factory=list)
    allowed_state: list[str] = field(default_factory=list)
    max_sequence_length: int = 3
    max_candidates: int = 10
    enabled: bool = True

    def accepts_contract(self, name: str) -> bool:
        if not self.allowed_contracts:
            return True
        return any(
            pattern == name or pattern == "*"
            or (pattern.endswith("*") and name.startswith(pattern[:-1]))
            for pattern in self.allowed_contracts
        )

    def accepts_function(self, qualified: str) -> bool:
        if not self.allowed_functions:
            return True
        return any(
            pattern == qualified or pattern == "*"
            or (pattern.endswith("*") and qualified.startswith(pattern[:-1]))
            for pattern in self.allowed_functions
        )

    def accepts_category(self, category: str) -> bool:
        if not self.allowed_categories:
            return True
        return category in self.allowed_categories


@dataclass
class CampaignInvariant:
    """An invariant the campaign expects to hold."""
    statement: str
    category: str
    affected_state: list[str] = field(default_factory=list)
    policy: str = "exact"
    confidence: float = 0.7

    def render(self) -> str:
        return self.statement


@dataclass
class CampaignDefinition:
    """A named, scoped analysis campaign."""
    campaign_id: str
    name: str
    description: str
    pack: str = "generic"
    scope: CampaignScope = field(default_factory=CampaignScope)
    transitions: list[CampaignTransition] = field(default_factory=list)
    invariants: list[CampaignInvariant] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    priority: float = 0.5
    enabled: bool = True

    def matches_transition(self, edge_kind: str) -> bool:
        if not self.transitions:
            return True
        return any(t.kind.value == edge_kind for t in self.transitions)
