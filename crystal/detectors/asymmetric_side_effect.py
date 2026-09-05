"""Asymmetric side-effect detector.

Two shapes, one asymmetry: paths that are supposed to be equivalent are not.

1. Companion asymmetry (the F3 shape). Several sites perform the same primary
   operation (e.g. ``increase_balance``) and most of them pair it with a
   companion side-effect (e.g. ``record_transfer``); one site forgets. On
   Quantus the two forgetful sites credited funds that had no zk-tree leaf.

2. Guard asymmetry (the sibling shape). Two entry points of the same contract
   reach the same callee with the same argument shape. On one path the call
   sits behind a condition that can revoke it — a ``require``, an
   ``if (..) revert``, an early return, an enclosing branch, a modifier — and
   on the other path it does not. The guarded path looks correct in isolation
   and the bare one looks simple, which is why this shape ships.

The guard shape is structural: any callee qualifies, and the guard is read
from the statement tree (the conditions enclosing the call, the reverts that
precede it, the helpers on its path) rather than from a name list. A guard is
compared only when it is *coupled* to the call: it consults the component the
call mutates, or state the callee touches, or a value the call receives.
Two entry points with unrelated preconditions are not an asymmetry; the same
transition, once conditional and once not, is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import ir as I
from ..vocabulary import REENTRANCY_GUARDS
from .base import DetectorSignal, signal

DETECTOR = "asymmetric-side-effect"

# Operations that move value. For the companion shape these are the primary
# operations; for the guard shape they are only a confidence boost.
VALUE_OPERATIONS = frozenset({
    "increase_balance", "decrease_balance",
    "deposit", "withdraw",
    "mint_into", "burn_from", "mint", "burn",
    "transfer", "deposit_creating", "deposit_into_existing",
    "slash", "reward",
    "credit", "debit",
    # Solidity internal-function variants (underscore-prefixed).
    "_mint", "_burn", "_transfer",
    "_safeMint", "_safeTransfer",
    "transferFrom", "safeTransfer", "safeTransferFrom",
})

# Patterns that suggest an operation is initialisation/genesis (one-shot).
GENESIS_HINTS = re.compile(
    r"genesis|initialize|init|bootstrap|setup|on_genesis", re.IGNORECASE
)

# One-shot library modifiers: an initializer is deployment, not a sibling.
ONE_SHOT_MODIFIERS = frozenset({"initializer", "reinitializer", "onlyinitializing"})

# Guard kinds. `revert` and `exit` are conditions that terminate the function
# before the call; `branch` is a condition the call sits inside; `modifier`
# is a function-level guard.
REVERT, EXIT, BRANCH, MODIFIER = "revert", "exit", "branch", "modifier"

# Coupling strength between a guard and the call it precedes.
STRONG, WEAK = "strong", "weak"

INLINE_DEPTH = 3

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TYPE_WORDS = frozenset({
    "uint", "int", "bool", "address", "bytes", "string", "memory", "storage",
    "calldata", "payable", "let", "mut", "var", "const",
})


def _leaf_name(qualified: str) -> str:
    """Leaf function name; handles Rust ``A::B::f`` and Solidity ``a.f``."""
    name = qualified.rsplit("::", 1)[-1]
    name = name.rsplit(".", 1)[-1]
    return name.split("(")[0].strip()


def _normalize(text: str) -> str:
    text = re.sub(r"\s+", " ", (text or "").strip())
    while text.startswith("(") and text.endswith(")"):
        text = text[1:-1].strip()
    return text


def _flip(polarity: str) -> str:
    return "fails" if polarity == "holds" else "holds"


def _condition_key(text: str, polarity: str) -> tuple[str, str]:
    """``require(!x)`` and ``if (x) revert`` gate the same thing."""
    normalized = _normalize(text)
    while normalized.startswith("!"):
        normalized = _normalize(normalized[1:])
        polarity = _flip(polarity)
    return normalized, polarity


def _local_name(target) -> str | None:
    if target is None:
        return None
    names = [
        n for n in _IDENT.findall(target.text)
        if n not in _TYPE_WORDS and not re.fullmatch(r"u?int\d*|bytes\d+", n)
    ]
    return names[-1] if names else None


# ---------------------------------------------------------------------------
# Guard shape
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _Guard:
    kind: str
    text: str
    line: int
    polarity: str            # what must be true of `text` for the call to run
    identifiers: tuple[str, ...]
    origin: str = ""         # helper the guard was inlined from, if any

    @property
    def key(self) -> tuple:
        if self.kind == MODIFIER:
            return (MODIFIER, self.text.split("(")[0].strip().lower())
        return ("condition",) + _condition_key(self.text, self.polarity)

    def describe(self) -> str:
        where = f"L{self.line}"
        if self.origin:
            where += f" (in {self.origin})"
        if self.kind == MODIFIER:
            return f"{where} modifier {self.text}"
        if self.kind == BRANCH:
            state = "hold" if self.polarity == "holds" else "fail"
            return f"{where} branch `{self.text}` must {state}"
        verb = "reverts" if self.kind == REVERT else "returns early"
        if self.polarity == "fails":
            return f"{where} {verb} when `{self.text}`"
        return f"{where} {verb} unless `{self.text}`"


@dataclass
class _Env:
    """What the walk knows about locals at a point on the path."""

    receiver_of: dict[str, str] = field(default_factory=dict)
    state_of: dict[str, frozenset[str]] = field(default_factory=dict)

    def copy(self) -> "_Env":
        return _Env(dict(self.receiver_of), dict(self.state_of))

    def state_behind(self, identifiers) -> set[str]:
        found: set[str] = set(identifiers)
        for name in identifiers:
            found |= self.state_of.get(name, frozenset())
        return found


@dataclass
class _Site:
    contract: str
    function: str
    path: str
    line: int
    language: str
    callee: str
    receiver: str | None
    call_text: str
    arg_texts: tuple[str, ...]
    arg_identifiers: frozenset[str]
    guards: tuple[_Guard, ...]
    env: _Env
    resolved: object = None      # the callee's Function when it can be found
    # Guards the callee re-establishes itself before acting; a wrapper that
    # pre-checks what its callee checks is defensive, not protective.
    rechecked: frozenset = frozenset()

    @property
    def qualified(self) -> str:
        return f"{self.contract}.{self.function}"

    @property
    def group_key(self) -> tuple:
        return (self.contract, self.callee, _normalize(self.receiver or ""),
                len(self.arg_texts))


class _Anchor:
    """Signals anchor on a function; a call site anchors on its enclosing one."""

    def __init__(self, contract, name, line, path, language):
        self.contract = contract
        self.name = name
        self.line = line
        self.path = path
        self.language = language
        self.is_entry_point = True


class _Resolver:
    """Find the function a call reaches, without trusting names to be unique."""

    def __init__(self, contracts):
        self.contracts = list(contracts)
        self.by_name = {c.name: c for c in self.contracts}
        self.type_names = set(self.by_name)
        self.bodies: dict[str, list] = {}
        self._helper_cache: dict[tuple, tuple] = {}
        for contract in self.contracts:
            for function in contract.functions:
                if function.kind in {"modifier", "constructor"}:
                    continue
                self.bodies.setdefault(function.name, []).append(function)

    def is_type(self, name: str) -> bool:
        return name in self.type_names

    def helper_guards(self, function, contract, depth=INLINE_DEPTH) -> tuple:
        key = (id(function), depth)
        if key not in self._helper_cache:
            self._helper_cache[key] = tuple(
                _helper_guards(function, self, contract, depth, set())
            )
        return self._helper_cache[key]

    def _in_hierarchy(self, contract, name, seen=None):
        seen = seen or set()
        if contract is None or contract.name in seen:
            return None
        seen.add(contract.name)
        for function in contract.functions:
            if function.name == name and function.kind not in {"modifier", "constructor"}:
                return function
        for base in contract.bases:
            found = self._in_hierarchy(self.by_name.get(base), name, seen)
            if found is not None:
                return found
        return None

    def receiver_type(self, contract, receiver: str | None) -> str | None:
        if not receiver:
            return None
        base = receiver.split(".")[0].split("[")[0].strip()
        for variable in contract.state_vars:
            if variable.name == base:
                words = _IDENT.findall(variable.type_name)
                return words[-1] if words else None
        return base if base in self.type_names else None

    def resolve(self, contract, call):
        name = _leaf_name(call.callee)
        if call.kind == I.INTERNAL_CALL and call.receiver is None:
            return self._in_hierarchy(contract, name)
        candidates = [f for f in self.bodies.get(name, ()) if f.has_ir]
        if not candidates:
            return None
        wanted = self.receiver_type(contract, call.receiver)
        if wanted:
            for function in candidates:
                owner = self.by_name.get(function.contract)
                if function.contract == wanted or (
                    owner is not None and wanted in owner.bases
                ):
                    return function
        if len(candidates) == 1:
            return candidates[0]
        return None


def _block_terminates(statements) -> str | None:
    """`revert`/`exit` when every path through the block leaves the function."""
    for stmt in statements:
        if stmt.kind == I.REVERT:
            return REVERT
        if stmt.kind == I.REQUIRE:
            text = _normalize(stmt.condition.text if stmt.condition else "")
            if text in {"false", "0"}:
                return REVERT
        if stmt.kind == I.RETURN:
            return EXIT
    return None


def _condition_of(stmt) -> tuple[str, tuple[str, ...]]:
    if stmt.condition is not None:
        return stmt.condition.text, tuple(stmt.condition.identifiers)
    text = stmt.text.strip()
    match = re.match(r"(?:require|assert|ensure!?)\s*\((.*)\)\s*;?$", text, re.S)
    inner = match.group(1) if match else text
    inner = inner.rsplit(",", 1)[0] if '"' in inner else inner
    return inner, tuple(dict.fromkeys(_IDENT.findall(inner)))


def _helper_guards(function, resolver, contract, depth, seen) -> list[_Guard]:
    """Unconditional revocations a helper performs before returning.

    Only top-level guards count: a revert nested in a branch of the helper is
    not something every caller is protected by.
    """
    if function is None or not function.has_ir or function.name in seen or depth <= 0:
        return []
    seen = seen | {function.name}
    out: list[_Guard] = []
    for stmt in function.ir.statements:
        if stmt.kind == I.REQUIRE:
            text, ids = _condition_of(stmt)
            out.append(_Guard(REVERT, text, stmt.line, "holds", ids, function.name))
        elif stmt.kind == I.IF and not stmt.orelse:
            kind = _block_terminates(stmt.body)
            if kind and stmt.condition is not None:
                out.append(_Guard(kind, stmt.condition.text, stmt.line, "fails",
                                  tuple(stmt.condition.identifiers), function.name))
        elif stmt.call is not None and stmt.call.kind == I.INTERNAL_CALL:
            nested = resolver.resolve(contract, stmt.call)
            out.extend(_helper_guards(nested, resolver, contract, depth - 1, seen))
    return out


def _essence(guard: _Guard, env: _Env) -> set[str]:
    """A guard's identifiers with caller-side temporaries replaced by their source."""
    out: set[str] = set()
    for name in guard.identifiers:
        derived = env.state_of.get(name)
        if derived:
            out |= derived
        else:
            out.add(name)
    return out


