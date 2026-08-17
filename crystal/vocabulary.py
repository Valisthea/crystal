"""Shared security vocabulary.

Both the detectors and the semantics layer need the same notion of "this name
means authority" or "this call moves value". Keeping it here, in a module that
imports nothing from Crystal, is what stops `semantics` and `detectors` from
importing each other in a cycle — and stops the two layers from drifting into
disagreeing about what a guard is.
"""

from __future__ import annotations

AUTH_HELPERS = {
    "_checkowner", "_checkrole", "checkrole", "onlyowner", "_onlyowner",
    "ensure_signed", "ensure_root", "ensure_signed_or_root", "require_auth",
    "only_owner", "_authorizeupgrade", "_requireauth", "hasrole",
    "_checkauthorized", "assert_owner", "authorize",
}

SENDER_TOKENS = ("msg.sender", "_msgsender", "msgsender", "self.sender",
                 "ctx.accounts", "signer", "origin", "caller")

PRIVILEGED_NAME_HINTS = (
    "owner", "admin", "implementation", "pause", "paused", "guardian",
    "authority", "treasury", "minter", "operator", "role", "whitelist",
    "blacklist", "oracle", "feerate", "fee", "rate", "cap", "limit",
    "beneficiary", "governance", "governor", "controller", "manager",
    "router", "vault", "keeper", "signer", "threshold", "delay",
)

REENTRANCY_GUARDS = (
    "nonreentrant", "noreentrancy", "reentrancyguard", "lock", "locked",
    "mutex", "nonreentrantbefore",
)

# Restriction state: modifying or consulting it decides who may proceed.
GUARD_NAME_HINTS = (
    "highsecurity", "high_security", "whitelist", "allowlist", "blocklist",
    "blacklist", "denylist", "frozen", "freeze", "paused", "pause", "blocked",
    "restricted", "reversible", "guardian", "locked", "banned", "sanction",
    "permission", "authorized", "authorised", "approved", "eligib",
)

GUARD_CALL_HINTS = (
    "is_allowed", "is_call_allowed", "is_high_security", "is_whitelisted",
    "is_frozen", "is_paused", "is_blocked", "is_authorized", "is_authorised",
    "can_transfer", "ensure_allowed", "check_allowed", "is_permitted",
    "is_restricted", "is_reversible",
)
