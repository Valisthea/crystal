"""Symbolic state and the effect records the engine produces."""

from __future__ import annotations

from dataclasses import dataclass, field

from .algebra import STATE_PREFIX, SymExpr
from .constraints import Constraint


@dataclass
class SymbolicState:
    """Maps concrete access paths (`totalAssets`, `balances[SENDER]`) to values.

    Deltas are aggregated per base state variable, because that is the
    granularity at which Crystal reasons about protocol accounting: a transfer
    that debits one mapping entry and credits another nets to zero.
    """

    values: dict[str, SymExpr] = field(default_factory=dict)
    bases: dict[str, str] = field(default_factory=dict)
    initial: dict[str, SymExpr] = field(default_factory=dict)
    touched: set[str] = field(default_factory=set)

    def read(self, path: str, base: str) -> SymExpr:
        if path not in self.values:
            symbol = SymExpr.symbol(f"{STATE_PREFIX}{path}")
            self.values[path] = symbol
            self.initial[path] = symbol
            self.bases[path] = base
        self.touched.add(base)
        return self.values[path]

    def write(self, path: str, base: str, value: SymExpr) -> None:
        if path not in self.initial:
            self.initial[path] = SymExpr.symbol(f"{STATE_PREFIX}{path}")
        self.values[path] = value
        self.bases[path] = base
        self.touched.add(base)

    def deltas(self) -> dict[str, SymExpr]:
        aggregated: dict[str, SymExpr] = {}
        for path, value in self.values.items():
            base = self.bases.get(path, path)
            difference = value - self.initial.get(path, SymExpr.zero())
            aggregated[base] = aggregated.get(base, SymExpr.zero()) + difference
        return aggregated

    def clone(self) -> "SymbolicState":
        return SymbolicState(
            dict(self.values), dict(self.bases), dict(self.initial), set(self.touched)
        )


@dataclass(frozen=True)
class PathEffect:
    """One explored control-flow path through a function or sequence."""

    conditions: tuple[Constraint, ...]
    deltas: dict[str, str]
    feasible: bool = True

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, value in self.deltas.items() if value != "0"))


@dataclass(frozen=True)
class FunctionEffect:
    function: str
    deltas: dict[str, str]
    reads: tuple[str, ...]
    writes: tuple[str, ...]
    constraints: tuple[Constraint, ...]
    paths: tuple[PathEffect, ...]
    external_calls: tuple[str, ...]
    unsupported: tuple[str, ...]
    branch_dependent: bool
    model: str = "symbolic-ast"
    # Primary-path polynomials, for structural comparison between functions.
    expressions: dict[str, SymExpr] = field(default_factory=dict)

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, value in self.deltas.items() if value != "0"))

    @property
    def supported(self) -> bool:
        return not self.unsupported


@dataclass(frozen=True)
class SequenceEffect:
    sequence: tuple[str, ...]
    before: dict[str, str]
    after: dict[str, str]
    deltas: dict[str, str]
    touched: tuple[str, ...]
    constraints: tuple[Constraint, ...]
    unsupported: tuple[str, ...]
    branch_dependent: bool
    model: str = "symbolic-ast"
    # Unrendered polynomials, kept so the concrete backend can substitute
    # values instead of re-parsing the rendered strings.
    expressions: dict[str, SymExpr] = field(default_factory=dict)
    initial: dict[str, SymExpr] = field(default_factory=dict)

    @property
    def changed(self) -> tuple[str, ...]:
        return tuple(sorted(name for name, value in self.deltas.items() if value != "0"))