def _callee_recheck(guard: _Guard, callee, arg_texts, env: _Env, resolver):
    """The callee's own unconditional guard that covers everything `guard` reads.

    Identifiers are translated through the parameter list, so a callee that
    checks `_threshold <= ownerCount` covers a caller that checked
    `ownerCount - 1 >= _threshold` before passing `_threshold`.
    """
    if guard.kind == MODIFIER or callee is None or not callee.has_ir:
        return None
    wanted = _essence(guard, env)
    if not wanted:
        return None
    mapping: dict[str, set[str]] = {}
    for parameter, argument in zip(callee.params, arg_texts):
        mapping[parameter.name] = set(_IDENT.findall(argument))
    owner = resolver.by_name.get(callee.contract)
    for own in resolver.helper_guards(callee, owner, depth=2):
        translated: set[str] = set()
        for name in own.identifiers:
            translated |= mapping.get(name, {name})
        if wanted <= translated:
            return own
    return None


class _Walker:
    def __init__(self, contract, function, resolver):
        self.contract = contract
        self.function = function
        self.resolver = resolver
        self.sites: list[_Site] = []

    def run(self) -> list[_Site]:
        modifiers = tuple(
            _Guard(MODIFIER, m, self.function.line, "holds", (m,))
            for m in self.function.modifiers
            if not any(needle in m.lower() for needle in REENTRANCY_GUARDS)
        )
        self._walk(self.function.ir.statements, modifiers, [], _Env())
        return self.sites

    def _record(self, stmt, guards, env):
        call = stmt.call
        callee = _leaf_name(call.callee)
        if call.kind == I.BUILTIN_CALL or not callee:
            return
        if self.resolver.is_type(callee):
            return  # a cast such as `IBridge(bridge)`, not a call
        resolved = self.resolver.resolve(self.contract, call)
        if resolved is not None and resolved.mutability in {"view", "pure"}:
            return  # not a state transition
        identifiers: set[str] = set()
        for argument in call.arguments:
            identifiers.update(argument.identifiers)
        arg_texts = tuple(_normalize(a.text) for a in call.arguments)
        rechecked = frozenset(
            guard.key for guard in guards
            if _callee_recheck(guard, resolved, arg_texts, env, self.resolver)
        )
        self.sites.append(_Site(
            contract=self.contract.name,
            function=self.function.name,
            path=self.function.path or self.contract.path,
            line=call.line,
            language=getattr(self.function, "language", "solidity"),
            callee=callee,
            receiver=call.receiver,
            call_text=_normalize(call.text)[:160],
            arg_texts=arg_texts,
            arg_identifiers=frozenset(identifiers),
            guards=tuple(guards),
            env=env.copy(),
            resolved=resolved,
            rechecked=rechecked,
        ))

    def _learn(self, stmt, env: _Env) -> None:
        """Record what a local is derived from."""
        if stmt.kind not in {I.VAR_DECL, I.ASSIGN}:
            return
        local = _local_name(stmt.target)
        if not local:
            return
        state: set[str] = set(stmt.reads)
        value_ids: tuple[str, ...] = stmt.value.identifiers if stmt.value else ()
        for name in value_ids:
            state |= env.state_of.get(name, frozenset())
        if stmt.call is not None:
            for argument in stmt.call.arguments:
                for name in argument.identifiers:
                    state |= env.state_of.get(name, frozenset())
            target = self.resolver.resolve(self.contract, stmt.call)
            if target is not None:
                state |= set(target.reads) | set(target.writes)
            if stmt.call.receiver:
                env.receiver_of[local] = _normalize(stmt.call.receiver)
        env.state_of[local] = frozenset(state)

    def _walk(self, statements, enclosing, preceding, env: _Env) -> None:
        local = list(preceding)
        for stmt in statements:
            if stmt.kind == I.REQUIRE:
                if stmt.call is not None:
                    self._record(stmt, enclosing + tuple(local), env)
                text, ids = _condition_of(stmt)
                local.append(_Guard(REVERT, text, stmt.line, "holds", ids))
                continue
            if stmt.kind == I.IF:
                text, ids = _condition_of(stmt)
                holds = _Guard(BRANCH, text, stmt.line, "holds", ids)
                fails = _Guard(BRANCH, text, stmt.line, "fails", ids)
                self._walk(stmt.body, enclosing + (holds,), local, env.copy())
                self._walk(stmt.orelse, enclosing + (fails,), local, env.copy())
                body_kind = _block_terminates(stmt.body)
                else_kind = _block_terminates(stmt.orelse) if stmt.orelse else None
                if body_kind and not else_kind:
                    local.append(_Guard(body_kind, text, stmt.line, "fails", ids))
                elif else_kind and not body_kind:
                    local.append(_Guard(else_kind, text, stmt.line, "holds", ids))
                continue
            if stmt.call is not None:
                self._record(stmt, enclosing + tuple(local), env)
                if stmt.call.kind == I.INTERNAL_CALL and stmt.call.receiver is None:
                    helper = self.resolver.resolve(self.contract, stmt.call)
                    local.extend(self.resolver.helper_guards(helper, self.contract))
            self._learn(stmt, env)
            if stmt.body or stmt.orelse:
                self._walk(stmt.body, enclosing, local, env.copy())
                self._walk(stmt.orelse, enclosing, local, env.copy())


