"""Inheritance resolution.

v1 emitted one edge per declared base. v2 linearizes the hierarchy, resolves
which function body actually wins, flags shadowed state variables, and can push
inherited state into derived contracts so that reads/writes of a base variable
are attributed to the contract that actually exposes them.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class InheritanceEdge:
    child: str
    parent: str
    kind: str = "inherits"
    depth: int = 1
    resolved: bool = True


@dataclass
class ContractResolution:
    contract: str
    linearization: list[str] = field(default_factory=list)
    inherited_state: list[str] = field(default_factory=list)
    shadowed_state: list[str] = field(default_factory=list)
    overrides: list[tuple[str, str]] = field(default_factory=list)
    unresolved_bases: list[str] = field(default_factory=list)


def build_inheritance_graph(contracts) -> list[InheritanceEdge]:
    known = {contract.name for contract in contracts}
    edges: list[InheritanceEdge] = []
    for contract in contracts:
        for base in contract.bases:
            edges.append(InheritanceEdge(
                contract.name, base, "inherits", 1, base in known
            ))
        for ancestor, depth in _ancestors(contract.name, contracts).items():
            if depth > 1:
                edges.append(InheritanceEdge(
                    contract.name, ancestor, "inherits-transitively", depth,
                    ancestor in known,
                ))
    seen = set()
    unique = []
    for edge in edges:
        key = (edge.child, edge.parent, edge.kind)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    return sorted(unique, key=lambda x: (x.child, x.depth, x.parent))


def _by_name(contracts):
    return {contract.name: contract for contract in contracts}


def _ancestors(name, contracts, depth=1, seen=None):
    table = _by_name(contracts)
    found: dict[str, int] = {}
    seen = seen or set()
    contract = table.get(name)
    if contract is None or name in seen or depth > 16:
        return found
    seen = seen | {name}
    for base in contract.bases:
        if base not in found or found[base] > depth:
            found[base] = depth
        for ancestor, sub_depth in _ancestors(base, contracts, depth + 1, seen).items():
            if ancestor not in found or found[ancestor] > sub_depth:
                found[ancestor] = sub_depth
    return found


def linearize(name, contracts) -> list[str]:
    """Depth-first, most-derived-first ordering (C3 approximation)."""
    order = [name]
    ancestors = _ancestors(name, contracts)
    order.extend(sorted(ancestors, key=lambda x: (ancestors[x], x)))
    return list(dict.fromkeys(order))


def resolve(contracts) -> list[ContractResolution]:
    table = _by_name(contracts)
    resolutions: list[ContractResolution] = []

    for contract in contracts:
        order = linearize(contract.name, contracts)
        own_state = {variable.name for variable in contract.state_vars}
        own_functions = {function.name for function in contract.functions}

        inherited: list[str] = []
        shadowed: list[str] = []
        overrides: list[tuple[str, str]] = []
        for base_name in order[1:]:
            base = table.get(base_name)
            if base is None:
                continue
            for variable in base.state_vars:
                if variable.name in own_state:
                    shadowed.append(f"{base_name}.{variable.name}")
                elif variable.name not in inherited:
                    inherited.append(variable.name)
            for function in base.functions:
                if function.name in own_functions:
                    overrides.append((f"{contract.name}.{function.name}",
                                      f"{base_name}.{function.name}"))

        resolutions.append(ContractResolution(
            contract.name, order, sorted(inherited), sorted(set(shadowed)),
            sorted(set(overrides)),
            sorted(base for base in contract.bases if base not in table),
        ))
    return resolutions


def link_inheritance(contracts) -> list[ContractResolution]:
    """Attribute inherited state to derived contracts, in place.

    Without this, a function in a derived contract that touches a base
    contract's variable looks stateless, and every downstream engine loses the
    dependency. Function reads/writes are recomputed against the enlarged state
    set using the statement IR when one is available.
    """
    table = _by_name(contracts)
    resolutions = resolve(contracts)

    for resolution in resolutions:
        contract = table[resolution.contract]
        if not resolution.inherited_state:
            continue
        own = {variable.name for variable in contract.state_vars}
        for base_name in resolution.linearization[1:]:
            base = table.get(base_name)
            if base is None:
                continue
            for variable in base.state_vars:
                if variable.name in own:
                    continue
                own.add(variable.name)
                inherited = type(variable)(**{
                    **variable.__dict__,
                    "contract": contract.name,
                })
                contract.state_vars.append(inherited)
        _recompute_access(contract, own)
    return resolutions


def _recompute_access(contract, state_names: set[str]) -> None:
    for function in contract.functions:
        if function.ir is not None:
            writes = set(function.writes)
            reads = set(function.reads)
            for statement in function.ir.walk():
                target = statement.target.text if statement.target else ""
                base = _base_name(target)
                if base in state_names and statement.kind in {"assign", "delete"}:
                    writes.add(base)
                for name in state_names:
                    if re.search(rf"\b{re.escape(name)}\b", statement.text or ""):
                        reads.add(name)
            function.writes = writes
            function.reads = reads | writes
        elif function.body:
            reads = {
                name for name in state_names
                if re.search(rf"\b{re.escape(name)}\b", function.body)
            }
            writes = {
                name for name in state_names
                if re.search(rf"\b{re.escape(name)}\s*(?:[+\-*/]?=|\+\+|--)",
                             function.body)
            }
            function.reads = set(function.reads) | reads | writes
            function.writes = set(function.writes) | writes


def _base_name(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("self."):
        cleaned = cleaned[5:]
    match = re.match(r"[A-Za-z_]\w*", cleaned)
    return match.group(0) if match else ""
