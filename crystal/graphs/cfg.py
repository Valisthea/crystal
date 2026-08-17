"""Control-flow graph with real basic blocks.

v1 split the body on `;{}` and chained every fragment linearly, which produced
a list rather than a graph. v2 builds basic blocks from the statement IR:
branches fork, loops close back, and terminators (`return`, `revert`) end a
block without a fallthrough edge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import ir as I

TRUE = "true"
FALSE = "false"
NEXT = "next"
BACK = "loop-back"
EXIT = "exit"


@dataclass
class CFGNode:
    id: int
    kind: str
    text: str
    line: int = 0
    statements: list[str] = field(default_factory=list)
    terminator: str = ""


@dataclass
class CFG:
    function: str
    nodes: list[CFGNode] = field(default_factory=list)
    edges: list[tuple[int, int, str]] = field(default_factory=list)
    entry: int = 0
    exits: list[int] = field(default_factory=list)
    model: str = "ir-basic-blocks"

    @property
    def branch_count(self) -> int:
        return sum(1 for node in self.nodes if node.kind == "branch")

    @property
    def cyclomatic_complexity(self) -> int:
        return max(1, len(self.edges) - len(self.nodes) + 2)


class _Builder:
    def __init__(self, cfg: CFG):
        self.cfg = cfg

    def new_node(self, kind: str, text: str, line: int) -> int:
        node = CFGNode(len(self.cfg.nodes), kind, text[:300], line)
        self.cfg.nodes.append(node)
        return node.id

    def connect(self, sources, target: int, label: str = NEXT) -> None:
        for source in sources:
            if source is None:
                continue
            self.cfg.edges.append((source, target, label))

    def build(self, statements, incoming: list[int], label: str = NEXT) -> list[int]:
        """Return the set of block ids that fall through after `statements`."""
        current: int | None = None
        edge_label = label
        for statement in statements:
            if statement.kind in {I.IF, I.LOOP}:
                incoming = self._flush(current, incoming)
                current = None
                incoming = self._control(statement, incoming, edge_label)
                edge_label = NEXT
                continue
            if current is None:
                current = self.new_node("block", statement.text, statement.line)
                self.connect(incoming, current, edge_label)
                edge_label = NEXT
                incoming = []
            node = self.cfg.nodes[current]
            node.statements.append(f"L{statement.line} {statement.kind}: {statement.text}")
            if statement.kind in {I.RETURN, I.REVERT}:
                node.terminator = statement.kind
                node.kind = "terminator"
                self.cfg.exits.append(current)
                current = None
                incoming = []
            elif statement.kind == I.CALL and statement.call is not None \
                    and statement.call.kind in I.EXTERNAL_CALL_KINDS:
                node.kind = "external_call"
            elif statement.kind == I.REQUIRE and node.kind == "block":
                node.kind = "guard"
        return self._flush(current, incoming)

    def _flush(self, current, incoming) -> list[int]:
        if current is not None:
            return [current]
        return incoming

    def _control(self, statement, incoming: list[int], label: str) -> list[int]:
        condition = statement.condition.text if statement.condition else statement.text
        head = self.new_node(
            "branch" if statement.kind == I.IF else "loop-header",
            condition, statement.line,
        )
        self.connect(incoming, head, label)

        if statement.kind == I.LOOP:
            body_exits = self.build(statement.body, [head], TRUE) \
                if statement.body else [head]
            self.connect([x for x in body_exits if x != head], head, BACK)
            return [head]

        taken = self.build(statement.body, [head], TRUE) if statement.body else [head]
        skipped = self.build(statement.orelse, [head], FALSE) \
            if statement.orelse else [head]
        return list(dict.fromkeys(taken + skipped))


def _fallback(function) -> CFG:
    cfg = CFG(f"{function.contract}.{function.name}", model="text-approximation")
    statements = [x.strip() for x in re.split(r"[;{}]", function.body or "") if x.strip()]
    for index, statement in enumerate(statements):
        kind = "block"
        if re.match(r"^(if|for|while|do)\b", statement):
            kind = "branch"
        elif re.match(r"^(require|assert|revert)\b", statement):
            kind = "guard"
        elif re.search(r"\.(call|delegatecall|staticcall|transfer|send)\b", statement):
            kind = "external_call"
        cfg.nodes.append(CFGNode(index, kind, statement[:300]))
        if index:
            cfg.edges.append((index - 1, index, NEXT))
    if cfg.nodes:
        cfg.exits.append(cfg.nodes[-1].id)
    return cfg


def build_cfg(function) -> CFG:
    if function.ir is None or not function.ir.statements:
        return _fallback(function)

    cfg = CFG(f"{function.contract}.{function.name}")
    entry = CFGNode(0, "entry", "entry", function.line)
    cfg.nodes.append(entry)
    builder = _Builder(cfg)
    tail = builder.build(function.ir.statements, [0])

    exit_node = CFGNode(len(cfg.nodes), "exit", "exit", function.end_line)
    cfg.nodes.append(exit_node)
    builder.connect(tail, exit_node.id, EXIT)
    cfg.exits.append(exit_node.id)
    return cfg
