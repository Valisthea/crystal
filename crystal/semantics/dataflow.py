"""Data-flow edges derived from the symbolic effects.

v1 emitted one edge per (function, variable) read/write pair. v2 answers the
question that matters for exploitation: which attacker-controlled input reaches
which state variable, and through which statement.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..symbolic import SymbolicEngine
from ..symbolic.algebra import ARG_PREFIX, ENV_PREFIX, RETURN_PREFIX, SENDER

TAINTED_PREFIXES = (ARG_PREFIX, RETURN_PREFIX)


@dataclass(frozen=True)
class DataFlowEdge:
    function: str
    variable: str
    kind: str
    source: str = ""
    line: int = 0
    tainted: bool = False


def build_dataflow(contracts, engine=None) -> list[DataFlowEdge]:
    engine = engine or SymbolicEngine(contracts)
    edges: list[DataFlowEdge] = []

    for contract in contracts:
        for function in contract.functions:
            qualified = f"{contract.name}.{function.name}"

            if function.ir is None:
                for name in sorted(function.reads):
                    edges.append(DataFlowEdge(
                        qualified, f"{contract.name}.state.{name}", "read"
                    ))
                for name in sorted(function.writes):
                    edges.append(DataFlowEdge(
                        qualified, f"{contract.name}.state.{name}", "write"
                    ))
                continue

            for statement in function.ir.walk():
                for name in statement.writes:
                    edges.append(DataFlowEdge(
                        qualified, f"{contract.name}.state.{name}", "write",
                        statement.value.text if statement.value else "",
                        statement.line,
                    ))
                for name in statement.reads:
                    if name in statement.writes:
                        continue
                    edges.append(DataFlowEdge(
                        qualified, f"{contract.name}.state.{name}", "read",
                        statement.text[:120], statement.line,
                    ))

            try:
                effect = engine.execute_function(function)
            except RecursionError:
                continue
            for name, expression in effect.expressions.items():
                for symbol in expression.symbols:
                    if symbol.startswith(TAINTED_PREFIXES):
                        edges.append(DataFlowEdge(
                            qualified, f"{contract.name}.state.{name}",
                            "tainted-write", symbol, function.line, True,
                        ))
                    elif symbol == SENDER or symbol.startswith(ENV_PREFIX):
                        edges.append(DataFlowEdge(
                            qualified, f"{contract.name}.state.{name}",
                            "environment-write", symbol, function.line, False,
                        ))

    seen = set()
    unique = []
    for edge in edges:
        key = (edge.function, edge.variable, edge.kind, edge.line, edge.source)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    return sorted(unique, key=lambda x: (x.function, x.line, x.variable, x.kind))
