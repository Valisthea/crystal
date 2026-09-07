"""Regex Solidity front-end.

This is the original Crystal v1 parser. It stays in the tree as the guaranteed
fallback: when tree-sitter is not installed Crystal degrades to this parser
instead of failing. Contract/function/state extraction is byte-for-byte the v1
behaviour; the v2 additions are typed parameters and an approximate statement
IR so that ordering-sensitive analysis still gets a (clearly labelled) signal.
"""

from __future__ import annotations

import re
from pathlib import Path

from .. import ir as I
from ..models import SOLIDITY, Contract, ContractType, Function, Parameter, StateVar
from .base import (
    ParseResult,
    base_identifier,
    identifiers_in,
    matching_brace,
    split_arguments,
    strip_comments,
)
from .solidity_types import (
    EMPTY_CATALOG,
    TypeCatalog,
    catalog_from_paths,
    catalog_from_text,
)

CONTRACT_RE = re.compile(
    r"\b(contract|abstract\s+contract|interface|library)\s+([A-Za-z_]\w*)"
    r"(?:\s+is\s+([^\{]+))?\s*\{", re.MULTILINE
)

# What follows a state variable's name when it is being written.
#
# The name may be followed by any chain of index or member accesses first —
# `balances[msg.sender] -= x` and `pool.total += y` are writes to `balances`
# and `pool`. Requiring the operator to sit immediately after the name missed
# every indexed assignment, which in Solidity is most state writes: the regex
# front-end reported empty `writes` for whole contracts, and everything keyed
# on written state (the causal graph, symbolic deltas, coupling grading) came
# out empty on the fallback path while looking healthy under tree-sitter.
#
# `=` must also not be the tail of `==`, `!=`, `<=` or `>=`; the old pattern
# read a comparison as an assignment.
_ACCESS_CHAIN = r"(?:\s*\[[^\]]*\]|\s*\.[A-Za-z_]\w*)*"
_ASSIGN_TAIL = r"(?:(?:[+\-*/%&|^]|<<|>>)?=(?!=)|\+\+|--)"


def _write_re(name: str) -> re.Pattern:
    return re.compile(
        r"\b" + re.escape(name) + _ACCESS_CHAIN + r"\s*" + _ASSIGN_TAIL
    )


# A local declaration's left-hand side carries a type before the name:
# `uint256 stored`, `Quotes.PegOutQuote memory quote`, `address payable to`,
# `(bool sent, bytes memory data)`. An assignment target never does — it is a
# bare name, an index, or a member access, none of which contain a space at
# top level.
_DECL_LHS_RE = re.compile(
    r"^[A-Za-z_][\w.]*(?:\s*\[\s*\])*"          # type, incl. arrays and Lib.Type
    r"(?:\s+(?:memory|storage|calldata|payable))*"
    r"\s+[A-Za-z_]\w*$"
)


def _is_declaration(lhs: str) -> bool:
    cleaned = (lhs or "").strip()
    if cleaned.startswith("("):
        # A tuple destructuring declares whenever any element carries a type.
        inner = cleaned[1:].rsplit(")", 1)[0]
        return any(_DECL_LHS_RE.match(part.strip())
                   for part in inner.split(",") if part.strip())
    return bool(_DECL_LHS_RE.match(cleaned))


def _declared_name(lhs: str) -> str:
    """The name a declaration introduces. Tuples keep their whole shape."""
    cleaned = (lhs or "").strip()
    if cleaned.startswith("("):
        return cleaned
    return cleaned.rsplit(None, 1)[-1] if " " in cleaned else cleaned
