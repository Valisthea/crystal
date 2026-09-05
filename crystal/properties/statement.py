"""From a campaign invariant's statement to a property form, or a refusal.

The grammar is small on purpose. It recognises the shapes an operator writes
when the invariant is meant to be executed, binds every name in them to the
parsed target, and refuses — with the fragment quoted — whatever it cannot
bind. Recognised shapes (case-insensitive, punctuation-tolerant):

    Relation     <terms> (equals | is at least | is at most | == | >= | <=) <terms>
                 term := sum(X) | the sum of X | X | the contract ether balance
                         | balance(Contract)
    Exclusion    for one KEY, at most one of F and G can succeed
    Replay       no KEY reaches VALUE twice [and F cannot run twice for one KEY]
    Independence <settlement> never depends on <state>   (category: settlement)

`X` and `F` must name a state variable or an entry point of a scoped
contract; `KEY` must be a parameter of every function it keys; `VALUE` must be
a member of the enum the mapping stores. Nothing is inferred from the prose
beyond these bindings, and each one is written into the property's notes.
"""

from __future__ import annotations

import re

from .ir import Exclusion, Independence, Relation, Replay, Term, Unsupported
from .model import Resolver

RELATION_RE = re.compile(
    r"^(?P<lhs>.+?)\s+(?P<op>equals|is equal to|==|is at least|>=|is at most|<=)\s+(?P<rhs>.+?)$",
    re.I,
)
SUM_RE = re.compile(r"^(?:the\s+)?sum\s*(?:\(\s*(?P<a>\w+)\s*\)|of\s+(?P<b>\w+))$", re.I)
BALANCE_RE = re.compile(
    r"^(?:(?:the\s+)?contract(?:'s)?\s+(?:ether\s+)?balance|address\(this\)\.balance"
    r"|balance\s*\(\s*(?P<c>\w+)\s*\))$",
    re.I,
)
EXCLUSION_RE = re.compile(
    r"^(?:for\s+(?:one|a|any|each)\s+(?P<key>\w+)\s*,\s*)?at\s+most\s+one\s+of\s+"
    r"(?P<fns>.+?)\s+can\s+succeed$",
    re.I,
)
REACH_RE = re.compile(r"^no\s+(?P<key>\w+)\s+reaches\s+(?P<value>\w+)\s+twice$", re.I)
ONCE_RE = re.compile(
    r"^(?P<fn>[\w.]+)\s+cannot\s+(?:run|succeed|be\s+called)\s+twice\s+for\s+(?:one|a|the\s+same)\s+(?P<key>\w+)$",
    re.I,
)
INDEPENDENCE_RE = re.compile(r"^(?P<subject>.+?)\s+never\s+depends\s+on\s+(?P<condition>.+)$", re.I)

OPS = {
    "equals": "==", "is equal to": "==", "==": "==",
    "is at least": ">=", ">=": ">=",
    "is at most": "<=", "<=": "<=",
}


def _normalise(text: str) -> str:
    text = (text or "").strip().rstrip(".").strip()
    return re.sub(r"\s+", " ", text)


def _split_terms(side: str) -> list[str]:
    return [part.strip() for part in re.split(r"\s+\+\s+|\s+plus\s+", side) if part.strip()]


class Parsed:
    """A parsed invariant: the form, plus how each name was bound."""

    def __init__(self, form, notes):
        self.form = form
        self.notes = tuple(notes)


def parse_invariant(invariant, resolver: Resolver) -> Parsed:
    statement = _normalise(invariant.statement)
    category = (invariant.category or "").lower()
    notes: list[str] = []

    if category == "settlement" and INDEPENDENCE_RE.match(statement) and " depends on " in statement:
        return _independence(statement, invariant, resolver, notes)

    match = EXCLUSION_RE.match(statement)
    if match:
        return _exclusion(match, invariant, resolver, notes)

    if category == "replay" or REACH_RE.match(statement) or ONCE_RE.match(statement):
        return _replay(statement, invariant, resolver, notes)

    match = RELATION_RE.match(statement)
    if match:
        return _relation(match, invariant, resolver, notes)

    return Parsed(Unsupported(
        f"statement form not recognised: `{statement}`; recognised forms are a "
        f"relation between sums/scalars/balances, `at most one of F and G can "
        f"succeed`, `no KEY reaches VALUE twice`, and `... never depends on ...` "
        f"(category settlement)"
    ), notes)


# ── Relation ─────────────────────────────────────────────────────────────