def _coupling(guard: _Guard, site: _Site) -> str | None:
    """How directly a guard speaks about the call it precedes."""
    if guard.kind == MODIFIER:
        return WEAK
    ids = set(guard.identifiers)
    if not ids:
        return None
    receiver = _normalize(site.receiver or "")
    if receiver:
        receiver_base = receiver.split(".")[0].split("[")[0]
        if receiver_base in ids:
            return STRONG
        if any(site.env.receiver_of.get(name) == receiver for name in ids):
            return STRONG
    if site.resolved is not None:
        touched = set(site.resolved.writes) | set(site.resolved.reads)
        if touched and touched & site.env.state_behind(ids):
            return STRONG
    if ids & site.arg_identifiers:
        return WEAK
    return None


def _coupling_note(guard: _Guard, site: _Site) -> str:
    receiver = _normalize(site.receiver or "")
    ids = set(guard.identifiers)
    if receiver:
        for name in guard.identifiers:
            if site.env.receiver_of.get(name) == receiver:
                return (f"`{name}` is read from `{receiver}`, the receiver the "
                        f"guarded call mutates")
        if receiver.split(".")[0].split("[")[0] in ids:
            return (f"the condition consults `{receiver}`, the receiver the "
                    f"guarded call mutates")
    if site.resolved is not None:
        touched = (set(site.resolved.writes) | set(site.resolved.reads))
        shared = sorted(touched & site.env.state_behind(ids))
        if shared:
            return (f"the condition depends on {', '.join(shared[:3])}, which "
                    f"{site.resolved.contract}.{site.resolved.name} touches")
    if guard.kind == MODIFIER:
        return "a function-level modifier"
    shared = sorted(ids & site.arg_identifiers)
    return f"the condition mentions {', '.join(shared[:3])}, passed to the call"


