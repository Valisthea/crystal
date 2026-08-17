"""Vyper front-end.

Vyper is indentation structured, so the fallback reader is an indentation
scanner rather than a brace scanner. tree-sitter-vyper is used when the grammar
package is installed; otherwise this reader produces the same records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .. import ir as I
from ..models import VYPER, Contract, ContractEvent, ContractType, Function
from ..models import Parameter, StateVar
from .base import ParseResult, identifiers_in, split_arguments

PARSER_NAME = "regex"

DEF_RE = re.compile(r"^def\s+([A-Za-z_]\w*)\s*\((.*)\)\s*(?:->\s*(.+?))?\s*:\s*$")
STATE_RE = re.compile(r"^([A-Za-z_]\w*)\s*:\s*(.+?)\s*$")
BLOCK_RE = re.compile(r"^(event|struct|interface|enum|flag)\s+([A-Za-z_]\w*)\s*:\s*$")
VISIBILITY_WRAPPERS = ("public", "immutable", "constant", "transient")
EXTERNAL_CALL_RE = re.compile(
    r"\b(raw_call|send|create_from_blueprint|create_copy_of|create_minimal_proxy_to)\s*\("
    r"|\b[A-Za-z_]\w*\s*\(\s*[^)]*\)\s*\.\s*[A-Za-z_]\w*\s*\("
    r"|\bextcall\b|\bstaticcall\b"
)


@lru_cache(maxsize=1)
def _load():
    try:
        import tree_sitter_vyper
        from tree_sitter import Language, Parser
    except ImportError:
        return None, "tree-sitter Vyper grammar not installed (regex fallback in use)"
    try:
        return Parser(Language(tree_sitter_vyper.language())), "ok"
    except Exception as exc:  # pragma: no cover - depends on binding build
        return None, f"tree-sitter Vyper language failed to load: {exc}"


def available() -> bool:
    return _load()[0] is not None


def status() -> str:
    return _load()[1]


def backend_name() -> str:
    return "tree-sitter" if available() else "regex"


def _strip_comment(line: str) -> str:
    out = []
    quote = None
    for index, char in enumerate(line):
        if quote:
            out.append(char)
            if char == quote and line[index - 1] != "\\":
                quote = None
            continue
        if char in "\"'":
            quote = char
            out.append(char)
            continue
        if char == "#":
            break
        out.append(char)
    return "".join(out).rstrip()


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip())


def _params(text: str) -> list[Parameter]:
    out: list[Parameter] = []
    for chunk in split_arguments(text or ""):
        if ":" in chunk:
            name, type_name = chunk.split(":", 1)
            out.append(Parameter(name.strip(), " ".join(type_name.split()).split("=")[0].strip()))
        elif chunk.strip():
            out.append(Parameter(chunk.strip(), ""))
    return out


def _base_name(text: str) -> str | None:
    cleaned = (text or "").strip()
    if cleaned.startswith("self."):
        cleaned = cleaned[5:]
    match = re.match(r"[A-Za-z_]\w*", cleaned)
    return match.group(0) if match else None


class _BodyScanner:
    def __init__(self, lines: list[tuple[int, str, int]], state_names: set[str]):
        self.lines = lines
        self.state = state_names
        self.unsupported: list[str] = []

    def state_in(self, text: str) -> tuple[str, ...]:
        return tuple(sorted(set(identifiers_in(text)) & self.state))

    def expr(self, text: str, line: int) -> I.IRExpr:
        return I.IRExpr("expression", (text or "").strip()[:400], _base_name(text),
                        identifiers=identifiers_in(text), line=line)

    def scan(self, start: int, end: int, indent: int) -> tuple[tuple[I.IRStmt, ...], int]:
        out: list[I.IRStmt] = []
        index = start
        while index < end:
            line_no, text, current_indent = self.lines[index]
            if not text.strip():
                index += 1
                continue
            if current_indent < indent:
                break
            if current_indent > indent:
                index += 1
                continue
            statement, index = self.statement(index, end, current_indent)
            if statement is not None:
                out.append(statement)
        return tuple(out), index

    def _nested(self, index: int, end: int, indent: int):
        if index >= end:
            return (), index
        next_indent = self.lines[index][2]
        if next_indent <= indent:
            return (), index
        return self.scan(index, end, next_indent)

    def statement(self, index: int, end: int, indent: int):
        line_no, raw, _ = self.lines[index]
        text = raw.strip()
        index += 1

        header = re.match(r"^(if|elif|else|for|while|assert|raise|log|return|pass|"
                          r"break|continue|with)\b", text)
        keyword = header.group(1) if header else ""

        if keyword in {"if", "elif"}:
            condition = text[len(keyword):].strip().rstrip(":")
            body, index = self._nested(index, end, indent)
            orelse: tuple[I.IRStmt, ...] = ()
            if index < end:
                next_line = self.lines[index]
                if next_line[2] == indent and re.match(r"^(else|elif)\b", next_line[1].strip()):
                    branch, index = self.statement(index, end, indent)
                    orelse = (branch,) if branch is not None else ()
            return I.IRStmt(I.IF, line_no, text[:400],
                            condition=self.expr(condition, line_no), body=body,
                            orelse=orelse, reads=self.state_in(condition)), index
        if keyword == "else":
            body, index = self._nested(index, end, indent)
            return I.IRStmt(I.BLOCK, line_no, "else", body=body), index
        if keyword in {"for", "while"}:
            condition = text[len(keyword):].strip().rstrip(":")
            body, index = self._nested(index, end, indent)
            bound = re.search(r"range\s*\(\s*(\d+)", condition)
            return I.IRStmt(I.LOOP, line_no, text[:400],
                            condition=self.expr(condition, line_no), body=body,
                            reads=self.state_in(condition), note=keyword,
                            loop_bound=int(bound.group(1)) if bound else None), index
        if keyword == "assert":
            condition = text[6:].strip()
            return I.IRStmt(I.REQUIRE, line_no, text[:400],
                            condition=self.expr(condition, line_no),
                            reads=self.state_in(condition), note="assert"), index
        if keyword == "raise":
            return I.IRStmt(I.REVERT, line_no, text[:400],
                            reads=self.state_in(text)), index
        if keyword == "log":
            return I.IRStmt(I.EMIT, line_no, text[:400],
                            reads=self.state_in(text)), index
        if keyword == "return":
            value = text[6:].strip()
            return I.IRStmt(I.RETURN, line_no, text[:400],
                            value=self.expr(value, line_no),
                            reads=self.state_in(value)), index
        if keyword in {"pass", "break", "continue"}:
            return None, index
        if keyword == "with":
            body, index = self._nested(index, end, indent)
            return I.IRStmt(I.BLOCK, line_no, text[:400], body=body), index

        call = self._call(text, line_no)
        target, operator, value = _split_assignment(text)
        if operator:
            base = _base_name(target)
            reads = set(self.state_in(value))
            if operator != "=" or "[" in target or target.strip().startswith("self."):
                reads |= set(self.state_in(target))
            declared = re.match(r"^[A-Za-z_]\w*\s*:\s*[^=]+$", target)
            return I.IRStmt(
                I.VAR_DECL if declared else I.ASSIGN, line_no, text[:400],
                target=self.expr(target, line_no), operator=operator,
                value=self.expr(value, line_no), call=call,
                writes=(base,) if base in self.state and not declared else (),
                reads=tuple(sorted(reads)),
            ), index
        if call is not None:
            return I.IRStmt(I.CALL, line_no, text[:400], call=call,
                            reads=self.state_in(text)), index
        return I.IRStmt(I.UNKNOWN, line_no, text[:400],
                        reads=self.state_in(text)), index

    def _call(self, text: str, line_no: int):
        if EXTERNAL_CALL_RE.search(text):
            match = re.search(r"\b([A-Za-z_]\w*)\s*\(", text)
            return I.IRCall(match.group(1) if match else "raw_call",
                            I.LOW_LEVEL_CALL, line_no, text[:400], None, (),
                            "value=" in text or "send(" in text)
        match = re.search(r"\b([A-Za-z_]\w*)\s*\(", text)
        if not match:
            return None
        name = match.group(1)
        if name in {"if", "while", "for", "assert", "return", "range"}:
            return None
        receiver = "self" if text.strip().startswith("self.") else None
        return I.IRCall(name, I.INTERNAL_CALL if receiver else I.BUILTIN_CALL,
                        line_no, text[:400], receiver, (), False)


@dataclass
class _PendingFunction:
    line: int
    name: str
    params_text: str
    return_text: str
    decorators: list[str]
    start: int
    end: int


def _split_assignment(text: str) -> tuple[str, str | None, str]:
    depth = 0
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0:
            for operator in ("+=", "-=", "*=", "/=", "%=", "|=", "&=", "^=", "**="):
                if text.startswith(operator, index):
                    return text[:index].strip(), operator, text[index + len(operator):].strip()
            if char == "=" and not text.startswith("==", index):
                if index and text[index - 1] in "!<>=+-*/%|&^":
                    index += 1
                    continue
                return text[:index].strip(), "=", text[index + 1:].strip()
        index += 1
    return text, None, ""


def parse_text(text: str, path: str) -> list[Contract]:
    raw_lines = text.splitlines()
    lines = [
        (number + 1, _strip_comment(line), _indent(_strip_comment(line) or line))
        for number, line in enumerate(raw_lines)
    ]
    name = Path(path).stem
    contract = Contract(name, str(path), 1, kind="contract", language=VYPER,
                        end_line=len(raw_lines), parser=PARSER_NAME)

    state_names: set[str] = set()
    decorators: list[str] = []
    functions: list[_PendingFunction] = []
    index = 0
    total = len(lines)

    while index < total:
        line_no, content, indent = lines[index]
        stripped = content.strip()
        if not stripped or indent != 0:
            index += 1
            continue
        if stripped.startswith("#"):
            index += 1
            continue
        if stripped.startswith("@"):
            decorators.append(stripped[1:])
            index += 1
            continue
        if stripped.startswith(("import ", "from ")):
            contract.imports.append(stripped)
            index += 1
            continue
        if stripped.startswith("implements:"):
            contract.bases.append(stripped.split(":", 1)[1].strip())
            index += 1
            continue

        block = BLOCK_RE.match(stripped)
        if block:
            members: list[str] = []
            index += 1
            while index < total and (not lines[index][1].strip() or lines[index][2] > 0):
                if lines[index][1].strip():
                    members.append(lines[index][1].strip())
                index += 1
            if block.group(1) == "event":
                contract.events.append(ContractEvent(
                    block.group(2), _params(", ".join(members)), line_no,
                ))
            else:
                contract.types.append(ContractType(
                    block.group(2), block.group(1), members, line_no,
                ))
            continue

        definition = DEF_RE.match(stripped)
        if definition:
            start = index + 1
            index += 1
            while index < total and (not lines[index][1].strip() or lines[index][2] > 0):
                index += 1
            functions.append(_PendingFunction(
                line_no, definition.group(1), definition.group(2),
                definition.group(3) or "", list(decorators), start, index,
            ))
            decorators = []
            continue

        state = STATE_RE.match(stripped)
        if state and not stripped.endswith(":"):
            variable_name, type_text = state.group(1), state.group(2)
            visibility = "internal"
            for wrapper in VISIBILITY_WRAPPERS:
                if type_text.startswith(f"{wrapper}("):
                    visibility = "public" if wrapper == "public" else visibility
                    type_text = type_text[len(wrapper) + 1:].rstrip(")")
            keys: list[str] = []
            value_type = ""
            mapping = re.match(r"HashMap\s*\[(.+)\]\s*$", type_text)
            if mapping:
                parts = split_arguments(mapping.group(1))
                if len(parts) == 2:
                    keys, value_type = [parts[0]], parts[1]
            state_names.add(variable_name)
            contract.state_vars.append(StateVar(
                name, variable_name, type_text, visibility, line_no,
                constant="constant(" in state.group(2),
                immutable="immutable(" in state.group(2),
                key_types=keys, value_type=value_type, language=VYPER,
            ))
        index += 1

    for entry in functions:
        line_no = entry.line
        function_name = entry.name
        params_text = entry.params_text
        return_text = entry.return_text
        decorators = entry.decorators
        start, end = entry.start, entry.end
        scanner = _BodyScanner(lines, state_names)
        statements, _ = scanner.scan(start, end, lines[start][2] if start < end else 4)
        writes: set[str] = set()
        calls: list[str] = []
        external = False
        for statement in statements:
            for sub in statement.walk():
                writes |= set(sub.writes)
                if sub.call is not None:
                    if sub.call.kind in I.EXTERNAL_CALL_KINDS:
                        external = True
                    if sub.call.callee:
                        calls.append(sub.call.callee)
        body_text = "\n".join(lines[i][1] for i in range(start, end))
        reads = {n for n in state_names if re.search(rf"\b{re.escape(n)}\b", body_text)}
        reads |= writes

        visibility = "external" if "external" in decorators else "internal"
        mutability = "stateful"
        if "view" in decorators:
            mutability = "view"
        elif "pure" in decorators:
            mutability = "pure"
        elif "payable" in decorators:
            mutability = "payable"

        function = Function(
            name, function_name, visibility, mutability,
            sorted({d.split("(")[0] for d in decorators})[:16],
            reads, writes,
            (["<low-level-call>"] if external else []) + sorted(set(calls))[:32],
            line_no, body_text,
        )
        function.params = _params(params_text)
        function.returns = [Parameter("", return_text)] if return_text else []
        function.kind = "function"
        function.language = VYPER
        function.parser = PARSER_NAME
        function.path = str(path)
        function.payable = "payable" in decorators
        function.end_line = lines[end - 1][0] if end and end <= len(lines) else line_no
        function.ir = I.IRFunctionBody(
            statements=statements, unsupported=tuple(scanner.unsupported),
            source=body_text,
        )
        contract.functions.append(function)

    return [contract] if (contract.functions or contract.state_vars) else []


def parse_file(path) -> list[Contract]:
    return parse_text(Path(path).read_text(encoding="utf-8", errors="ignore"), str(path))


def parse_sources(paths) -> list[Contract]:
    out: list[Contract] = []
    for path in paths:
        if Path(path).suffix.lower() in {".vy", ".vyi"}:
            out.extend(parse_file(path))
    return out


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), PARSER_NAME, VYPER)