FUNCTION_RE = re.compile(
    r"\bfunction\s+([A-Za-z_]\w*)\s*\(([^)]*)\)([^{};]*)\{", re.MULTILINE
)
MODIFIER_RE = re.compile(
    r"\bmodifier\s+([A-Za-z_]\w*)\s*(?:\(([^)]*)\))?[^{;]*\{", re.MULTILINE
)
CONSTRUCTOR_RE = re.compile(r"\bconstructor\s*\(([^)]*)\)([^{;]*)\{", re.MULTILINE)
FALLBACK_RE = re.compile(r"\b(receive|fallback)\s*\(\s*\)([^{;]*)\{", re.MULTILINE)
# A state variable takes any number of modifiers between its type and its name,
# in any order: `uint16 private constant MAX_BASIS_POINTS = 1e4;`. Accepting
# only one dropped every `constant` and `immutable` declaration on this path —
# so a target's configured parameters were invisible to the fallback parser
# while tree-sitter saw them all.
VAR_MODIFIERS = "public|private|internal|constant|immutable|transient|override"
VAR_RE = re.compile(
    r"^\s*(?P<type>(?:mapping\s*\([^;]+\)|[A-Za-z_]\w*(?:\[\])?))"
    r"(?P<mods>(?:\s+(?:" + VAR_MODIFIERS + r"))*)"
    r"\s+(?P<name>[A-Za-z_]\w*)\s*(?:=\s*(?P<init>[^;]*))?;",
    re.MULTILINE
)
VIS_RE = re.compile(r"\b(public|external|internal|private)\b")
MUT_RE = re.compile(r"\b(view|pure|payable)\b")
LOWLEVEL_RE = re.compile(r"\.(call|delegatecall|staticcall|transfer|send)\b")
TYPE_DECL_RE = re.compile(r"\b(struct|enum)\s+([A-Za-z_]\w*)\s*\{")
# A callee candidate: `name(`, `a.b.c(`, `foo{value: 1}(`.
CALLEE_RE = re.compile(r"([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*(?:\{[^}]*\})?\s*\(")
# What follows a closed argument list when the call chains outward:
# `IFoo(x).bar(`, `foo(a)[0].bar{gas: 1}(`.
CHAIN_RE = re.compile(r"\s*(?:\[[^\]]*\]\s*)*\.\s*([A-Za-z_]\w*)\s*(?:\{[^}]*\})?\s*\(")
NEW_RE = re.compile(r"\bnew\s+$")
# Words that can precede `(` in a statement without naming a callee.
NOT_CALLEES = {
    "if", "for", "while", "do", "return", "returns", "catch", "try", "else",
    "emit", "revert", "delete", "unchecked", "assembly", "new",
}

MODIFIER_KEYWORDS = {
    "external", "public", "internal", "private", "view", "pure",
    "payable", "returns", "virtual", "override", "memory", "calldata",
    "storage",
}

LOCATIONS = {"memory", "calldata", "storage"}

CONTROL_KEYWORDS = {
    "if", "for", "while", "do", "unchecked", "assembly", "try", "return",
    "emit", "revert", "require", "assert", "delete", "break", "continue",
}


def _parse_params(text: str) -> list[Parameter]:
    params: list[Parameter] = []
    for chunk in split_arguments(text or ""):
        tokens = chunk.replace("indexed", " indexed ").split()
        if not tokens:
            continue
        indexed = "indexed" in tokens
        tokens = [t for t in tokens if t != "indexed"]
        location = next((t for t in tokens if t in LOCATIONS), "")
        tokens = [t for t in tokens if t not in LOCATIONS]
        if len(tokens) == 1:
            params.append(Parameter("", tokens[0], indexed, location))
        elif tokens:
            params.append(Parameter(tokens[-1], " ".join(tokens[:-1]), indexed, location))
    return params


def _matching(text: str, opening: int, open_ch: str, close_ch: str) -> int:
    depth = 0
    i = opening
    n = len(text)
    while i < n:
        if text[i] == open_ch:
            depth += 1
        elif text[i] == close_ch:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _split_arguments(text: str) -> tuple[str, ...]:
    """Split a call's argument text on top-level commas."""
    parts, depth, current = [], 0, []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == "," and depth == 0:
            parts.append("".join(current))
            current = []
            continue
        current.append(char)
    parts.append("".join(current))
    return tuple(part.strip() for part in parts if part.strip())