def _collect_guard_sites(contracts, resolver, include_tests=False) -> list[_Site]:
    sites: list[_Site] = []
    for contract in contracts:
        if getattr(contract, "is_test", False) and not include_tests:
            continue
        if contract.kind in {"interface", "library"}:
            continue
        for function in contract.functions:
            if function.is_test and not include_tests:
                continue
            if not function.is_entry_point or function.kind in {"modifier", "constructor"}:
                continue
            if function.mutability in {"view", "pure"} or not function.has_ir:
                continue
            if any(m.lower().split("(")[0] in ONE_SHOT_MODIFIERS
                   for m in function.modifiers):
                continue
            sites.extend(_Walker(contract, function, resolver).run())
    return sites


def _compare(first: _Site, second: _Site):
    """Return (guarded, bare, extras, reverse, strength) or None."""
    # The same transition means the same operands. When the arguments differ
    # the extra guard is usually about the differing operand (the other
    # flow's subject, the other flow's amount), which is a different
    # precondition rather than a forgotten one.
    if first.arg_texts != second.arg_texts:
        return None
    keys_first = {g.key for g in first.guards}
    keys_second = {g.key for g in second.guards}

    def extras(site, other_keys):
        strong, weak = [], []
        for guard in site.guards:
            if guard.key in other_keys or guard.key in site.rechecked:
                continue
            strength = _coupling(guard, site)
            if strength == STRONG:
                strong.append(guard)
            elif strength == WEAK:
                weak.append(guard)
        return strong, weak

    strong_first, weak_first = extras(first, keys_second)
    strong_second, weak_second = extras(second, keys_first)

    if strong_first and not strong_second:
        return (first, second, strong_first + weak_first,
                strong_second + weak_second, STRONG)
    if strong_second and not strong_first:
        return (second, first, strong_second + weak_second,
                strong_first + weak_first, STRONG)
    if strong_first or strong_second:
        # Each path checks something about the target: different
        # preconditions, not one path forgetting.
        return None
    # With only argument-level coupling the weak verdict needs a sibling that
    # carries nothing coupled at all: one path checks, the other is bare.
    if weak_first and not weak_second:
        return first, second, weak_first, [], WEAK
    if weak_second and not weak_first:
        return second, first, weak_second, [], WEAK
    return None


