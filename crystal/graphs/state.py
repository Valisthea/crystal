"""Causal state graph: function → state → function edges with enriched metadata.

The graph answers "if A writes state X, who reads it next and under what
conditions?"  Edges carry the writer, reader, shared storage, key relation,
confidence, and source location so downstream engines (sequence discovery,
order-sensitivity, differential) can prune and prioritise without re-deriving
the relationships.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from .. import ir as I
from ..naming import StateNamespace, bare_name, contract_of
from .binding import build_bindings


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

    def without_contracts(self, excluded) -> "StateGraph":
        """A new graph with every element of the `excluded` contracts removed.

        Research drops scaffolding and superseded code (`test-contracts/`,
        `legacy/`, ...) *after* the graph is built, so without this the graph
        is the one output still describing them: a report that counts causal
        edges over code it says it excluded, an order-sensitivity pass whose
        edge budget is spent on a vendored tree, a campaign matching edge
        kinds nothing else can see. The copy describes the same contract set
        as `result["contracts"]`:

        * transitions of an excluded contract are dropped;
        * causal edges with either endpoint in an excluded contract are
          dropped;
        * state nodes owned by an excluded contract are dropped, and the
          writers/readers of the surviving nodes are pruned to surviving
          functions. A node that only excluded functions touched goes with
          them — `build_state_graph` would never have created it.

        Contract identity is the *name*, the key every `Contract.function`
        step and detector signal is already excluded by, so a live contract
        that shares its name with an excluded copy is excluded here exactly as
        it is everywhere else. Relative order is preserved throughout: sequence
        generation, order sensitivity, composition and campaigns walk these
        lists in order and budget by position, so the result is a stable
        subsequence of each list, never a re-sort. A transition's own
        reads/writes are its behaviour and are left untouched.
        """
        excluded = set(excluded)

        def dropped(function: str) -> bool:
            return _function_contract(function) in excluded

        transitions = [
            t for t in self.transitions
            if (t.contract or _function_contract(t.function)) not in excluded
        ]
        causal_edges = [
            e for e in self.causal_edges
            if (e.source_contract or _function_contract(e.source)) not in excluded
            and (e.target_contract or _function_contract(e.target)) not in excluded
        ]
        state_nodes: list[StateNode] = []
        for node in self.state_nodes:
            if (node.contract or contract_of(node.name)) in excluded:
                continue
            writers = tuple(f for f in node.writers if not dropped(f))
            readers = tuple(f for f in node.readers if not dropped(f))
            if (node.writers or node.readers) and not (writers or readers):
                continue
            if writers != node.writers or readers != node.readers:
                node = replace(node, writers=writers, readers=readers)
            state_nodes.append(node)
        return StateGraph(
            transitions=transitions,
            causal_edges=causal_edges,
            state_nodes=state_nodes,
        )


def _function_contract(qualified: str) -> str:
    """`"Vault.deposit"` -> `"Vault"`: the contract half of a graph function name.

    Graph functions are always `Contract.function`, whatever the source
    language (the `::` namespace belongs to state names, not functions). A bare
    name carries no contract and resolves to `""`.
    """
    return qualified.split(".", 1)[0] if "." in (qualified or "") else ""


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
    "call-flow": 0.66,
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

    sg.causal_edges.extend(_call_edges(contracts, fn_meta))
    return sg


def _call_edges(contracts, fn_meta) -> list[CausalEdge]:
    """Edges for calls that cross a contract boundary through a declared type.

    Storage-sharing is not the only way two functions compose. A protocol split
    across contracts composes by *calling*: `PegOutContract.refundPegOut` reaches
    `CollateralManagement.slashPegOutCollateral` through an interface-typed
    handle, and no storage is shared at any point. Without these edges a
    protocol that is nothing but composition yields an empty causal graph, and
    every downstream engine that walks it — sequence generation, composition
    candidates, order sensitivity, campaigns — reports nothing on the very
    shape it exists to find.

    Only receivers that resolve through a declared type produce an edge. A bare
    name match would connect any two contracts that happen to share a method
    name, which is the mistake namespacing exists to undo.
    """
    bindings = build_bindings(contracts)
    edges: list[CausalEdge] = []
    seen: set[tuple[str, str, int]] = set()

    for contract in contracts:
        reachable = _reachable_external_calls(contract)
        for function in contract.functions:
            source = f"{contract.name}.{function.name}"
            source_trans = fn_meta.get(source)
            if source_trans is None:
                continue
            # Only from a function something can actually call. An external
            # call made inside an internal helper belongs to the entry points
            # that reach it: `_transfer` is not callable, `refund` is.
            if not (source_trans.visibility in {"public", "external"}
                    or source_trans.is_entry_point):
                continue
            for call, hops in reachable.get(function.name, ()):
                for target, confidence in bindings.resolve(
                    contract.name, call.receiver, call.callee
                ):
                    target_trans = fn_meta.get(target)
                    if target_trans is None or target == source:
                        continue
                    key = (source, target, call.line)
                    if key in seen:
                        continue
                    seen.add(key)

                    # The callee's own writes are what the call sets in motion,
                    # so they are what a later step can consume.
                    consumed = tuple(sorted(target_trans.writes))
                    produced = tuple(sorted(source_trans.writes))
                    categories = tuple(sorted({
                        classify_state(name) for name in consumed
                    }))
                    score = min(0.95, confidence + 0.02 * len(consumed))
                    edges.append(CausalEdge(
                        source=source,
                        target=target,
                        produced=produced,
                        consumed=consumed,
                        score=round(score, 3),
                        edge_kind="call-flow",
                        categories=categories,
                        source_contract=contract.name,
                        target_contract=target_trans.contract,
                        source_path=source_trans.path,
                        target_path=target_trans.path,
                        source_line=call.line,
                        target_line=target_trans.line,
                        key_relation="declared-type",
                        condition=" -> ".join(
                            list(hops) + [f"{call.receiver}.{call.callee}"]
                        ),
                    ))
    return edges


def _reachable_external_calls(contract, max_depth: int = 4):
    """External calls each function reaches, following internal calls.

    Returns `{function name: [(call, internal hops taken to reach it)]}`. The
    hops are kept so the evidence can say `refund -> _transfer -> _c.slash`
    rather than anchoring a chain on a helper the caller cannot invoke.
    """
    declared = {function.name: function for function in contract.functions}
    direct: dict[str, list] = {}
    internal: dict[str, list[str]] = {}

    for function in contract.functions:
        outgoing, inner = [], []
        for call in (function.ir.calls() if function.ir is not None else ()):
            if call.kind in I.EXTERNAL_CALL_KINDS and call.receiver:
                outgoing.append(call)
            elif (call.kind in {I.INTERNAL_CALL, I.BUILTIN_CALL}
                  and call.callee in declared
                  and call.callee != function.name):
                inner.append(call.callee)
        direct[function.name] = outgoing
        internal[function.name] = inner

    def walk(name, seen, depth, hops):
        found = [(call, hops) for call in direct.get(name, ())]
        if depth >= max_depth:
            return found
        for callee in internal.get(name, ()):
            if callee in seen:
                continue
            found.extend(
                walk(callee, seen | {callee}, depth + 1, hops + (callee,))
            )
        return found

    return {
        function.name: walk(function.name, frozenset({function.name}), 0, ())
        for function in contract.functions
    }


# ── One-shot and attacker-reachability classification ───────────────────

# Substring that marks an OpenZeppelin-style initialization guard on a modifier:
# `initializer`, `reinitializer(2)`, `onlyInitializing` all contain it. The name
# is a library convention, not a protocol-specific one, so matching it is fair.
_INITIALIZER_MODIFIER_HINT = "initializ"

# A hand-rolled one-shot guards itself with a flag it both reads and sets.
_INIT_FLAG_HINTS = ("initialized", "initializing")

# Modifier substrings that mean "only a privileged principal may call this", so
# the function is not a surface an unprivileged attacker can drive.
_PRIVILEGED_MODIFIER_HINTS = (
    "owner", "admin", "governance", "role", "authorized", "restricted",
    "guardian",
)


def _is_one_shot(trans) -> bool:
    """True when a transition can run at most once for the life of the contract.

    Two shapes qualify: the OpenZeppelin `initializer`/`reinitializer` modifier
    (fast path), and the structural equivalent — a function guarded by an
    initialization flag it both reads and sets, so a second call cannot pass.
    """
    for modifier in trans.modifiers:
        if _INITIALIZER_MODIFIER_HINT in bare_name(modifier).lower():
            return True
    self_guarded = set(trans.reads) & set(trans.writes)
    for slot in self_guarded:
        normalized = bare_name(slot).lower().replace("_", "")
        if any(hint in normalized for hint in _INIT_FLAG_HINTS):
            return True
    return False


def _is_privileged(trans) -> bool:
    """True when a modifier restricts the call to an owner/role/admin principal."""
    for modifier in trans.modifiers:
        lowered = bare_name(modifier).lower()
        if any(hint in lowered for hint in _PRIVILEGED_MODIFIER_HINTS):
            return True
    return False


def one_shot_functions(state_graph) -> set[str]:
    """Qualified functions that can run at most once (see `_is_one_shot`).

    Such a function can never head or participate in a live attack sequence: the
    deployed proxy already ran it, so a second call reverts. Callers exclude it
    from any sequence of length > 1.
    """
    return {
        trans.function for trans in state_graph.transitions
        if _is_one_shot(trans)
    }


def attacker_reachable_functions(state_graph) -> set[str]:
    """Qualified entry points an unprivileged attacker can actually call.

    A chain is only worth triaging if its *head* is reachable: a public/external
    entry point that is neither one-shot (already initialized) nor gated behind
    an owner/role modifier. Tails are unrestricted — a privileged callee reached
    *through* a reachable head is exactly the composition we want to surface.
    """
    reachable: set[str] = set()
    for trans in state_graph.transitions:
        is_entry = (
            trans.visibility in {"public", "external"} or trans.is_entry_point
        )
        if not is_entry:
            continue
        if _is_one_shot(trans) or _is_privileged(trans):
            continue
        reachable.add(trans.function)
    return reachable


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
