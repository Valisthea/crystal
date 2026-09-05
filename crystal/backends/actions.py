"""What a property is about, and which entry points can touch it.

A witness has to show that a transition *mutating the state the property is
about* actually succeeded. Two questions have to be answered from the parsed
model before any tool runs:

* which state variables the property reads (`property_subject`), and
* which externally callable functions write them (`mutating_actions`).

The second one cannot be read off `Function.writes` alone: on real code an
entry point usually delegates to an internal helper (`addPegInCollateralTo`
calls `_addPegInCollateralTo`, which is where `_pegInCollateral` changes), so
writes are followed through internal calls. Every helper here is duck-typed
against the property shape the compiler hands over (`name`/`expression`/
`subject`, or `filename`/`source`/`invariant`) so the two halves meet at merge
without either knowing the other's classes.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..naming import bare_name

MAX_CALL_DEPTH = 4

_TARGET_GETTER_RE = re.compile(r"\btarget\.([A-Za-z_]\w*)\s*\(")
_QUALIFIED_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]*)\.([A-Za-z_]\w*)\b")
_IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")
_BLOCK_ENV_RE = re.compile(r"\b(block\.number|block\.timestamp|now)\b")
_BLOCK_CLAUSE_RE = re.compile(
    r"[^;{}]*\b(?:block\.number|block\.timestamp|now)\b[^;{}]*"
)
_TOKEN_PULL_RE = re.compile(r"\b(safeTransferFrom|transferFrom)\s*\(")

FUZZABLE_TYPE_RE = re.compile(
    r"^(address|bool|string|bytes|bytes(?:[1-9]|[12]\d|3[0-2])|u?int(?:\d+)?)(\[\])?$"
)


# -- Property shape ------------------------------------------------------------

def property_name(prop) -> str:
    """`name`, else the stem of `filename`, else the class name — never empty."""
    name = getattr(prop, "name", "") or ""
    if name:
        return str(name)
    filename = getattr(prop, "filename", "") or ""
    if filename:
        return Path(str(filename)).stem
    return type(prop).__name__


def property_text(prop) -> str:
    """Everything the property says, so the subject can be read out of it."""
    pieces = []
    for attribute in ("expression", "source", "subject", "origin"):
        value = getattr(prop, attribute, "")
        if value:
            pieces.append(str(value))
    invariant = getattr(prop, "invariant", None)
    if invariant is not None:
        for attribute in ("expression", "subject", "variables", "state"):
            value = getattr(invariant, attribute, "")
            if value:
                pieces.append(" ".join(value) if isinstance(value, (list, tuple, set)) else str(value))
    return "\n".join(pieces)


def property_subject(prop, contract) -> tuple[str, ...]:
    """State variables of `contract` the property is about, bare names, in order.

    Reads `target.<getter>()` calls, `<Contract>.<name>` references and bare
    identifiers, keeping only those that name a state variable of the contract
    (inherited ones included once inheritance is linked). Empty means Crystal
    could not tie the property to any state — and a property tied to nothing
    can never be HELD, because no transition can be shown to mutate it.
    """
    declared = {bare_name(variable.name) for variable in contract.state_vars}
    if not declared:
        return ()
    explicit = getattr(prop, "subject", "") or ""
    found: list[str] = []
    if explicit:
        found.extend(
            bare_name(item) for item in str(explicit).replace(",", " ").split()
        )
    text = property_text(prop)
    found.extend(_TARGET_GETTER_RE.findall(text))
    found.extend(
        name for owner, name in _QUALIFIED_RE.findall(text) if owner == contract.name
    )
    if not found:
        found.extend(token for token in _IDENTIFIER_RE.findall(text) if token in declared)
    return tuple(dict.fromkeys(name for name in found if name in declared))


# -- Contract model --------------------------------------------------------------

def entry_points(contract) -> list:
    """Externally callable, state-changing functions — the fuzzer's alphabet."""
    return [
        function for function in contract.functions
        if function.is_entry_point
        and function.kind not in {"constructor", "modifier", "receive", "fallback"}
        and function.mutability not in {"view", "pure"}
        and function.name
    ]


def _function_index(contract, contracts=()) -> dict[str, list]:
    """Name -> functions, over the contract and every base Crystal parsed."""
    index: dict[str, list] = {}
    seen: set[str] = set()
    queue = [contract]
    by_name = {candidate.name: candidate for candidate in contracts}
    while queue:
        current = queue.pop(0)
        if current.name in seen:
            continue
        seen.add(current.name)
        for function in current.functions:
            index.setdefault(function.name, []).append(function)
        for base in getattr(current, "bases", ()) or ():
            if base in by_name and base not in seen:
                queue.append(by_name[base])
    return index