def _relation(match, invariant, resolver, notes) -> Parsed:
    op = OPS[match.group("op").lower()]
    policy = (invariant.policy or "exact").lower()
    if policy == "exact" and op != "==":
        notes.append(f"policy `exact` but the statement says `{op}`; the statement wins")
    if policy == "lower_bound" and op == "==":
        notes.append("policy `lower_bound` but the statement says `equals`; the statement wins")

    sides = []
    for side in (match.group("lhs"), match.group("rhs")):
        terms = []
        for text in _split_terms(side):
            term = _term(text, invariant, resolver)
            if isinstance(term, Unsupported):
                return Parsed(term, notes)
            terms.append(term)
        sides.append(tuple(terms))

    contracts = {t.state.contract for side in sides for t in side if t.state is not None}
    if len(contracts) > 1:
        return Parsed(Unsupported(
            f"relation mixes state of {sorted(contracts)}; a conservation relation is "
            f"checked against one contract's balance and Crystal will not pick which"
        ), notes)
    contract = next(iter(contracts), "")
    resolved = []
    for side in sides:
        fixed = []
        for term in side:
            if term.kind == "balance" and not term.contract:
                if not contract:
                    return Parsed(Unsupported(
                        "`the contract balance` names no contract and no state term "
                        "fixes one; write balance(Contract)"
                    ), notes)
                term = Term("balance", term.text, None, contract)
                notes.append(f"`{term.text}` bound to balance({contract})")
            fixed.append(term)
        resolved.append(tuple(fixed))

    for side in resolved:
        for term in side:
            if term.state is not None and term.state.is_mapping:
                problems = resolver.key_problems(resolver.by_name[term.state.contract], term.state.name)
                if problems:
                    return Parsed(Unsupported(
                        f"sum({term.state.qualified}) needs an enumerable holder set, and "
                        f"a key is computed rather than supplied by a caller: "
                        + "; ".join(problems[:3])
                    ), notes)
                notes.append(
                    f"sum({term.state.qualified}): every write site keys on a caller-supplied "
                    f"identifier, so the harness's recorded inputs enumerate the holders"
                )
    return Parsed(Relation(resolved[0], op, resolved[1]), notes)


def _term(text: str, invariant, resolver) -> Term | Unsupported:
    match = SUM_RE.match(text)
    if match:
        name = match.group("a") or match.group("b")
        state = _bind_state(name, invariant, resolver)
        if isinstance(state, Unsupported):
            return state
        if not state.is_mapping:
            return Unsupported(f"`{text}` sums {state.qualified}, which is not a mapping")
        if state.key_types != ("address",):
            return Unsupported(
                f"`{text}` sums {state.qualified} keyed by {','.join(state.key_types)}; "
                f"only address-keyed mappings have a holder set the harness can enumerate"
            )
        return Term("sum", text, state)
    match = BALANCE_RE.match(text)
    if match:
        contract = match.group("c") or ""
        if contract and contract not in resolver.by_name:
            return Unsupported(f"`{text}` names contract `{contract}`, which is not in the parsed set")
        return Term("balance", text, None, contract)
    if re.fullmatch(r"\w+", text):
        state = _bind_state(text, invariant, resolver)
        if isinstance(state, Unsupported):
            return state
        if state.is_mapping:
            return Unsupported(
                f"`{text}` is a mapping ({state.type_name}); write sum({text}) to aggregate it"
            )
        return Term("scalar", text, state)
    return Unsupported(
        f"term `{text}` names no state variable of the scoped contracts and is not a "
        f"sum, a scalar or a balance; Crystal will not infer the arithmetic that defines it"
    )


def _bind_state(name: str, invariant, resolver):
    found = resolver.state(name)
    if not found:
        return Unsupported(
            f"`{name}` is not a state variable of any scoped contract"
            + ("" if name in invariant.affected_state else
               f" (and is not listed in affected_state {invariant.affected_state})")
        )
    if len(found) > 1:
        return Unsupported(
            f"`{name}` is declared by several contracts "
            f"({', '.join(s.contract for s in found)}); qualify it as Contract::{name}"
        )
    state = found[0]
    if not state.getter:
        return Unsupported(
            f"{state.qualified} is {state.type_name} with no public getter and no view "
            f"function that reads only it; the harness cannot read it and Crystal will "
            f"not read raw storage for a slot it did not derive"
        )
    return state


# ── Exclusion ────────────────────────────────────────────────────────────

