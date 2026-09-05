"""Resolving a call through an interface-typed state variable.

A protocol split across contracts wires itself with declared types:

    ICollateralManagement _collateralManagement;
    ...
    _collateralManagement.slashPegOutCollateral(who, amount);

Read `PegOutContract` alone and that call goes nowhere — the callee is declared
in an interface with no body. The composition only exists once the declared
type is bound to the contract that implements it, and that binding is the
difference between "five contracts that share no storage" and a protocol.

Binding is by declared type, never by bare function name: two contracts can
both define `settle` without being the same `settle`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Types that are never a contract handle, so never worth resolving.
_PRIMITIVE_PREFIXES = (
    "uint", "int", "address", "bool", "bytes", "string", "mapping",
    "fixed", "ufixed",
)


def _is_candidate_type(type_name: str) -> bool:
    text = (type_name or "").strip()
    if not text or "(" in text or "[" in text:
        return False
    return not any(text.startswith(prefix) for prefix in _PRIMITIVE_PREFIXES)


@dataclass
class InterfaceBindings:
    """Declared type -> the concrete contracts that stand behind it."""

    # contract -> {state variable name: declared type}
    handles: dict[str, dict[str, str]] = field(default_factory=dict)
    # declared type -> concrete contracts implementing it
    behind: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # contract -> {function name: qualified name}
    functions: dict[str, dict[str, str]] = field(default_factory=dict)

    def implementations(self, type_name: str) -> tuple[str, ...]:
        return self.behind.get(type_name, ())

    def resolve(self, contract: str, receiver: str, callee: str):
        """Every `Contract.function` a `receiver.callee()` call can reach.

        Returns pairs of (qualified target, confidence). A single implementer
        is a near-certain binding; several means the deployment decides, so
        each is offered at reduced confidence rather than one being guessed.
        """
        declared = self.handles.get(contract, {}).get(receiver)
        if not declared:
            return []
        implementations = self.behind.get(declared, ())
        if not implementations:
            return []
        confidence = 0.88 if len(implementations) == 1 else 0.55
        found = []
        for implementation in implementations:
            qualified = self.functions.get(implementation, {}).get(callee)
            if qualified:
                found.append((qualified, confidence))
        return found


def build_bindings(contracts) -> InterfaceBindings:
    bindings = InterfaceBindings()
    by_name = {contract.name: contract for contract in contracts}

    for contract in contracts:
        bindings.functions[contract.name] = {
            function.name: f"{contract.name}.{function.name}"
            for function in contract.functions
        }
        handles = {
            variable.name: variable.type_name.strip()
            for variable in contract.state_vars
            if _is_candidate_type(variable.type_name)
        }
        if handles:
            bindings.handles[contract.name] = handles

    # A concrete contract stands behind its own name and behind every type it
    # inherits, transitively — `A is B is IC` implements `IC`.
    behind: dict[str, list[str]] = {}
    for contract in contracts:
        if contract.kind == "interface":
            continue
        for supplied in _supertypes(contract.name, by_name):
            behind.setdefault(supplied, []).append(contract.name)

    bindings.behind = {
        key: tuple(sorted(set(value))) for key, value in behind.items()
    }
    return bindings


def _supertypes(name: str, by_name, seen=None) -> set[str]:
    seen = seen if seen is not None else set()
    if name in seen or name not in by_name:
        return seen
    seen.add(name)
    for base in by_name[name].bases:
        _supertypes(base, by_name, seen)
    return seen