def _textual_writes(body: str, candidates) -> set[str]:
    """Assignments the parser may have missed: `name[...] += x`, `name = y`."""
    found = set()
    for name in candidates:
        pattern = rf"\b{re.escape(name)}\b(?:\s*\[[^\]]*\])*(?:\.\w+)*\s*(?:[+\-*/%|&^]|<<|>>)?=(?!=)"
        if re.search(pattern, body or ""):
            found.add(name)
    return found


_CALL_RE = re.compile(r"(?<![\w.])([A-Za-z_]\w*)\s*\(")


def _callees(function, index) -> list[str]:
    """Names the function calls: the parser's list, plus a textual scan of the
    body for `name(` against the contract's own functions — the regex front-end
    records no calls, and an entry point that delegates to `_credit()` would
    otherwise appear to write nothing."""
    names = [
        callee.rsplit(".", 1)[-1].rsplit("::", 1)[-1]
        for callee in (getattr(function, "calls", ()) or ())
    ]
    for name in _CALL_RE.findall(function.body or ""):
        if name in index and name != function.name:
            names.append(name)
    return list(dict.fromkeys(names))


def _walk_calls(function, index, depth, seen):
    """Yield the function and every internal callee reachable from it."""
    key = (function.contract, function.name, function.line)
    if key in seen or depth > MAX_CALL_DEPTH:
        return
    seen.add(key)
    yield function
    for name in _callees(function, index):
        for candidate in index.get(name, ()):
            if candidate.kind in {"constructor", "modifier"}:
                continue
            yield from _walk_calls(candidate, index, depth + 1, seen)


def transitive_writes(contract, function, contracts=()) -> set[str]:
    """State written by `function` or any internal call it makes, bare names."""
    declared = {bare_name(variable.name) for variable in contract.state_vars}
    index = _function_index(contract, contracts)
    written: set[str] = set()
    for reached in _walk_calls(function, index, 0, set()):
        written.update(bare_name(name) for name in reached.writes)
        written.update(_textual_writes(reached.body, declared))
    return {name for name in written if name in declared}


def transitive_body(contract, function, contracts=()) -> str:
    """Source of `function` plus every internal callee, for textual checks."""
    index = _function_index(contract, contracts)
    return "\n".join(
        reached.body or "" for reached in _walk_calls(function, index, 0, set())
    )


def mutating_actions(contract, subject, contracts=()) -> list:
    """Entry points whose transitive writes touch any variable in `subject`."""
    wanted = {bare_name(name) for name in subject}
    if not wanted:
        return []
    return [
        function for function in entry_points(contract)
        if transitive_writes(contract, function, contracts) & wanted
    ]


def block_dependence(contract, function, contracts=()) -> tuple[str, ...]:
    """The clauses through which `function` reads the block environment.

    A branch behind `block.number - since < delay` is unreachable on a backend
    that pins the block, so the clause is returned verbatim for the report.
    """
    body = transitive_body(contract, function, contracts)
    if not _BLOCK_ENV_RE.search(body):
        return ()
    clauses = []
    for match in _BLOCK_CLAUSE_RE.finditer(body):
        clause = " ".join(match.group(0).split())
        if clause and clause not in clauses:
            clauses.append(clause)
    return tuple(clauses[:4])


def needs_value(contract, function, contracts=()) -> bool:
    """True when the transition needs native value to make progress."""
    if getattr(function, "payable", False) or function.mutability == "payable":
        return True
    return "msg.value" in transitive_body(contract, function, contracts)


def pulls_tokens(contract, function, contracts=()) -> bool:
    """True when the transition pulls ERC-20 balances the harness never mints."""
    return bool(_TOKEN_PULL_RE.search(transitive_body(contract, function, contracts)))


def fuzzable_parameters(function) -> list[tuple[str, str]] | None:
    """`(type, name)` pairs when every parameter is an elementary ABI type or a
    dynamic array of one; None when a struct, enum or nested array would force
    Crystal to fabricate an encoding."""
    parameters = []
    for index, parameter in enumerate(function.params):
        cleaned = re.sub(r"\b(memory|calldata|storage|payable)\b", "", parameter.type_name or "")
        cleaned = re.sub(r"\s+", "", cleaned)
        match = FUZZABLE_TYPE_RE.match(cleaned)
        if match is None:
            return None
        base, array = match.group(1), match.group(2) or ""
        if base == "uint":
            base = "uint256"
        elif base == "int":
            base = "int256"
        location = " memory" if base in {"string", "bytes"} or array else ""
        parameters.append((f"{base}{array}{location}", parameter.name or f"arg{index}"))
    return parameters