def _exclusion(match, invariant, resolver, notes) -> Parsed:
    key = match.group("key")
    names = [n.strip() for n in re.split(r"\s*,\s*|\s+and\s+|\s+or\s+", match.group("fns")) if n.strip()]
    if not key:
        return Parsed(Unsupported(
            "exclusion names no key (`for one KEY, at most one of ...`)"
        ), notes)
    functions = []
    for name in names:
        bound = _bind_function(name, resolver)
        if isinstance(bound, Unsupported):
            return Parsed(bound, notes)
        contract, function = bound
        if key not in {p.name for p in function.params}:
            return Parsed(Unsupported(
                f"`{key}` is not a parameter of {contract.name}.{function.name}"
                f"({', '.join(str(p) for p in function.params)})"
            ), notes)
        functions.append(resolver.function_ref(contract, function))
        notes.append(f"`{name}` bound to {contract.name}.{function.name}")
    if len(functions) < 2:
        return Parsed(Unsupported("exclusion needs at least two entry points"), notes)
    return Parsed(Exclusion(key, tuple(functions)), notes)


def _bind_function(name: str, resolver):
    found = resolver.find_entry_point(name)
    if not found:
        return Unsupported(f"`{name}` is not an entry point of any scoped contract")
    if len(found) > 1:
        return Unsupported(
            f"`{name}` is an entry point of several contracts "
            f"({', '.join(c.name for c, _ in found)}); qualify it as Contract.{name}"
        )
    return found[0]


# ── Replay ───────────────────────────────────────────────────────────────

def _unify_key(key: str, other: str, resolver, contract, function, notes) -> str | Unsupported:
    """Two clauses may key on the same identity in two representations: a
    struct parameter and the hash the contract derives from it. Accept that
    only when the contract itself exposes the derivation as a view function
    (`hashPegInQuote(quote)`); otherwise the two keys are two keys."""
    if key == other:
        return key
    params = {p.name: p for p in function.params}
    struct_param = params.get(other) or params.get(key)
    if struct_param is None or resolver.struct_fields(struct_param.type_name, contract) is None:
        return Unsupported(f"replay clauses key on both `{key}` and `{other}`")
    derived = key if struct_param.name == other else other
    derivation = resolver.key_derivation(contract, function, derived)
    if derivation is None:
        return Unsupported(
            f"replay clauses key on both `{key}` and `{other}`, and {contract.name}."
            f"{function.name} exposes no view function deriving `{derived}` from "
            f"`{struct_param.name}`"
        )
    notes.append(
        f"`{struct_param.name}` and `{derived}` are one identity: {contract.name}."
        f"{derivation}({struct_param.type_name}) derives `{derived}`"
    )
    return derived


def _replay(statement: str, invariant, resolver, notes) -> Parsed:
    clauses = [c.strip() for c in re.split(r"\s+and\s+", statement) if c.strip()]
    key = ""
    once = []
    reach = None
    reach_value = ""
    reach_writers = ()
    for clause in clauses:
        match = REACH_RE.match(clause)
        if match:
            key = key or match.group("key")
            if match.group("key") != key:
                return Parsed(Unsupported(f"replay clauses key on both `{key}` and `{match.group('key')}`"), notes)
            value = match.group("value")
            found = None
            for name in invariant.affected_state:
                for state in resolver.state(name):
                    members = resolver.enum_members(state.value_type, resolver.by_name.get(state.contract))
                    if members and value in members:
                        found = state
            if found is None:
                return Parsed(Unsupported(
                    f"`{value}` is not a member of any enum stored by "
                    f"{invariant.affected_state}"
                ), notes)
            if not found.getter:
                return Parsed(Unsupported(
                    f"{found.qualified} has no public accessor; the harness cannot read it"
                ), notes)
            contract = resolver.by_name[found.contract]
            writers = resolver.value_writers(contract, found.name, value)
            if not writers:
                return Parsed(Unsupported(
                    f"no entry point of {found.contract} assigns {value} to {found.name}"
                ), notes)
            for writer in writers:
                if key not in {p.name for p in writer.params}:
                    params = [p.name for p in writer.params]
                    struct_keys = [p for p in writer.params if resolver.struct_fields(p.type_name, contract)]
                    if not struct_keys:
                        return Parsed(Unsupported(
                            f"`{key}` is neither a parameter of {found.contract}.{writer.name}"
                            f"({', '.join(params)}) nor derivable from one"
                        ), notes)
                    notes.append(
                        f"{found.contract}.{writer.name} derives `{key}` inside the contract "
                        f"from {struct_keys[0].type_name} {struct_keys[0].name}; the harness "
                        f"must obtain it through the recipe that builds the call"
                    )
            reach = found
            reach_value = value
            reach_writers = tuple(resolver.function_ref(contract, w) for w in writers)
            notes.append(
                f"`{value}` bound to {found.qualified} ({found.value_type}); assigned by "
                + ", ".join(w.name for w in writers)
            )
            continue
        match = ONCE_RE.match(clause)
        if match:
            bound = _bind_function(match.group("fn"), resolver)
            if isinstance(bound, Unsupported):
                return Parsed(bound, notes)
            contract, function = bound
            if not key:
                key = match.group("key")
            else:
                unified = _unify_key(key, match.group("key"), resolver, contract, function, notes)
                if isinstance(unified, Unsupported):
                    return Parsed(unified, notes)
                key = unified
            if key not in {p.name for p in function.params}:
                struct_keys = [p for p in function.params if resolver.struct_fields(p.type_name, contract)]
                if not struct_keys:
                    return Parsed(Unsupported(
                        f"`{key}` is neither a parameter of {contract.name}.{function.name} "
                        f"nor derivable from one"
                    ), notes)
                notes.append(
                    f"{contract.name}.{function.name} derives `{key}` inside the contract "
                    f"from {struct_keys[0].type_name} {struct_keys[0].name}; the harness "
                    f"must obtain it through the recipe that builds the call"
                )
            once.append(resolver.function_ref(contract, function))
            notes.append(f"`{match.group('fn')}` bound to {contract.name}.{function.name}")
            continue
        return Parsed(Unsupported(
            f"replay clause not recognised: `{clause}`; expected `no KEY reaches VALUE "
            f"twice` or `F cannot run twice for one KEY`"
        ), notes)
    if not once and reach is None:
        return Parsed(Unsupported("replay statement bound nothing"), notes)
    return Parsed(Replay(key, tuple(once), reach, reach_value, reach_writers), notes)


