"""What each stage of a pipeline actually does.

The distinction that carries the whole detector is between a stage that enforces
an *authority* decision and one that merely validates *protocol metadata*.
`CheckNonce` rejects transactions all day long and guards nothing: it compares a
counter. Treating it as a guard would make every pipeline report a bypass, which
is the failure mode this classifier exists to prevent.

The test is therefore not "does it reject" but "does its rejection consult
restriction state about a principal".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..vocabulary import (
    AUTH_HELPERS,
    GUARD_CALL_HINTS,
    GUARD_NAME_HINTS,
    SENDER_TOKENS,
)

GUARD = "GUARD"
MOVES_VALUE = "MOVES-VALUE"
CHECKS_ONLY = "CHECKS-ONLY"
OBSERVES = "OBSERVES"
UNRESOLVED = "UNRESOLVED"

# Protocol metadata: replay counters, versions, chain identity, resource limits.
# Rejecting on these is correctness, not authorization.
PROTOCOL_METADATA_HINTS = (
    "nonce", "spec_version", "specversion", "transaction_version", "txversion",
    "genesis", "era", "mortality", "weight", "length", "metadata_hash",
    "metadatahash", "block_hash", "blockhash", "extrinsic_version", "reclaim",
    "birth", "death", "period", "phase", "hash",
)

VALUE_CALL_RE = re.compile(
    r"^(?:do_|try_|force_|unchecked_|_)?"
    r"(?:transfer|withdraw|deposit|mint|burn|slash|settle|repay|redeem|reserve"
    r"|unreserve|charge|pay|debit|send|refund|hold|release)"
    r"(?:_\w+)?$"
)
VALUE_CALL_EXACT = {
    "correct_and_deposit_fee", "correct_and_deposit", "can_withdraw_fee",
    "withdraw_fee", "deposit_into_existing", "deposit_creating",
    "make_free_balance_be", "resolve_into_existing",
}

REJECTION_TOKENS = ("err(", "return err", "revert", "invalid", "reject",
                    "ensure!", "assert", "throw", "transactionvalidityerror")

# What the value operation belongs to. Two stages acting on the same account
# through different families are exactly the F5 shape.
MECHANISM_FAMILIES = {
    "fees": ("fee", "charge", "tip", "pay", "correct_and_deposit"),
    "transfers": ("transfer", "send", "do_transfer"),
    "custody": ("hold", "release", "reserve", "unreserve", "lock", "unlock"),
    "supply": ("mint", "burn", "issue"),
    "penalty": ("slash", "confiscate"),
    "settlement": ("settle", "repay", "redeem", "withdraw", "deposit", "debit"),
}


def moves_value(callee: str) -> bool:
    name = (callee or "").lower()
    return name in VALUE_CALL_EXACT or bool(VALUE_CALL_RE.match(name))


def mechanism_family(callee: str) -> str:
    name = (callee or "").lower()
    for family, hints in MECHANISM_FAMILIES.items():
        if any(hint in name for hint in hints):
            return family
    return "unknown"


@dataclass
class StageRole:
    name: str
    index: int
    resolved: bool
    role: str = UNRESOLVED
    guard_evidence: list[str] = field(default_factory=list)
    value_evidence: list[str] = field(default_factory=list)
    consulted_guards: list[str] = field(default_factory=list)
    metadata_checks: list[str] = field(default_factory=list)
    mechanisms: list[str] = field(default_factory=list)
    principals: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
    routed_to: list[str] = field(default_factory=list)
    external_routes: list[str] = field(default_factory=list)
    path: str = ""
    line: int = 0
    crate: str = ""

    @property
    def is_guard(self) -> bool:
        return self.role == GUARD

    @property
    def is_mover(self) -> bool:
        return self.role == MOVES_VALUE

    # Kept for the module-graph view, which reads roles as predicates.
    @property
    def moves_value(self) -> bool:
        return self.role == MOVES_VALUE

    def scope(self) -> str:
        if self.role == GUARD:
            families = ", ".join(self.mechanisms) or "RuntimeCall dispatch"
            return f"gates {families}"
        if self.role == MOVES_VALUE:
            families = ", ".join(self.mechanisms) or "value"
            return f"debits {', '.join(self.principals) or 'an account'} via {families}"
        if self.role == CHECKS_ONLY:
            return "validates protocol metadata: " + ", ".join(self.metadata_checks[:4])
        if self.role == OBSERVES:
            return "records or observes without rejecting or moving value"
        return "not resolved to parsed code"


def _principals(text: str) -> list[str]:
    lowered = (text or "").lower()
    return sorted({token for token in SENDER_TOKENS if token in lowered})


def _rejects(statement) -> bool:
    lowered = (statement.text or "").lower()
    if statement.kind in {"require", "revert"}:
        return True
    if any(token in lowered for token in REJECTION_TOKENS):
        return True
    for child in list(statement.body) + list(statement.orelse):
        child_text = (child.text or "").lower()
        if child.kind in {"revert", "return"} and any(
            token in child_text for token in REJECTION_TOKENS
        ):
            return True
    return False


def classify(contract, resolver=None) -> StageRole:
    """Assign one role to a pipeline stage from what its code does."""
    role = StageRole(contract.name, -1, True, OBSERVES,
                     path=contract.path, line=contract.line)

    for function in contract.functions:
        if function.ir is None or function.is_test:
            continue
        for statement in function.ir.walk():
            text = statement.text or ""
            lowered = text.lower()
            call = statement.call
            callee = (call.callee or "").lower() if call is not None else ""

            if call is not None and moves_value(callee):
                role.value_evidence.append(
                    f"{contract.name}.{function.name}:{call.line} {text[:140]}"
                )
                family = mechanism_family(callee)
                if family not in role.mechanisms:
                    role.mechanisms.append(family)
                role.principals = sorted(set(role.principals) | set(_principals(text)))

            authority_call = bool(callee) and (
                callee in AUTH_HELPERS
                or any(hint in callee for hint in GUARD_CALL_HINTS)
            )
            restriction_state = any(hint in lowered for hint in GUARD_NAME_HINTS)
            if (authority_call or restriction_state) and _rejects(statement):
                role.guard_evidence.append(
                    f"{contract.name}.{function.name}:{statement.line} {text[:140]}"
                )
                hit = callee if authority_call else next(
                    (hint for hint in GUARD_NAME_HINTS if hint in lowered), ""
                )
                if hit and hit not in role.consulted_guards:
                    role.consulted_guards.append(hit)
                role.principals = sorted(set(role.principals) | set(_principals(text)))
            elif _rejects(statement) and any(
                hint in lowered for hint in PROTOCOL_METADATA_HINTS
            ):
                hit = next(hint for hint in PROTOCOL_METADATA_HINTS if hint in lowered)
                if hit not in role.metadata_checks:
                    role.metadata_checks.append(hit)

    if resolver is not None:
        for resolution in resolver.resolutions_for(contract):
            if resolution.resolved:
                if resolution.contract not in role.routed_to:
                    role.routed_to.append(resolution.contract)
                    role.routes.append(resolution.describe())
            elif resolution.external:
                role.external_routes.append(resolution.describe())

    # Order matters: a stage that both guards and moves value is a guard, because
    # it is not the one bypassing anything.
    if role.guard_evidence:
        role.role = GUARD
        if not role.mechanisms:
            role.mechanisms.append("call-dispatch")
    elif role.value_evidence:
        role.role = MOVES_VALUE
    elif role.metadata_checks:
        role.role = CHECKS_ONLY
    else:
        role.role = OBSERVES
    return role


def inherit_routed(role: StageRole, contracts_by_name, resolver=None) -> StageRole:
    """Fold in what the runtime-bound implementations do.

    The debit really happens in `FungibleAdapter`, so if that consulted the
    whitelist the stage would be covered and no signal should fire.
    """
    for target in list(role.routed_to):
        routed = contracts_by_name.get(target)
        if routed is None:
            continue
        inner = classify(routed, resolver=None)
        role.guard_evidence.extend(inner.guard_evidence)
        role.value_evidence.extend(inner.value_evidence)
        for guard in inner.consulted_guards:
            if guard not in role.consulted_guards:
                role.consulted_guards.append(guard)
        for family in inner.mechanisms:
            if family not in role.mechanisms:
                role.mechanisms.append(family)
    if role.guard_evidence and role.role == MOVES_VALUE:
        role.role = GUARD
    elif role.value_evidence and role.role in {OBSERVES, CHECKS_ONLY}:
        role.role = MOVES_VALUE
    return role