def _classify_call(text: str, catalog: TypeCatalog | None = None
                   ) -> tuple[str, str, str | None, bool, tuple[str, ...]]:
    """Return (callee, kind, receiver, value_attached, arguments).

    The arguments are the call's own, as written. They were previously dropped,
    which left `IRCall.arguments` empty on this front-end — so anything asking
    what actually flows into a call (the coupling grade's argument half, the
    "same arguments at both sites" test, the symbolic engine's call records)
    silently had nothing to work with.

    Casts and struct literals (`CToken(addr)`, `Exp({mantissa: x})`,
    `uint256(x)`) are spelled like calls but call nothing; they are skipped on
    the catalog's evidence and the search continues inside their arguments,
    so `Exp({mantissa: CToken(c).borrowIndex()})` still yields `borrowIndex`.
    A call that chains outward — `IFoo(x).bar(y)` — is reported as its
    outermost member call with the inner expression as receiver, which is
    what the tree-sitter front-end records for the same text.
    """
    catalog = catalog or EMPTY_CATALOG
    value_attached = "{value:" in text.replace(" ", "")
    low = LOWLEVEL_RE.search(text)
    if low:
        name = low.group(1)
        receiver = text[:low.start()].strip().rsplit(" ", 1)[-1] or None
        kind = {
            "call": I.LOW_LEVEL_CALL,
            "delegatecall": I.DELEGATECALL,
            "staticcall": I.STATICCALL,
            "transfer": I.VALUE_TRANSFER,
            "send": I.VALUE_TRANSFER,
        }[name]
        close = _matching(text, text.find("(", low.end() - 1), "(", ")")
        opening = text.find("(", low.end() - 1)
        arguments = _split_arguments(text[opening + 1:close - 1]) if close > 0 else ()
        return (name, kind, receiver,
                value_attached or name in {"transfer", "send"}, arguments)
    for match in CALLEE_RE.finditer(text):
        callee = match.group(1)
        if callee in NOT_CALLEES:
            continue
        start = match.start(1)
        creation = NEW_RE.search(text, 0, start)
        receiver_start = creation.start() if creation else start
        receiver, name = callee.rsplit(".", 1) if "." in callee else (None, callee)
        close = _matching(text, match.end() - 1, "(", ")")
        while True:
            chain = CHAIN_RE.match(text, close)
            if not chain:
                break
            receiver = " ".join(text[receiver_start:close].split())
            name = chain.group(1)
            close = _matching(text, chain.end() - 1, "(", ")")
        if receiver is None:
            # `new Foo(..)` runs a constructor; a conversion runs nothing.
            if creation or not catalog.is_conversion(name):
                return (name, I.INTERNAL_CALL, None, value_attached,
                        _split_arguments(text[match.end():close - 1]))
            continue
        if catalog.is_qualified_conversion(receiver, name):
            continue
        kind = I.INTERNAL_CALL if receiver in {"super", "this"} else I.EXTERNAL_CALL
        opening = text.rfind("(", 0, close)
        return (name, kind, receiver, value_attached,
                _split_arguments(text[opening + 1:close - 1]))
    return "", I.INTERNAL_CALL, None, value_attached, ()