def _guard_asymmetries(sites: list[_Site]) -> list[dict]:
    groups: dict[tuple, list[_Site]] = {}
    for site in sites:
        groups.setdefault(site.group_key, []).append(site)

    reports: dict[tuple, dict] = {}
    for key, members in groups.items():
        if len({m.function for m in members}) < 2:
            continue
        for index, first in enumerate(members):
            for second in members[index + 1:]:
                if first.function == second.function:
                    continue
                verdict = _compare(first, second)
                if verdict is None:
                    continue
                guarded, bare, extras, reverse, strength = verdict
                report = reports.setdefault((bare.qualified, bare.line, key), {
                    "bare": bare, "guarded": [], "extras": [], "reverse": [],
                    "strength": WEAK, "total": len(members),
                })
                report["guarded"].append(guarded)
                for guard in extras:
                    if guard not in report["extras"]:
                        report["extras"].append(guard)
                for guard in reverse:
                    if guard not in report["reverse"]:
                        report["reverse"].append(guard)
                if strength == STRONG:
                    report["strength"] = STRONG
    return list(reports.values())


def _guard_signals(contracts, include_tests=False) -> list[DetectorSignal]:
    resolver = _Resolver(contracts)
    sites = _collect_guard_sites(contracts, resolver, include_tests)
    signals: list[DetectorSignal] = []
    for report in _guard_asymmetries(sites):
        bare: _Site = report["bare"]
        guarded: list[_Site] = report["guarded"]
        extras: list[_Guard] = report["extras"]
        strength = report["strength"]
        sibling = guarded[0]
        lead = next(
            (g for g in extras if _coupling(g, sibling) == STRONG), extras[0]
        )
        confidence = 0.70 if strength == STRONG else 0.58
        if bare.callee in VALUE_OPERATIONS:
            confidence += 0.04
        if bare.resolved is not None and bare.resolved.writes:
            confidence += 0.04
        if any(g.kind in {REVERT, EXIT} for g in extras):
            confidence += 0.03
        confidence = round(min(0.85, confidence), 3)

        target = bare.call_text
        evidence = [
            f"shared transition: {target}",
            f"authorization guard present in {len(guarded)}/{report['total']} "
            f"sites reaching {bare.callee}",
        ]
        for site in guarded[:3]:
            evidence.append(
                f"  guarded site: {site.qualified} ({site.path}:{site.line})"
            )
        evidence.append(
            f"  unguarded site: {bare.qualified} ({bare.path}:{bare.line})"
        )
        for guard in extras[:4]:
            evidence.append(
                f"guard on {sibling.function} path, absent on {bare.function} "
                f"path: {guard.describe()}"
            )
        evidence.append(f"coupling: {_coupling_note(lead, sibling)}")
        evidence.append(
            "same arguments at both sites: " + ", ".join(bare.arg_texts)
        )
        if report["reverse"]:
            evidence.append(
                f"conditions on the {bare.function} path that "
                f"{sibling.function} does not place on the same call: "
                + "; ".join(g.describe() for g in report["reverse"][:3])
            )
        own = [g for g in bare.guards if g.kind != MODIFIER]
        evidence.append(
            f"conditions on the {bare.function} path before the call: "
            + ("; ".join(g.describe() for g in own[:4]) if own else "none")
        )

        ordered_trace = [f"L{g.line} {g.kind}: {g.text}" for g in own[:4]]
        ordered_trace.append(f"L{bare.line} call: {target}")
        ordered_trace.append(
            f"  expected (from {sibling.qualified}): {lead.describe()}"
        )

        falsification = [
            f"Does {bare.qualified} establish `{lead.text}` by another means "
            f"(a helper the parser did not inline, an inherited modifier)?",
            f"Does {bare.callee} re-check the condition itself, so the guard on "
            f"{sibling.function} is redundant rather than protective?",
            f"Is the extra guard specific to {sibling.function}'s flow rather "
            f"than to the transition (different preconditions by design)?",
            f"Can {bare.qualified} be reached in a state where "
            f"{sibling.function} would have reverted?",
        ]

        signals.append(signal(
            detector=DETECTOR,
            title=(
                f"{bare.callee} without an authorization guard in "
                f"{bare.qualified} (guarded in {sibling.function})"
            ),
            function=_Anchor(bare.contract, bare.function, bare.line, bare.path,
                             bare.language),
            line=bare.line,
            confidence=confidence,
            reason=(
                f"{bare.qualified} reaches {target} with no revocable condition "
                f"that {sibling.qualified} places on the same call: "
                f"`{lead.text}` ({lead.describe()})"
            ),
            evidence=evidence,
            ordered_trace=ordered_trace,
            falsification=falsification,
        ))
    return signals


