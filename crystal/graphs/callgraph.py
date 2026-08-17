"""Call graph built from the statement IR.

v1 resolved call targets by matching a bare function name against every
contract in the project. v2 uses the call records the parser produced: the
receiver, the call kind and the line are known, so internal calls resolve
inside the contract (and its bases), interface calls resolve by signature, and
unresolved external calls are kept as explicit unknown targets rather than
being silently dropped.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .. import ir as I

UNKNOWN = "<unresolved-external>"


@dataclass(frozen=True)
class CallEdge:
    source: str
    target: str
    confidence: float
    kind: str = I.INTERNAL_CALL
    line: int = 0
    receiver: str = ""
    resolved: bool = True
    value_attached: bool = False


@dataclass
class CallGraphSummary:
    edges: list[CallEdge] = field(default_factory=list)
    unresolved: list[CallEdge] = field(default_factory=list)
    external_entry_points: list[str] = field(default_factory=list)


def _index(contracts):
    by_contract: dict[str, dict[str, str]] = {}
    by_name: dict[str, list[str]] = {}
    bases: dict[str, list[str]] = {}
    for contract in contracts:
        bases[contract.name] = list(contract.bases)
        table = by_contract.setdefault(contract.name, {})
        for function in contract.functions:
            qualified = f"{contract.name}.{function.name}"
            table[function.name] = qualified
            by_name.setdefault(function.name, []).append(qualified)
    return by_contract, by_name, bases


def _resolve_internal(name, contract_name, by_contract, bases, depth=0):
    table = by_contract.get(contract_name, {})
    if name in table:
        return table[name]
    if depth > 6:
        return None
    for base in bases.get(contract_name, []):
        found = _resolve_internal(name, base, by_contract, bases, depth + 1)
        if found:
            return found
    return None


def build_call_graph(contracts) -> list[CallEdge]:
    by_contract, by_name, bases = _index(contracts)
    edges: list[CallEdge] = []

    for contract in contracts:
        for function in contract.functions:
            source = f"{contract.name}.{function.name}"
            if function.ir is None:
                # Fallback: v1 behaviour over the recorded call name list.
                for called in function.calls:
                    if called.startswith("<"):
                        continue
                    for target in by_name.get(called, []):
                        edges.append(CallEdge(
                            source, target,
                            0.95 if target.startswith(contract.name + ".") else 0.65,
                        ))
                continue

            for call in function.ir.calls():
                if not call.callee:
                    continue
                if call.kind in {I.INTERNAL_CALL, I.BUILTIN_CALL}:
                    target = _resolve_internal(
                        call.callee, contract.name, by_contract, bases
                    )
                    if target is None:
                        continue
                    edges.append(CallEdge(
                        source, target, 0.95, call.kind, call.line,
                        call.receiver or "", True, call.value_attached,
                    ))
                    continue

                candidates = by_name.get(call.callee, [])
                if len(candidates) == 1:
                    edges.append(CallEdge(
                        source, candidates[0], 0.70, call.kind, call.line,
                        call.receiver or "", True, call.value_attached,
                    ))
                elif candidates:
                    for target in candidates:
                        edges.append(CallEdge(
                            source, target, 0.45, call.kind, call.line,
                            call.receiver or "", True, call.value_attached,
                        ))
                else:
                    label = (
                        f"{call.receiver}.{call.callee}" if call.receiver
                        else call.callee
                    )
                    edges.append(CallEdge(
                        source, f"{UNKNOWN}:{label}", 0.40, call.kind, call.line,
                        call.receiver or "", False, call.value_attached,
                    ))

    seen = set()
    unique = []
    for edge in edges:
        key = (edge.source, edge.target, edge.line, edge.kind)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    return sorted(unique, key=lambda x: (x.source, x.line, x.target))


def summarize(contracts) -> CallGraphSummary:
    edges = build_call_graph(contracts)
    return CallGraphSummary(
        edges=[edge for edge in edges if edge.resolved],
        unresolved=[edge for edge in edges if not edge.resolved],
        external_entry_points=sorted({
            f"{contract.name}.{function.name}"
            for contract in contracts for function in contract.functions
            if function.is_entry_point
        }),
    )