# ── Independence ─────────────────────────────────────────────────────────

def _independence(statement: str, invariant, resolver, notes) -> Parsed:
    match = INDEPENDENCE_RE.match(statement)
    subject_text, condition_text = match.group("subject"), match.group("condition")
    states = []
    for name in invariant.affected_state:
        found = resolver.state(name)
        if len(found) != 1:
            return Parsed(Unsupported(
                f"`{name}` binds to {len(found)} state variables; independence needs exactly one"
            ), notes)
        states.append(found[0])
    if len(states) != 2:
        return Parsed(Unsupported(
            f"independence needs exactly two affected states (the settlement registry and "
            f"the state it must not depend on); got {invariant.affected_state}"
        ), notes)

    # The subject state is written by entry points of its own contract; the
    # condition state lives elsewhere and is only reached through bound calls.
    candidates = []
    for subject, condition in ((states[0], states[1]), (states[1], states[0])):
        if subject.contract == condition.contract:
            continue
        contract = resolver.by_name[subject.contract]
        writers = resolver.entry_writers(contract, subject.name)
        settlement = [
            f for f in writers
            if resolver.reaches_writer_of(contract, f, condition)
        ]
        if settlement:
            candidates.append((subject, condition, contract, settlement))
    if len(candidates) != 1:
        return Parsed(Unsupported(
            f"could not tell the settlement state from the accounting state among "
            f"{[s.qualified for s in states]}: expected exactly one to be written by entry "
            f"points that also reach, through a bound call, a writer of the other"
        ), notes)
    subject, condition, contract, settlement = candidates[0]
    notes.append(
        f"`{subject_text}` bound to entry points of {contract.name} that write "
        f"{subject.qualified} and perform accounting on {condition.qualified} through a "
        f"bound call: " + ", ".join(f.name for f in settlement)
    )
    notes.append(f"`{condition_text}` bound to {condition.qualified}")
    unbound = re.sub(r"^\s*settlement\s*", "", subject_text, flags=re.I).strip()
    if unbound:
        notes.append(
            f"qualifier `{unbound}` is not bound to code; the property is checked for every "
            f"settlement entry point above"
        )
    if not condition.getter:
        return Parsed(Unsupported(
            f"{condition.qualified} has no public accessor; the harness cannot observe it"
        ), notes)

    guards = []
    arithmetic = False
    for function in settlement:
        found, problems = resolver.state_guards(contract, function, condition)
        if problems:
            return Parsed(Unsupported("; ".join(problems)), notes)
        guards.extend(found)
        arithmetic = arithmetic or resolver.bound_writer_arithmetic(contract, function, condition)
        if found:
            notes.append(
                f"{contract.name}.{function.name}: revert(s) conditioned on "
                f"{condition.qualified}: " + "; ".join(g.describe() for g in found)
            )
        else:
            notes.append(
                f"{contract.name}.{function.name}: no revert is conditioned on "
                f"{condition.qualified} (static)"
            )
    if arithmetic:
        notes.append(
            f"a bound writer applies checked arithmetic to {condition.qualified}; a "
            f"Panic(0x11) surfacing from a settlement call is treated as state-dependent"
        )
    return Parsed(Independence(
        tuple(resolver.function_ref(contract, f) for f in settlement),
        condition, tuple(guards), arithmetic,
    ), notes)