# ---------------------------------------------------------------------------
# Companion shape
# ---------------------------------------------------------------------------

@dataclass
class _CallSite:
    function: str
    contract: str
    line: int
    path: str
    callee: str
    companions: set[str] = field(default_factory=set)
    is_test: bool = False
    is_genesis: bool = False
    language: str = "rust"


def _collect_call_sites(contracts, include_tests: bool = False):
    """Find all call sites of value operations with their companion calls."""
    sites: list[_CallSite] = []

    for contract in contracts:
        if contract.is_test and not include_tests:
            continue

        for function in contract.functions:
            if function.is_test and not include_tests:
                continue

            callees: set[str] = set()
            value_calls: list[tuple[str, int]] = []

            if function.ir:
                for stmt in function.ir.walk():
                    if stmt.call is not None:
                        callee = _leaf_name(stmt.call.callee)
                        callees.add(callee)
                        if callee in VALUE_OPERATIONS:
                            value_calls.append((callee, stmt.call.line))
            else:
                for call_name in function.calls:
                    base = _leaf_name(call_name)
                    callees.add(base)
                    if base in VALUE_OPERATIONS:
                        value_calls.append((base, function.line))

            is_genesis = bool(GENESIS_HINTS.search(function.name))

            for callee, line in value_calls:
                companions = callees - {callee} - VALUE_OPERATIONS
                sites.append(_CallSite(
                    function=f"{contract.name}.{function.name}",
                    contract=contract.name,
                    line=line,
                    path=function.path or contract.path,
                    callee=callee,
                    companions=companions,
                    is_test=function.is_test,
                    is_genesis=is_genesis,
                    language=getattr(function, "language", "rust"),
                ))

    return sites


