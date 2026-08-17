"""Move front-end.

Uses tree-sitter-move when the grammar package is installed and otherwise falls
back to a structural regex reader. Move's brace/semicolon syntax is close
enough to Solidity that the shared statement scanner produces a usable IR in
both modes.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from ..models import MOVE, Contract, ContractType, Function, Parameter, StateVar
from .base import ParseResult, matching_brace, split_arguments, strip_comments
from .solidity_regex import build_ir

PARSER_NAME = "regex"

MODULE_RE = re.compile(r"\bmodule\s+(?:([A-Za-z_]\w*|0x[0-9a-fA-F]+)::)?([A-Za-z_]\w*)\s*\{")
STRUCT_RE = re.compile(r"\bstruct\s+([A-Za-z_]\w*)(?:<[^>]*>)?\s*(has\s+[^\{]*)?\{")
FUNCTION_RE = re.compile(
    r"\b(?:(public\s*\(?\s*(?:friend|package)?\s*\)?|native|entry)\s+)*"
    r"(?:public\s+)?(?:entry\s+)?fun\s+([A-Za-z_]\w*)(?:<[^>]*>)?\s*\(",
    re.MULTILINE,
)


@lru_cache(maxsize=1)
def _load():
    for module_name in ("tree_sitter_move", "tree_sitter_move_on_aptos"):
        try:
            grammar = __import__(module_name)
            from tree_sitter import Language, Parser
        except ImportError:
            continue
        try:
            return Parser(Language(grammar.language())), "ok"
        except Exception as exc:  # pragma: no cover - depends on binding build
            return None, f"tree-sitter Move language failed to load: {exc}"
    return None, "tree-sitter Move grammar not installed (regex fallback in use)"


def available() -> bool:
    return _load()[0] is not None


def status() -> str:
    return _load()[1]


def backend_name() -> str:
    return "tree-sitter" if available() else "regex"


def _field_resolver(state_names: set[str]):
    def resolve(text: str) -> str | None:
        cleaned = (text or "").strip()
        segments = re.findall(r"[A-Za-z_]\w*", cleaned)
        for segment in reversed(segments):
            if segment in state_names:
                return segment
        return segments[0] if segments else None
    return resolve


def _params(text: str) -> list[Parameter]:
    out: list[Parameter] = []
    for chunk in split_arguments(text or ""):
        if ":" in chunk:
            name, type_name = chunk.split(":", 1)
            out.append(Parameter(name.strip(), " ".join(type_name.split())))
        else:
            out.append(Parameter(chunk.strip(), ""))
    return out


def _matching_paren(text: str, opening: int) -> int:
    depth = 0
    for index in range(opening, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    return len(text)


def parse_text(text: str, path: str) -> list[Contract]:
    scan = strip_comments(text)
    contracts: list[Contract] = []

    for module in MODULE_RE.finditer(scan):
        brace = scan.find("{", module.start())
        end = matching_brace(scan, brace)
        body = text[brace + 1:end - 1]
        scan_body = scan[brace + 1:end - 1]
        line_base = scan.count("\n", 0, module.start()) + 1

        contract = Contract(
            module.group(2), str(path), line_base,
            kind="module", language=MOVE,
            end_line=scan.count("\n", 0, end) + 1, parser=PARSER_NAME,
        )
        if module.group(1):
            contract.imports.append(module.group(1))

        state_names: set[str] = set()
        for struct in STRUCT_RE.finditer(scan_body):
            open_brace = scan_body.find("{", struct.start())
            close = matching_brace(scan_body, open_brace)
            fields_text = body[open_brace + 1:close - 1]
            abilities = (struct.group(2) or "").strip()
            struct_line = line_base + scan_body.count("\n", 0, struct.start())
            members: list[str] = []
            for field in split_arguments(fields_text):
                if ":" not in field:
                    continue
                field_name, field_type = field.split(":", 1)
                field_name = field_name.strip()
                field_type = " ".join(field_type.split())
                members.append(f"{field_name}: {field_type}")
                if "key" in abilities or "store" in abilities:
                    state_names.add(field_name)
                    contract.state_vars.append(StateVar(
                        contract.name, field_name, field_type, "struct-field",
                        struct_line, language=MOVE, value_type=field_type,
                    ))
            contract.types.append(ContractType(
                struct.group(1), "struct", members, struct_line,
            ))

        for match in FUNCTION_RE.finditer(scan_body):
            paren = scan_body.find("(", match.start())
            paren_end = _matching_paren(scan_body, paren)
            header_end = scan_body.find("{", paren_end)
            if header_end < 0:
                continue
            close = matching_brace(scan_body, header_end)
            header = scan_body[match.start():header_end]
            function_body = body[header_end + 1:close - 1]
            function_line = line_base + scan_body.count("\n", 0, match.start())

            visibility = "internal"
            if re.search(r"\bpublic\b", header):
                visibility = "public"
            if re.search(r"\bentry\b", header):
                visibility = "external"
            acquires = re.search(r"\bacquires\s+([^\{]+)", header)

            ir = build_ir(
                function_body, state_names,
                line_base + scan_body.count("\n", 0, header_end),
                resolve_base=_field_resolver(state_names),
            )
            writes: set[str] = set()
            calls: list[str] = []
            external = False
            for statement in ir.statements:
                for sub in statement.walk():
                    writes |= set(sub.writes)
                    if sub.call is not None:
                        if sub.call.kind in {"external", "low_level"}:
                            external = True
                        if sub.call.callee:
                            calls.append(sub.call.callee)
            reads = {n for n in state_names if re.search(rf"\b{re.escape(n)}\b", function_body)}
            reads |= writes

            function = Function(
                contract.name, match.group(2), visibility,
                "view" if not writes else "stateful",
                [x.strip() for x in (acquires.group(1).split(",") if acquires else [])][:16],
                reads, writes, sorted(set(calls))[:32], function_line, function_body,
            )
            function.params = _params(body[paren + 1:paren_end - 1])
            function.kind = "entry" if visibility == "external" else "function"
            function.language = MOVE
            function.parser = PARSER_NAME
            function.path = str(path)
            function.end_line = line_base + scan_body.count("\n", 0, close)
            function.ir = ir
            contract.functions.append(function)

        contracts.append(contract)
    return contracts


def parse_file(path) -> list[Contract]:
    return parse_text(Path(path).read_text(encoding="utf-8", errors="ignore"), str(path))


def parse_sources(paths) -> list[Contract]:
    out: list[Contract] = []
    for path in paths:
        if Path(path).suffix.lower() == ".move":
            out.extend(parse_file(path))
    return out


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), PARSER_NAME, MOVE)
