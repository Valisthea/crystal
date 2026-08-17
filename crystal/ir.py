"""Language-neutral statement IR shared by every Crystal parser.

Solidity, Rust, Move and Vyper front-ends all lower their native syntax tree
into these records. Everything downstream (symbolic engine, detectors, graphs)
consumes the IR and therefore stays language agnostic.
"""

from __future__ import annotations

from dataclasses import dataclass

# Statement kinds.
ASSIGN = "assign"
VAR_DECL = "var_decl"
REQUIRE = "require"
REVERT = "revert"
IF = "if"
LOOP = "loop"
CALL = "call"
EMIT = "emit"
RETURN = "return"
ASSEMBLY = "assembly"
BLOCK = "block"
DELETE = "delete"
UNKNOWN = "unknown"

# Call kinds.
INTERNAL_CALL = "internal"
EXTERNAL_CALL = "external"
LOW_LEVEL_CALL = "low_level"
DELEGATECALL = "delegatecall"
STATICCALL = "staticcall"
VALUE_TRANSFER = "value_transfer"
BUILTIN_CALL = "builtin"

EXTERNAL_CALL_KINDS = frozenset({
    EXTERNAL_CALL, LOW_LEVEL_CALL, DELEGATECALL, STATICCALL, VALUE_TRANSFER,
})


@dataclass(frozen=True)
class IRExpr:
    """A minimally structured expression.

    `text` always carries the original source slice so a human can audit any
    derived claim. `base` is the root identifier of an lvalue such as
    `balances[msg.sender]` (-> `balances`).
    """

    kind: str
    text: str
    base: str | None = None
    operator: str | None = None
    args: tuple["IRExpr", ...] = ()
    identifiers: tuple[str, ...] = ()
    line: int = 0

    def mentions(self, name: str) -> bool:
        return name in self.identifiers or self.base == name


@dataclass(frozen=True)
class IRCall:
    callee: str
    kind: str
    line: int
    text: str
    receiver: str | None = None
    arguments: tuple[IRExpr, ...] = ()
    value_attached: bool = False


@dataclass(frozen=True)
class IRStmt:
    kind: str
    line: int
    text: str
    target: IRExpr | None = None
    operator: str | None = None
    value: IRExpr | None = None
    condition: IRExpr | None = None
    call: IRCall | None = None
    body: tuple["IRStmt", ...] = ()
    orelse: tuple["IRStmt", ...] = ()
    reads: tuple[str, ...] = ()
    writes: tuple[str, ...] = ()
    loop_bound: int | None = None
    unchecked: bool = False
    note: str | None = None

    def walk(self):
        yield self
        for child in self.body:
            yield from child.walk()
        for child in self.orelse:
            yield from child.walk()


@dataclass(frozen=True)
class IRFunctionBody:
    statements: tuple[IRStmt, ...] = ()
    has_assembly: bool = False
    unsupported: tuple[str, ...] = ()
    source: str = ""

    def walk(self):
        for stmt in self.statements:
            yield from stmt.walk()

    def calls(self):
        for stmt in self.walk():
            if stmt.call is not None:
                yield stmt.call

    def external_calls(self):
        return [c for c in self.calls() if c.kind in EXTERNAL_CALL_KINDS]


@dataclass
class OrderedEvent:
    """Ordered execution trace element used by ordering-sensitive detectors."""

    index: int
    kind: str
    line: int
    detail: str
    variable: str | None = None
    call_kind: str | None = None


def flatten_events(body: IRFunctionBody) -> list[OrderedEvent]:
    """Linearize a function body into an ordered event trace.

    The order is syntactic (source order, depth first). It is an approximation
    of execution order that is sound enough for "external call before state
    write" style reasoning on straight-line and simply-branched code.
    """

    events: list[OrderedEvent] = []

    def visit(statements: tuple[IRStmt, ...]) -> None:
        for stmt in statements:
            if stmt.call is not None:
                events.append(OrderedEvent(
                    len(events),
                    "external_call" if stmt.call.kind in EXTERNAL_CALL_KINDS else "call",
                    stmt.call.line,
                    stmt.call.text[:200],
                    None,
                    stmt.call.kind,
                ))
            if stmt.kind == REQUIRE:
                events.append(OrderedEvent(
                    len(events), "guard", stmt.line, stmt.text[:200]
                ))
            for name in stmt.writes:
                events.append(OrderedEvent(
                    len(events), "state_write", stmt.line, stmt.text[:200], name
                ))
            for name in stmt.reads:
                if name not in stmt.writes:
                    events.append(OrderedEvent(
                        len(events), "state_read", stmt.line, stmt.text[:200], name
                    ))
            visit(stmt.body)
            visit(stmt.orelse)

    visit(body.statements)
    return events