def _find_asymmetries(sites: list[_CallSite]):
    """Find companion functions that appear in most but not all sites."""
    by_operation: dict[str, list[_CallSite]] = {}
    for site in sites:
        by_operation.setdefault(site.callee, []).append(site)

    asymmetries: list[dict] = []

    for operation, op_sites in by_operation.items():
        production_sites = [
            s for s in op_sites if not s.is_test and not s.is_genesis
        ]
        if len(production_sites) < 2:
            continue

        companion_counts: dict[str, int] = {}
        for site in production_sites:
            for comp in site.companions:
                companion_counts[comp] = companion_counts.get(comp, 0) + 1

        total = len(production_sites)
        for companion, count in companion_counts.items():
            # A companion seen once out of many is noise, not a convention.
            # With exactly two equivalent sites, "one does, one does not" is
            # the whole signal.
            if count < 2 and total > 2:
                continue
            if count >= total:
                continue

            with_companion = [s for s in production_sites if companion in s.companions]
            without_companion = [s for s in production_sites if companion not in s.companions]
            if not without_companion:
                continue

            asymmetries.append({
                "operation": operation,
                "companion": companion,
                "with": with_companion,
                "without": without_companion,
                "confidence": round(min(0.85, count / total), 3),
                "total": total,
                "count": count,
            })

    return asymmetries