class _StatementScanner:
    """Brace/paren aware statement splitter over comment-stripped source."""

    def __init__(self, raw: str, state_names: set[str], line_base: int,
                 resolve_base=None, catalog: TypeCatalog | None = None):
        self.raw = raw
        self.scan = strip_comments(raw)
        self.state = state_names
        self.line_base = line_base
        self.resolve = resolve_base or base_identifier
        self.catalog = catalog or EMPTY_CATALOG
        self.unsupported: list[str] = []
        self.has_assembly = False

    def line(self, offset: int) -> int:
        return self.line_base + self.scan.count("\n", 0, offset)

    def text(self, start: int, end: int) -> str:
        return " ".join(self.raw[start:end].split())[:400]

    def statements(self) -> tuple[I.IRStmt, ...]:
        return self.block(0, len(self.scan))

    def block(self, start: int, end: int) -> tuple[I.IRStmt, ...]:
        out: list[I.IRStmt] = []
        i = start
        while i < end:
            ch = self.scan[i]
            if ch.isspace() or ch == ";":
                i += 1
                continue
            if ch == "}":
                i += 1
                continue
            if ch == "{":
                close = _matching(self.scan, i, "{", "}")
                out.append(I.IRStmt(
                    I.BLOCK, self.line(i), self.text(i, min(close, end)),
                    body=self.block(i + 1, min(close, end) - 1),
                ))
                i = close
                continue
            word = re.match(r"[A-Za-z_]\w*", self.scan[i:end])
            keyword = word.group(0) if word else ""
            if keyword in {"if"}:
                i = self._if(out, i, end)
            elif keyword in {"for", "while"}:
                i = self._loop(out, i, end, keyword)
            elif keyword == "do":
                i = self._do(out, i, end)
            elif keyword in {"unchecked", "assembly"}:
                i = self._braced(out, i, end, keyword)
            elif keyword == "try":
                i = self._try(out, i, end)
            else:
                i = self._simple(out, i, end)
        return tuple(out)

    def _statement_end(self, start: int, end: int) -> int:
        depth = 0
        i = start
        while i < end:
            ch = self.scan[i]
            if ch in "([{":
                depth += 1
            elif ch in ")]}":
                if depth == 0:
                    return i
                depth -= 1
            elif ch == ";" and depth == 0:
                return i + 1
            i += 1
        return end

    def _body(self, start: int, end: int) -> tuple[tuple[I.IRStmt, ...], int]:
        i = start
        while i < end and self.scan[i].isspace():
            i += 1
        if i < end and self.scan[i] == "{":
            close = min(_matching(self.scan, i, "{", "}"), end)
            return self.block(i + 1, close - 1), close
        stop = self._statement_end(i, end)
        return self.block(i, stop), stop

    def _if(self, out: list[I.IRStmt], i: int, end: int) -> int:
        popen = self.scan.find("(", i)
        if popen < 0 or popen >= end:
            return self._simple(out, i, end)
        pclose = min(_matching(self.scan, popen, "(", ")"), end)
        cond_text = self.raw[popen + 1:pclose - 1].strip()
        body, after = self._body(pclose, end)
        orelse: tuple[I.IRStmt, ...] = ()
        tail = self.scan[after:end]
        else_match = re.match(r"\s*else\b", tail)
        if else_match:
            orelse, after = self._body(after + else_match.end(), end)
        out.append(I.IRStmt(
            I.IF, self.line(i), self.text(i, pclose),
            condition=self._expr(cond_text, self.line(popen)),
            body=body, orelse=orelse,
            reads=self._state_in(cond_text),
        ))
        return after

    def _loop(self, out: list[I.IRStmt], i: int, end: int, keyword: str) -> int:
        popen = self.scan.find("(", i)
        if popen < 0 or popen >= end:
            return self._simple(out, i, end)
        pclose = min(_matching(self.scan, popen, "(", ")"), end)
        header = self.raw[popen + 1:pclose - 1].strip()
        body, after = self._body(pclose, end)
        out.append(I.IRStmt(
            I.LOOP, self.line(i), self.text(i, pclose),
            condition=self._expr(header, self.line(popen)),
            body=body, reads=self._state_in(header),
            loop_bound=_constant_loop_bound(header),
            note=keyword,
        ))
        return after

    def _do(self, out: list[I.IRStmt], i: int, end: int) -> int:
        body, after = self._body(i + 2, end)
        stop = self._statement_end(after, end)
        out.append(I.IRStmt(
            I.LOOP, self.line(i), self.text(i, stop), body=body, note="do",
        ))
        return stop

    def _braced(self, out: list[I.IRStmt], i: int, end: int, keyword: str) -> int:
        bopen = self.scan.find("{", i)
        if bopen < 0 or bopen >= end:
            return self._simple(out, i, end)
        close = min(_matching(self.scan, bopen, "{", "}"), end)
        if keyword == "assembly":
            self.has_assembly = True
            raw = self.raw[bopen + 1:close - 1]
            # Storage slots touched from Yul cannot be resolved without a
            # storage layout, so assembly records reads only and is reported
            # as unsupported rather than guessed.
            out.append(I.IRStmt(
                I.ASSEMBLY, self.line(i), self.text(i, close),
                reads=self._state_in(raw), note="inline-assembly",
            ))
            self.unsupported.append(f"inline assembly at line {self.line(i)}")
            return close
        out.append(I.IRStmt(
            I.BLOCK, self.line(i), self.text(i, close),
            body=self.block(bopen + 1, close - 1), unchecked=True, note=keyword,
        ))
        return close

    def _try(self, out: list[I.IRStmt], i: int, end: int) -> int:
        bopen = self.scan.find("{", i)
        if bopen < 0 or bopen >= end:
            return self._simple(out, i, end)
        close = min(_matching(self.scan, bopen, "{", "}"), end)
        header = self.raw[i:bopen]
        callee, kind, receiver, value, arguments = _classify_call(
            header, self.catalog)
        body = self.block(bopen + 1, close - 1)
        after = close
        while True:
            match = re.match(r"\s*catch\b[^{]*", self.scan[after:end])
            if not match:
                break
            nxt = self.scan.find("{", after + match.start())
            if nxt < 0 or nxt >= end:
                break
            nclose = min(_matching(self.scan, nxt, "{", "}"), end)
            body = body + self.block(nxt + 1, nclose - 1)
            after = nclose
        out.append(I.IRStmt(
            I.CALL, self.line(i), self.text(i, bopen), body=body, note="try",
            call=I.IRCall(callee, kind or I.EXTERNAL_CALL, self.line(i),
                          self.text(i, bopen), receiver,
                          self._args(arguments, self.line(i)), value),
        ))
        return after

    def _simple(self, out: list[I.IRStmt], i: int, end: int) -> int:
        stop = self._statement_end(i, end)
        raw = self.raw[i:stop].strip().rstrip(";").strip()
        if not raw:
            return max(stop, i + 1)
        out.append(self._classify(raw, self.line(i), self.text(i, stop)))
        return max(stop, i + 1)

    def _expr(self, text: str, line: int) -> I.IRExpr:
        return I.IRExpr(
            "expression", text.strip()[:400], self.resolve(text),
            identifiers=identifiers_in(text), line=line,
        )

    def _args(self, arguments, line: int) -> tuple[I.IRExpr, ...]:
        return tuple(self._expr(text, line) for text in arguments)

    def _state_in(self, text: str) -> tuple[str, ...]:
        found = set(identifiers_in(text)) & self.state
        return tuple(sorted(found))

    def _classify(self, raw: str, line: int, text: str) -> I.IRStmt:
        head = re.match(r"[A-Za-z_]\w*", raw)
        keyword = head.group(0) if head else ""

        if keyword in {"require", "assert"}:
            inner = raw[raw.find("(") + 1:] if "(" in raw else raw
            return I.IRStmt(I.REQUIRE, line, text,
                            condition=self._expr(inner, line),
                            reads=self._state_in(inner), note=keyword)
        if keyword == "revert":
            return I.IRStmt(I.REVERT, line, text, reads=self._state_in(raw))
        if keyword == "emit":
            return I.IRStmt(I.EMIT, line, text, reads=self._state_in(raw))
        if keyword == "return":
            return I.IRStmt(I.RETURN, line, text,
                            value=self._expr(raw[6:], line),
                            reads=self._state_in(raw[6:]))
        if keyword == "delete":
            target = raw[6:].strip()
            base = self.resolve(target)
            return I.IRStmt(I.DELETE, line, text,
                            target=self._expr(target, line),
                            writes=(base,) if base in self.state else (),
                            reads=self._state_in(target))

        lhs, op, rhs = _split_assignment(raw)
        if op:
            base = self.resolve(lhs)
            reads = set(self._state_in(rhs))
            if op != "=" or "[" in lhs or "." in lhs:
                reads |= set(self._state_in(lhs))
            call = None
            if "(" in rhs:
                callee, kind, receiver, value, arguments = _classify_call(
                    rhs, self.catalog)
                if callee:
                    call = I.IRCall(callee, kind, line, text, receiver,
                                    self._args(arguments, line), value)
            # `uint256 stored = _ledger.held(who)` declares a local; `stored = …`
            # assigns to one. Emitting ASSIGN for both loses the fact that the
            # local was *introduced by* that call, which is how a reader learns
            # that a later `stored < amount` is talking about `_ledger`.
            declared = _is_declaration(lhs)
            kind_of_statement = I.VAR_DECL if declared else I.ASSIGN
            # The target of a declaration is the name it introduces, not the
            # type that precedes it: a reader asking "where did `stored` come
            # from?" looks up `stored`, never `uint256 stored`.
            target_text = _declared_name(lhs) if declared else lhs
            return I.IRStmt(
                kind_of_statement, line, text,
                target=self._expr(target_text, line), operator=op,
                value=self._expr(rhs, line), call=call,
                writes=(base,) if base in self.state else (),
                reads=tuple(sorted(reads)),
            )

        update = re.match(r"^(.*?)(\+\+|--)$|^(\+\+|--)(.*)$", raw.strip())
        if update:
            target = (update.group(1) or update.group(4) or "").strip()
            op = update.group(2) or update.group(3)
            base = self.resolve(target)
            return I.IRStmt(
                I.ASSIGN, line, text, target=self._expr(target, line),
                operator="+=" if op == "++" else "-=",
                value=I.IRExpr("literal", "1", None, line=line),
                writes=(base,) if base in self.state else (),
                reads=self._state_in(target),
            )

        if "(" in raw:
            callee, kind, receiver, value, arguments = _classify_call(
                raw, self.catalog)
            if callee:
                return I.IRStmt(
                    I.CALL, line, text,
                    call=I.IRCall(callee, kind, line, text, receiver,
                                  self._args(arguments, line), value),
                    reads=self._state_in(raw),
                )
        return I.IRStmt(I.UNKNOWN, line, text, reads=self._state_in(raw))


