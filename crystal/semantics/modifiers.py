"""Modifier graph with guard classification.

Knowing that a function carries `onlyOwner` is only useful if the modifier
actually checks something. v2 resolves the modifier definition (including
through base contracts) and classifies what it enforces, so the access-control
and reentrancy detectors reason about behaviour instead of naming.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..detectors.base import REENTRANCY_GUARDS, SENDER_TOKENS

AUTHORITY = "authority"
REENTRANCY = "reentrancy"
STATE_GUARD = "state-guard"
PASSTHROUGH = "passthrough"
UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class ModifierEdge:
    function: str
    modifier: str
    guard_kind: str = UNRESOLVED
    definition: str = ""
    line: int = 0


@dataclass
class ModifierDefinition:
    contract: str
    name: str
    guard_kind: str
    line: int
    conditions: list[str] = field(default_factory=list)


def _definitions(contracts) -> dict[tuple[str, str], ModifierDefinition]:
    table: dict[tuple[str, str], ModifierDefinition] = {}
    for contract in contracts:
        for modifier in contract.modifier_definitions:
            conditions = []
            if modifier.ir is not None:
                conditions = [
                    statement.text for statement in modifier.ir.walk()
                    if statement.kind in {"require", "if", "revert"}
                ]
            table[(contract.name, modifier.name)] = ModifierDefinition(
                contract.name, modifier.name,
                _classify(modifier.name, conditions, modifier.writes),
                modifier.line, conditions,
            )
    return table


def _classify(name: str, conditions, writes) -> str:
    lowered = name.lower()
    text = " ".join(conditions).lower()
    if any(token in lowered for token in REENTRANCY_GUARDS) or (writes and not conditions):
        return REENTRANCY
    if any(token in text for token in SENDER_TOKENS):
        return AUTHORITY
    if any(token in lowered for token in
           ("only", "auth", "role", "admin", "owner", "governance", "permission")):
        return AUTHORITY
    if conditions:
        return STATE_GUARD
    return PASSTHROUGH


def _lookup(contract, name, definitions, contracts, depth=0):
    found = definitions.get((contract.name, name))
    if found is not None or depth > 6:
        return found
    table = {c.name: c for c in contracts}
    for base in contract.bases:
        base_contract = table.get(base)
        if base_contract is None:
            continue
        found = _lookup(base_contract, name, definitions, contracts, depth + 1)
        if found is not None:
            return found
    return None


def build_modifier_graph(contracts) -> list[ModifierEdge]:
    definitions = _definitions(contracts)
    edges: list[ModifierEdge] = []
    for contract in contracts:
        for function in contract.functions:
            for modifier in function.modifiers:
                definition = _lookup(contract, modifier, definitions, contracts)
                edges.append(ModifierEdge(
                    f"{contract.name}.{function.name}", modifier,
                    definition.guard_kind if definition else UNRESOLVED,
                    f"{definition.contract}.{definition.name}" if definition else "",
                    definition.line if definition else 0,
                ))
    return sorted(edges, key=lambda x: (x.function, x.modifier))


def modifier_definitions(contracts) -> list[ModifierDefinition]:
    return sorted(_definitions(contracts).values(),
                  key=lambda x: (x.contract, x.name))