def _companion_signals(contracts, include_tests=False) -> list[DetectorSignal]:
    sites = _collect_call_sites(contracts, include_tests=include_tests)
    signals: list[DetectorSignal] = []

    for asym in _find_asymmetries(sites):
        label = asym["companion"]
        for missing_site in asym["without"]:
            evidence = [
                f"primary operation: {asym['operation']}",
                f"expected companion: {label}",
                f"companion present in {asym['count']}/{asym['total']} sites",
            ]
            for ws in asym["with"][:3]:
                evidence.append(
                    f"  companion present: {ws.function} ({ws.path}:{ws.line})"
                )
            for wos in asym["without"][:3]:
                evidence.append(
                    f"  companion MISSING: {wos.function} ({wos.path}:{wos.line})"
                )

            observed_instead = sorted(
                missing_site.companions - {asym["companion"]}
            )[:3]
            evidence.append(
                "observed instead: "
                + (", ".join(observed_instead) if observed_instead else "nothing")
            )

            ordered_trace = [
                f"L{missing_site.line} call: {asym['operation']}",
                f"  expected: {asym['companion']} (present in "
                f"{asym['count']}/{asym['total']} equivalent sites)",
                f"  actual: {'none' if not observed_instead else ', '.join(observed_instead)}",
            ]

            falsification = [
                f"Is there a sweep/backfill that applies {label} retroactively?",
                f"Is {label} enforced elsewhere on the same path?",
                "Is the missing side-effect intentional (documented exception)?",
                f"Does {asym['operation']} have a different accounting path in this context?",
            ]

            signals.append(signal(
                detector=DETECTOR,
                title=(
                    f"{asym['operation']} without {label} "
                    f"in {missing_site.function}"
                ),
                function=_Anchor(missing_site.contract,
                                 missing_site.function.split(".")[-1],
                                 missing_site.line, missing_site.path,
                                 missing_site.language),
                line=missing_site.line,
                confidence=asym["confidence"],
                reason=(
                    f"{missing_site.function} calls {asym['operation']} without "
                    f"{label}, which "
                    f"{asym['count']}/{asym['total']} equivalent sites include"
                ),
                evidence=evidence,
                ordered_trace=ordered_trace,
                falsification=falsification,
            ))
    return signals


def detect(
    contracts,
    engine=None,
    include_tests: bool = False,
    **kwargs,
) -> list[DetectorSignal]:
    """Run both shapes of the asymmetric side-effect detector."""
    signals = _companion_signals(contracts, include_tests)
    signals.extend(_guard_signals(contracts, include_tests))
    return sorted(signals, key=lambda s: (-s.confidence, s.contract, s.function, s.line))
