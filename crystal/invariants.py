"""Invariant candidate discovery from state structure, deltas, and causal graph.

Crystal does NOT affirm an invariant is true just because it observed it.
Every candidate carries evidence and confidence; the status is always
``invariant_candidate``, never ``invariant_proven``.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class InvariantCandidate:
    category: str
    expression: str
    confidence: float
    evidence: list[str]
    affected_functions: list[str] = field(default_factory=list)
    affected_state: list[str] = field(default_factory=list)
    suggested_test: str = ""
    source: str = "static-analysis"

    def render(self) -> str:
        return self.expression


# ── State variable classification ───────────────────────────────────────

_OWNERSHIP_HINTS = {"owner", "admin", "operator", "approved", "operators"}
_ROLE_HINTS = {"role", "roles", "hasrole", "minter", "pauser", "guardian"}
_NONCE_HINTS = {"nonce", "nonces", "counter", "index", "sequence"}
_TEMPORAL_HINTS = {"expiry", "deadline", "validuntil", "timestamp", "lockeduntil"}
_BALANCE_HINTS = {"balance", "balances", "allowance", "allowances"}
_APPROVAL_HINTS = {"approved", "approval", "approvals", "getapproved"}


def _classify(name: str) -> str:
    lower = name.lower().replace("_", "")
    for hints, cat in (
        (_BALANCE_HINTS, "balance"),
        (_OWNERSHIP_HINTS, "ownership"),
        (_ROLE_HINTS, "role"),
        (_NONCE_HINTS, "nonce"),
        (_TEMPORAL_HINTS, "temporal"),
        (_APPROVAL_HINTS, "approval"),
    ):
        if lower in hints or any(h in lower for h in hints):
            return cat
    return "storage"


# ── Discovery ───────────────────────────────────────────────────────────

def discover_invariants(contracts, state_deltas=None, state_graph=None):
    candidates: list[InvariantCandidate] = []

    for c in contracts:
        names = {v.name for v in c.state_vars}
        lower = {n.lower(): n for n in names}

        # Paired accounting variables.
        pairs = [
            ("totalassets", "totalsupply", "asset/share consistency"),
            ("assets", "shares", "asset/share consistency"),
            ("totaldebt", "totalassets", "debt/asset accounting"),
            ("reserve", "totalsupply", "reserve/supply accounting"),
        ]
        for left, right, category in pairs:
            if left in lower and right in lower:
                writers = _writers_of(c, lower[left]) | _writers_of(c, lower[right])
                candidates.append(InvariantCandidate(
                    category=category,
                    expression=(
                        f"{lower[left]} and {lower[right]} should remain "
                        f"internally consistent"
                    ),
                    confidence=0.76,
                    evidence=[f"{c.name}.{lower[left]}", f"{c.name}.{lower[right]}"],
                    affected_functions=sorted(writers),
                    affected_state=[lower[left], lower[right]],
                    suggested_test=(
                        f"assert {lower[left]} / {lower[right]} stays bounded "
                        f"across deposit/withdraw sequences"
                    ),
                ))

        # Monotonic counters / totals.
        for var in names:
            lv = var.lower()
            if any(token in lv for token in ("nonce", "counter", "index")):
                writers = _writers_of(c, var)
                candidates.append(InvariantCandidate(
                    category="monotonicity",
                    expression=(
                        f"{var} should not decrease during ordinary "
                        f"protocol transitions"
                    ),
                    confidence=0.68,
                    evidence=[f"{c.name}.{var}"],
                    affected_functions=sorted(writers),
                    affected_state=[var],
                    suggested_test=f"assert {var}_after >= {var}_before",
                ))

        # User balances and total accounting surfaces.
        has_balance = any("balance" in n.lower() for n in names)
        has_total = any(n.lower().startswith("total") for n in names)
        if has_balance and has_total:
            candidates.append(InvariantCandidate(
                category="conservation",
                expression=(
                    "aggregate user accounting should remain bounded "
                    "by protocol totals/reserves"
                ),
                confidence=0.64,
                evidence=[c.name],
                affected_state=[
                    n for n in names
                    if "balance" in n.lower() or n.lower().startswith("total")
                ],
                suggested_test=(
                    "assert sum(balances[user] for user in users) "
                    "<= totalSupply"
                ),
            ))

        # Ownership exclusivity.
        owner_vars = [n for n in names if _classify(n) == "ownership"]
        for ov in owner_vars:
            writers = _writers_of(c, ov)
            readers = _readers_of(c, ov)
            if writers and readers:
                candidates.append(InvariantCandidate(
                    category="ownership",
                    expression=(
                        f"after {ov} transfer, previous holder must not "
                        f"retain privileged authority"
                    ),
                    confidence=0.72,
                    evidence=[f"{c.name}.{ov}"],
                    affected_functions=sorted(writers | readers),
                    affected_state=[ov],
                    suggested_test=(
                        f"transfer {ov}, then assert old holder cannot "
                        f"call privileged functions"
                    ),
                ))

        # Role revocation invariants.
        role_vars = [n for n in names if _classify(n) == "role"]
        for rv in role_vars:
            writers = _writers_of(c, rv)
            if writers:
                candidates.append(InvariantCandidate(
                    category="role",
                    expression=(
                        f"a {rv} revocation must invalidate stale authority"
                    ),
                    confidence=0.68,
                    evidence=[f"{c.name}.{rv}"],
                    affected_functions=sorted(writers),
                    affected_state=[rv],
                    suggested_test=(
                        f"revoke {rv}, then assert revoked entity cannot act"
                    ),
                ))

        # Nonce/session invalidation.
        nonce_vars = [n for n in names if _classify(n) == "nonce"]
        for nv in nonce_vars:
            writers = _writers_of(c, nv)
            if writers:
                candidates.append(InvariantCandidate(
                    category="nonce",
                    expression=(
                        f"after {nv} invalidation, previously authorized "
                        f"session must not execute"
                    ),
                    confidence=0.70,
                    evidence=[f"{c.name}.{nv}"],
                    affected_functions=sorted(writers),
                    affected_state=[nv],
                    suggested_test=(
                        f"authorize, increment {nv}, then assert old "
                        f"authorization fails"
                    ),
                ))

        # Temporal expiry.
        temporal_vars = [n for n in names if _classify(n) == "temporal"]
        for tv in temporal_vars:
            readers = _readers_of(c, tv)
            if readers:
                candidates.append(InvariantCandidate(
                    category="temporal",
                    expression=f"an expired {tv} must not authorize action",
                    confidence=0.66,
                    evidence=[f"{c.name}.{tv}"],
                    affected_functions=sorted(readers),
                    affected_state=[tv],
                    suggested_test=(
                        f"set timestamp > {tv}, then assert action is rejected"
                    ),
                ))

        # Approval scope.
        approval_vars = [n for n in names if _classify(n) == "approval"]
        for av in approval_vars:
            writers = _writers_of(c, av)
            if writers:
                candidates.append(InvariantCandidate(
                    category="approval",
                    expression=(
                        f"{av} must not create authority beyond its scope"
                    ),
                    confidence=0.64,
                    evidence=[f"{c.name}.{av}"],
                    affected_functions=sorted(writers),
                    affected_state=[av],
                    suggested_test=(
                        f"grant {av}, then assert only scoped actions succeed"
                    ),
                ))

    # Delta-derived invariants.
    if state_deltas:
        candidates.extend(_delta_invariants(state_deltas))

    # Causal-graph-derived invariants.
    if state_graph:
        candidates.extend(_causal_invariants(state_graph))

    return candidates


def _writers_of(contract, var_name: str) -> set[str]:
    return {
        f"{contract.name}.{f.name}" for f in contract.functions
        if var_name in f.writes
    }


def _readers_of(contract, var_name: str) -> set[str]:
    return {
        f"{contract.name}.{f.name}" for f in contract.functions
        if var_name in f.reads
    }


def _delta_invariants(state_deltas) -> list[InvariantCandidate]:
    """Derive invariants from observed state delta patterns."""
    candidates: list[InvariantCandidate] = []
    conservation_pairs: dict[frozenset[str], int] = {}

    for delta in state_deltas:
        changed = set(delta.changed)
        if len(changed) >= 2:
            for v1 in changed:
                for v2 in changed:
                    if v1 >= v2:
                        continue
                    pair = frozenset({v1, v2})
                    conservation_pairs[pair] = (
                        conservation_pairs.get(pair, 0) + 1
                    )

    for pair, count in conservation_pairs.items():
        if count < 2:
            continue
        v1, v2 = sorted(pair)
        candidates.append(InvariantCandidate(
            category="delta-correlation",
            expression=(
                f"{v1} and {v2} change together across {count} "
                f"sequences — potential conservation law"
            ),
            confidence=min(0.72, 0.50 + 0.05 * count),
            evidence=[f"co-changed in {count} sequences"],
            affected_state=[v1, v2],
            source="delta-analysis",
        ))

    return candidates


def _causal_invariants(state_graph) -> list[InvariantCandidate]:
    """Derive invariants from causal graph structure."""
    candidates: list[InvariantCandidate] = []

    for node in getattr(state_graph, "state_nodes", []):
        if node.category == "ownership" and len(node.writers) > 1:
            candidates.append(InvariantCandidate(
                category="ownership",
                expression=(
                    f"multiple writers to {node.name} — ownership "
                    f"transfer paths must agree on post-conditions"
                ),
                confidence=0.62,
                evidence=list(node.writers[:5]),
                affected_functions=list(node.writers),
                affected_state=[node.name],
                source="causal-graph",
            ))

    return candidates