def _split_assignment(raw: str) -> tuple[str, str | None, str]:
    depth = 0
    i = 0
    n = len(raw)
    while i < n:
        ch = raw[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif depth == 0:
            for op in ("+=", "-=", "*=", "/=", "%=", "|=", "&=", "^="):
                if raw.startswith(op, i):
                    return raw[:i].strip(), op, raw[i + len(op):].strip()
            if ch == "=" and not raw.startswith("==", i) and not raw.startswith("=>", i):
                if i and raw[i - 1] in "!<>=+-*/%|&^":
                    i += 1
                    continue
                return raw[:i].strip(), "=", raw[i + 1:].strip()
        i += 1
    return raw, None, ""


def _constant_loop_bound(header: str) -> int | None:
    match = re.search(r"<\s*(\d+)", header or "")
    if match:
        return int(match.group(1))
    return None


def build_ir(body: str, state_names: set[str], line_base: int,
             resolve_base=None, catalog: TypeCatalog | None = None) -> I.IRFunctionBody:
    scanner = _StatementScanner(body, set(state_names), line_base, resolve_base,
                                catalog)
    statements = scanner.statements()
    return I.IRFunctionBody(
        statements=statements,
        has_assembly=scanner.has_assembly,
        unsupported=tuple(scanner.unsupported),
        source=body,
    )


def _special(contract, name, kind, params_text, tail, body, start, line,
             state_names, path, visibility, catalog=None):
    """Build a constructor / receive / fallback record."""
    opening = body.find("{", start)
    end = matching_brace(body, opening)
    inner = body[opening + 1:end - 1]
    declared = line + 1 + body.count("\n", 0, start)

    reads = {n for n in state_names if re.search(r"\b" + re.escape(n) + r"\b", inner)}
    writes = {n for n in state_names if _write_re(n).search(inner)}
    mm = MUT_RE.search(tail)
    function = Function(
        contract.name, name, visibility, mm.group(1) if mm else "stateful",
        [], reads, writes,
        ["<low-level-call>"] if LOWLEVEL_RE.search(inner) else [],
        declared, inner,
    )
    function.params = _parse_params(params_text)
    function.kind = kind
    function.language = SOLIDITY
    function.parser = "regex"
    function.path = str(path)
    function.payable = "payable" in tail
    function.end_line = line + 1 + body.count("\n", 0, end)
    function.ir = build_ir(
        inner, state_names, line + 1 + body.count("\n", 0, opening),
        catalog=catalog,
    )
    return function


def _user_types(body: str, line: int) -> list[ContractType]:
    """Struct and enum declarations of one contract body.

    Read from comment-stripped text (same length, same offsets) so a struct
    mentioned in a comment does not become a type.
    """
    scan = strip_comments(body)
    types: list[ContractType] = []
    for tm in TYPE_DECL_RE.finditer(scan):
        kind, name = tm.group(1), tm.group(2)
        close = matching_brace(scan, tm.end() - 1)
        inner = scan[tm.end():close - 1]
        separator = ";" if kind == "struct" else ","
        members = [" ".join(m.split()) for m in inner.split(separator) if m.strip()]
        types.append(ContractType(
            name, kind, members, line + 1 + body.count("\n", 0, tm.start()),
        ))
    return types


def parse_text(text: str, path: str, catalog: TypeCatalog | None = None) -> list[Contract]:
    """Parse one file. `catalog` carries the project's declared type names so a
    cast to a contract declared elsewhere is not recorded as a call; the file's
    own declarations are always known."""
    result: list[Contract] = []
    catalog = catalog_from_text(text).merged(catalog)

    for cm in CONTRACT_RE.finditer(text):
        brace = text.find("{", cm.start())
        end = matching_brace(text, brace)
        body = text[brace + 1:end - 1]
        line = text.count("\n", 0, cm.start())
        bases = [x.strip().split()[0] for x in (cm.group(3) or "").split(",") if x.strip()]
        # The declaring keyword decides the kind. Reporting an `interface` as a
        # `contract` makes it its own implementation in `build_bindings`, which
        # halves the confidence of every declared-type binding and disables the
        # cross-contract composition the tree-sitter path produces — silently,
        # because the fallback parser is exactly where nobody is watching.
        contract = Contract(cm.group(2), str(path), line + 1, bases=bases,
                            language=SOLIDITY, parser="regex")
        contract.kind = " ".join(cm.group(1).split())
        contract.end_line = text.count("\n", 0, end) + 1

        for vm in VAR_RE.finditer(body):
            depth = body[:vm.start()].count("{") - body[:vm.start()].count("}")
            if depth == 0:
                modifiers = (vm.group("mods") or "").split()
                visibility = next(
                    (m for m in modifiers
                     if m in {"public", "private", "internal"}),
                    "default",
                )
                contract.state_vars.append(StateVar(
                    contract.name, vm.group("name"), vm.group("type"),
                    visibility,
                    line + 1 + body.count("\n", 0, vm.start()),
                    constant="constant" in modifiers,
                    immutable="immutable" in modifiers,
                    initial_value=(vm.group("init") or "").strip(),
                ))

        state_names = {v.name for v in contract.state_vars}
        contract.types = _user_types(body, line)

        for fm in FUNCTION_RE.finditer(body):
            opening = body.find("{", fm.start())
            fend = matching_brace(body, opening)
            fbody = body[opening + 1:fend - 1]
            tail = fm.group(3) or ""
            vm = VIS_RE.search(tail)
            mm = MUT_RE.search(tail)

            reads = {
                n for n in state_names
                if re.search(r"\b" + re.escape(n) + r"\b", fbody)
            }
            writes = {n for n in state_names if _write_re(n).search(fbody)}
            calls = []
            if LOWLEVEL_RE.search(fbody):
                calls.append("<low-level-call>")

            result_mods = [
                name for name in re.findall(
                    r"\b([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*(?=returns|\{|$)", tail
                )
                if name not in MODIFIER_KEYWORDS
            ]

            fn_line = line + 1 + body.count("\n", 0, fm.start())
            returns_match = re.search(r"returns\s*\(([^)]*)\)", tail)
            function = Function(
                contract.name,
                fm.group(1),
                vm.group(1) if vm else "unspecified",
                mm.group(1) if mm else "stateful",
                sorted(set(result_mods))[:16],
                reads, writes, calls,
                fn_line,
                fbody,
            )
            function.params = _parse_params(fm.group(2))
            function.returns = _parse_params(returns_match.group(1) if returns_match else "")
            function.language = SOLIDITY
            function.parser = "regex"
            function.path = str(path)
            function.payable = "payable" in tail
            function.end_line = line + 1 + body.count("\n", 0, fend)
            function.ir = build_ir(
                fbody, state_names, line + 1 + body.count("\n", 0, opening),
                catalog=catalog,
            )
            contract.functions.append(function)

        # Constructors and fallbacks carry ABI information the Foundry backend
        # needs; without them a generated harness would silently deploy with
        # the wrong signature.
        for cm2 in CONSTRUCTOR_RE.finditer(body):
            contract.functions.append(_special(
                contract, "constructor", "constructor", cm2.group(1),
                cm2.group(2) or "", body, cm2.start(), line, state_names, path,
                visibility="public", catalog=catalog,
            ))
        for fm2 in FALLBACK_RE.finditer(body):
            contract.functions.append(_special(
                contract, fm2.group(1), fm2.group(1), "", fm2.group(2) or "",
                body, fm2.start(), line, state_names, path,
                visibility="external", catalog=catalog,
            ))

        for mm in MODIFIER_RE.finditer(body):
            opening = body.find("{", mm.start())
            mend = matching_brace(body, opening)
            mbody = body[opening + 1:mend - 1]
            mline = line + 1 + body.count("\n", 0, mm.start())
            modifier = Function(
                contract.name, mm.group(1), "internal", "stateful", [],
                {n for n in state_names if re.search(r"\b" + re.escape(n) + r"\b", mbody)},
                {n for n in state_names
                 if re.search(r"\b" + re.escape(n) + r"\s*(?:[+\-*/]?=|\+\+|--)", mbody)},
                [], mline, mbody,
            )
            modifier.params = _parse_params(mm.group(2) or "")
            modifier.kind = "modifier"
            modifier.language = SOLIDITY
            modifier.parser = "regex"
            modifier.path = str(path)
            modifier.end_line = line + 1 + body.count("\n", 0, mend)
            modifier.ir = build_ir(
                mbody, state_names, line + 1 + body.count("\n", 0, opening),
                catalog=catalog,
            )
            contract.modifier_definitions.append(modifier)

        result.append(contract)

    return result


def parse_file(path, catalog: TypeCatalog | None = None) -> list[Contract]:
    text = Path(path).read_text(encoding="utf-8", errors="ignore")
    return parse_text(text, str(path), catalog)


# Project-wide declared names; `parse_project` builds this once per language.
type_catalog = catalog_from_paths


def parse_sources(paths) -> list[Contract]:
    solidity = [path for path in paths if Path(path).suffix.lower() == ".sol"]
    catalog = type_catalog(solidity)
    result: list[Contract] = []
    for path in solidity:
        result.extend(parse_file(path, catalog))
    return result


def parse(paths) -> ParseResult:
    return ParseResult(parse_sources(paths), "regex", SOLIDITY)
