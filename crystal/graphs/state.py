"""Causal state graph: function → state → function edges with enriched metadata.

The graph answers "if A writes state X, who reads it next and under what
conditions?"  Edges carry the writer, reader, shared storage, key relation,
confidence, and source location so downstream engines (sequence discovery,
order-sensitivity, differential) can prune and prioritise without re-deriving
the relationships.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..naming import StateNamespace, bare_name


# ── State classification ────────────────────────────────────────────────

BALANCE_HINTS = frozenset({
    "balance", "balances", "balanceof", "totalsupply", "totalassets",
    "totaldebt", "reserve", "shares", "allowance", "allowances",
})

OWNERSHIP_HINTS = frozenset({
    "owner", "ownerof", "admin", "operator", "operators", "approved",
    "approvedfor", "approvals", "getapproved", "isapprovedforall",
})

ROLE_HINTS = frozenset({
    "role", "roles", "hasrole", "roleadmin", "minter", "pauser",
    "guardian", "authority", "whitelisted", "blacklisted",
    "permission", "permissions",
})

NONCE_HINTS = frozenset({
    "nonce", "nonces", "counter", "index", "sequence",
})

TEMPORAL_HINTS = frozenset({
    "expiry", "expiration", "deadline", "validuntil", "timestamp",
    "lockeduntil", "cooldown", "duration", "lastupdated",
})

PROXY_HINTS = frozenset({
    "implementation", "proxy", "beacon", "upgradeableproxy",
})

REGISTRY_HINTS = frozenset({
    "registry", "resolver", "resolver_", "records", "registrar",
    "parent", "child", "subdomain", "node",
})


def classify_state(name: str) -> str:
    """Classify a state variable by semantic category.

    Category is a question about meaning, not identity, so the contract
    namespace is stripped first: `Vault::balances` classifies as `balance`
    exactly like the bare `balances`.
    """
    lower = bare_name(name).lower().replace("_", "")
    for hint_set, category in (
        (BALANCE_HINTS, "balance"),
        (OWNERSHIP_HINTS, "ownership"),
        (ROLE_HINTS, "role"),
        (NONCE_HINTS, "nonce"),
        (TEMPORAL_HINTS, "temporal"),
        (PROXY_HINTS, "proxy"),
        (REGISTRY_HINTS, "registry"),
    ):
        if lower in hint_set or any(h in lower for h in hint_set):
            return category
    return "storage"


# ── Data structures ─────────────────────────────────────────────────────

@dataclass
class StateTransition:
    function: str
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    visibility: str = "unspecified"
    contract: str = ""
    path: str = ""
    line: int = 0
    kind: str = "function"
    modifiers: tuple[str, ...] = ()
    is_entry_point: bool = False
    payable: bool = False


@dataclass(frozen=True)
class CausalEdge:
    source: str
    target: str
    produced: tuple[str, ...]
    consumed: tuple[str, ...]
    score: float
    # Enriched fields — all default so existing callers are unaffected.
    edge_kind: str = "write-read"
    categories: tuple[str, ...] = ()
    source_contract: str = ""
    target_contract: str = ""
    source_path: str = ""
    target_path: str = ""
    source_line: int = 0
    target_line: int = 0
    key_relation: str = ""
    condition: str = ""


@dataclass
class StateNode:
    """A state variable as a node in the causal graph."""
    name: str
    contract: str
    category: str
    is_mapping: bool = False
    key_types: tuple[str, ...] = ()
    writers: tuple[str, ...] = ()
    readers: tuple[str, ...] = ()
    line: int = 0
    path: str = ""


@dataclass
class StateGraph:
    transitions: list[StateTransition] = field(default_factory=list)
    causal_edges: list[CausalEdge] = field(default_factory=list)
    state_nodes: list[StateNode] = field(default_factory=list)

    def writers_of(self, state: str) -> list[str]:
        return [t.function for t in self.transitions if state in t.writes]

    def readers_of(self, state: str) -> list[str]:
        return [t.function for t in self.transitions if state in t.reads]

    def edges_from(self, function: str) -> list[CausalEdge]:
        return [e for e in self.causal_edges if e.source == function]

    def edges_to(self, function: str) -> list[CausalEdge]:
        return [e for e in self.causal_edges if e.target == function]

    def shared_state(self, fn_a: str, fn_b: str) -> set[str]:
        """State variables touched by both functions."""
        a_all: set[str] = set()
        b_all: set[str] = set()
        for t in self.transitions:
            if t.function == fn_a:
                a_all |= t.reads | t.writes
            elif t.function == fn_b:
                b_all |= t.reads | t.writes
        return a_all & b_all


# ── Edge kind scoring ────────────────────────────────────────────────────

EDGE_KIND_SCORES = {
    "write-read": 0.62,
    "write-write": 0.55,
    "auth-action": 0.78,
    "ownership-action": 0.82,
    "role-action": 0.76,
    "nonce-auth": 0.74,
    "temporal-action": 0.72,
    "registry-action": 0.68,
    "balance-transfer": 0.70,
}


def _classify_edge(source_trans, target_trans, produced, consumed) -> str:
    """Classify a causal edge by the semantic category of shared state."""
    categories = {classify_state(s) for s in consumed}

    if "ownership" in categories and target_trans.writes:
        return "ownership-action"
    if "role" in categories and target_trans.writes:
        return "role-action"
    if "nonce" in categories:
        return "nonce-auth"
    if "temporal" in categories:
        return "temporal-action"
    if "balance" in categories:
        return "balance-transfer"
    if "registry" in categories:
        return "registry-action"

    source_writes_auth = any(
        classify_state(s) in ("ownership", "role") for s in source_trans.writes
    )
    if source_writes_auth and target_trans.writes:
        return "auth-action"

    if produced - consumed:
        return "write-write"
    return "write-read"


def _key_overlap(contracts, namespace, source_fn, target_fn, shared_vars) -> str:
    """Detect when writer and reader use the same mapping key."""
    var_keys: dict[str, set[str]] = {}
    for c in contracts:
        for sv in c.state_vars:
            slot = namespace.qualify(c.name, sv.name)
            if slot in shared_vars and sv.is_mapping:
                var_keys[slot] = set(sv.key_types)
    if not var_keys:
        return ""
    return "shared-key" if var_keys else ""


# ── Graph construction ──────────────────────────────────────────────────

def build_state_graph(contracts):
    sg = StateGraph()
    fn_meta: dict[str, StateTransition] = {}
    # State names are only unique within a contract. Keying the graph on bare
    # names merges every same-named variable in the project into one node,
    # which both loses real edges and invents edges between contracts that
    # share nothing but a spelling.
    namespace = StateNamespace(contracts)

    for c in contracts:
        for f in c.functions:
            qualified = f"{c.name}.{f.name}"
            trans = StateTransition(
                function=qualified,
                reads={namespace.qualify(c.name, r) for r in f.reads},
                writes={namespace.qualify(c.name, w) for w in f.writes},
                visibility=f.visibility,
                contract=c.name,
                path=f.path or c.path,
                line=f.line,
                kind=f.kind,
                modifiers=tuple(f.modifiers),
                is_entry_point=f.is_entry_point,
                payable=f.payable,
            )
            sg.transitions.append(trans)
            fn_meta[qualified] = trans

    # Build state nodes.
    state_writers: dict[str, list[str]] = {}
    state_readers: dict[str, list[str]] = {}
    for t in sg.transitions:
        for s in t.writes:
            state_writers.setdefault(s, []).append(t.function)
        for s in t.reads:
            state_readers.setdefault(s, []).append(t.function)

    all_state = set(state_writers) | set(state_readers)
    sv_info: dict[str, tuple] = {}
    for c in contracts:
        for sv in c.state_vars:
            sv_info[namespace.qualify(c.name, sv.name)] = (
                c.name, sv.is_mapping, tuple(sv.key_types), sv.line, c.path
            )

    for name in sorted(all_state):
        info = sv_info.get(name)
        sg.state_nodes.append(StateNode(
            name=name,
            contract=info[0] if info else "",
            category=classify_state(name),
            is_mapping=info[1] if info else False,
            key_types=info[2] if info else (),
            writers=tuple(state_writers.get(name, [])),
            readers=tuple(state_readers.get(name, [])),
            line=info[3] if info else 0,
            path=info[4] if info else "",
        ))

    # Build causal edges — entry points only for manageable size.
    entry_transitions = [
        t for t in sg.transitions
        if t.visibility in {"public", "external"} or t.is_entry_point
    ]

    for a in entry_transitions:
        for b in entry_transitions:
            if a.function == b.function:
                continue
            produced = a.writes & (b.reads | b.writes)
            consumed = a.writes & b.reads
            if not consumed:
                continue

            edge_kind = _classify_edge(a, b, produced, consumed)
            base_score = EDGE_KIND_SCORES.get(edge_kind, 0.55)
            score = min(0.95, base_score + 0.04 * len(consumed)
                        + 0.02 * len(produced - consumed))

            categories = tuple(sorted({classify_state(s) for s in consumed}))
            key_rel = _key_overlap(
                contracts, namespace, a.function, b.function, consumed
            )

            sg.causal_edges.append(CausalEdge(
                source=a.function,
                target=b.function,
                produced=tuple(sorted(produced)),
                consumed=tuple(sorted(consumed)),
                score=round(score, 3),
                edge_kind=edge_kind,
                categories=categories,
                source_contract=a.contract,
                target_contract=b.contract,
                source_path=a.path,
                target_path=b.path,
                source_line=a.line,
                target_line=b.line,
                key_relation=key_rel,
            ))

    return sg


# ── Sequence candidate generation ───────────────────────────────────────

# Priority categories that boost a sequence's score.
PRIORITY_CATEGORIES = frozenset({
    "ownership", "role", "nonce", "temporal", "balance", "proxy",
})


def candidate_sequences(state_graph, max_len=3):
    edges = state_graph.causal_edges
    outgoing: dict[str, list[CausalEdge]] = {}
    for e in edges:
        outgoing.setdefault(e.source, []).append(e)

    result: list[tuple[tuple[str, ...], float, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()

    def walk(path, edge_scores, categories):
        if len(path) >= 2:
            key = tuple(path)
            if key not in seen:
                seen.add(key)
                priority_boost = 0.04 * len(
                    set(categories) & PRIORITY_CATEGORIES
                )
                final_score = min(edge_scores) + priority_boost
                result.append((key, final_score, tuple(sorted(set(categories)))))
        if len(path) >= max_len:
            return
        for e in outgoing.get(path[-1], []):
            if e.target in path:
                continue
            walk(path + [e.target], edge_scores + [e.score],
                 categories + list(e.categories))

    for e in edges:
        walk([e.source, e.target], [e.score], list(e.categories))

    result.sort(key=lambda x: (-len(x[0]), -x[1], x[0]))
    return [x[0] for x in result[:250]]
