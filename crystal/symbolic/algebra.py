"""Canonical symbolic arithmetic used by the state-delta engine.

Values are multivariate polynomials over opaque symbols with integer
coefficients. Canonical ordering means two expressions that are semantically
equal render to the same string, so `delta(a) == delta(b)` is a meaningful
comparison instead of a textual coincidence.

Operations that the model cannot represent exactly (bitwise, modulo,
non-constant division, unresolved calls) collapse into a named opaque symbol
rather than silently producing a wrong number.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

Monomial = tuple  # tuple[str, ...] of symbol names, sorted; () is the constant

ARG_PREFIX = "ARG:"
STATE_PREFIX = "S0:"
ENV_PREFIX = "ENV:"
RETURN_PREFIX = "RET:"
SENDER = "SENDER"

SOLIDITY_UNITS = {
    "wei": 1, "gwei": 10 ** 9, "szabo": 10 ** 12, "finney": 10 ** 15,
    "ether": 10 ** 18, "seconds": 1, "minutes": 60, "hours": 3600,
    "days": 86400, "weeks": 604800,
}


@dataclass(frozen=True)
class SymExpr:
    terms: tuple[tuple[Monomial, int], ...] = ()

    # -- constructors ----------------------------------------------------
    @staticmethod
    def zero() -> "SymExpr":
        return SymExpr(())

    @staticmethod
    def const(value: int) -> "SymExpr":
        return SymExpr((((), value),)) if value else SymExpr(())

    @staticmethod
    def symbol(name: str) -> "SymExpr":
        return SymExpr((((name,), 1),))

    # -- algebra ---------------------------------------------------------
    def _accumulate(self, other_terms, sign: int = 1) -> "SymExpr":
        merged: dict[Monomial, int] = {m: c for m, c in self.terms}
        for monomial, coefficient in other_terms:
            merged[monomial] = merged.get(monomial, 0) + sign * coefficient
        return SymExpr(_canonical(merged))

    def __add__(self, other: "SymExpr") -> "SymExpr":
        return self._accumulate(other.terms, 1)

    def __sub__(self, other: "SymExpr") -> "SymExpr":
        return self._accumulate(other.terms, -1)

    def __neg__(self) -> "SymExpr":
        return SymExpr(tuple((m, -c) for m, c in self.terms))

    def __mul__(self, other: "SymExpr") -> "SymExpr":
        merged: dict[Monomial, int] = {}
        for left_monomial, left_coefficient in self.terms:
            for right_monomial, right_coefficient in other.terms:
                monomial = tuple(sorted(left_monomial + right_monomial))
                merged[monomial] = (
                    merged.get(monomial, 0) + left_coefficient * right_coefficient
                )
        return SymExpr(_canonical(merged))

    def divide(self, other: "SymExpr") -> "SymExpr":
        if other.is_zero:
            return SymExpr.symbol("DIV_BY_ZERO")
        if self == other:
            return SymExpr.const(1)
        if self.is_zero:
            return SymExpr.zero()
        divisor = other.constant_value
        if divisor is not None and divisor != 0:
            if all(coefficient % divisor == 0 for _, coefficient in self.terms):
                return SymExpr(_canonical({
                    m: c // divisor for m, c in self.terms
                }))
        return SymExpr.symbol(f"DIV({self.render()}/{other.render()})")

    def opaque(self, label: str) -> "SymExpr":
        return SymExpr.symbol(f"{label}({self.render()})")

    def substitute(self, mapping: dict[str, "SymExpr"]) -> "SymExpr":
        if not mapping:
            return self
        result = SymExpr.zero()
        for monomial, coefficient in self.terms:
            product = SymExpr.const(coefficient)
            for name in monomial:
                product = product * mapping.get(name, SymExpr.symbol(name))
            result = result + product
        return result

    def rename(self, suffix: str) -> "SymExpr":
        """Tag every non-state symbol so repeated calls stay distinguishable."""
        if not suffix:
            return self
        renamed: dict[Monomial, int] = {}
        for monomial, coefficient in self.terms:
            key = tuple(sorted(
                name if name.startswith(STATE_PREFIX) else f"{name}{suffix}"
                for name in monomial
            ))
            renamed[key] = renamed.get(key, 0) + coefficient
        return SymExpr(_canonical(renamed))

    # -- inspection ------------------------------------------------------
    @property
    def is_zero(self) -> bool:
        return not self.terms

    @property
    def constant_value(self) -> int | None:
        if not self.terms:
            return 0
        if len(self.terms) == 1 and self.terms[0][0] == ():
            return self.terms[0][1]
        return None

    @property
    def symbols(self) -> tuple[str, ...]:
        found: list[str] = []
        for monomial, _ in self.terms:
            found.extend(monomial)
        return tuple(dict.fromkeys(found))

    def depends_on_arguments(self) -> bool:
        return any(s.startswith(ARG_PREFIX) for s in self.symbols)

    def render(self) -> str:
        if not self.terms:
            return "0"
        parts: list[str] = []
        for index, (monomial, coefficient) in enumerate(self.terms):
            body = "*".join(monomial)
            magnitude = abs(coefficient)
            if not monomial:
                piece = str(magnitude)
            elif magnitude == 1:
                piece = body
            else:
                piece = f"{magnitude}*{body}"
            if index == 0:
                parts.append(f"-{piece}" if coefficient < 0 else piece)
            else:
                parts.append(f" - {piece}" if coefficient < 0 else f" + {piece}")
        return "".join(parts)

    def __str__(self) -> str:
        return self.render()


def _canonical(merged: dict[Monomial, int]) -> tuple[tuple[Monomial, int], ...]:
    return tuple(sorted(
        ((monomial, coefficient) for monomial, coefficient in merged.items()
         if coefficient),
        key=lambda item: (len(item[0]), item[0], item[1]),
    ))


ZERO = SymExpr.zero()


# ---------------------------------------------------------------------------
# Expression reader
# ---------------------------------------------------------------------------

TOKEN_RE = re.compile(r"""
    (?P<space>\s+)
  | (?P<number>0[xX][0-9a-fA-F_]+|\d[\d_]*)
  | (?P<name>[A-Za-z_$]\w*)
  | (?P<string>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
  | (?P<op><<=|>>=|\*\*|&&|\|\||==|!=|<=|>=|<<|>>|::|->|=>|\?|:|[-+*/%()\[\]{},.!~&|^<>=;@])
""", re.VERBOSE)

COMPARISONS = {"==", "!=", "<", ">", "<=", ">="}
OPAQUE_BINARY = {"%", "&", "|", "^", "<<", ">>", "**"}


def tokenize(text: str) -> list[tuple[str, str]]:
    tokens: list[tuple[str, str]] = []
    position = 0
    length = len(text or "")
    while position < length:
        match = TOKEN_RE.match(text, position)
        if match is None:
            position += 1
            continue
        position = match.end()
        kind = match.lastgroup
        if kind == "space":
            continue
        tokens.append((kind, match.group()))
    return tokens


class ExpressionReader:
    """Recursive-descent reader turning source text into a `SymExpr`.

    `resolve(path)` maps a fully-qualified access path (`totalAssets`,
    `balances[SENDER]`, `msg.value`) to its current symbolic value.
    """

    def __init__(self, resolve, note=None, suffix: str = ""):
        self.resolve = resolve
        self.note = note or (lambda message: None)
        self.suffix = suffix
        self.tokens: list[tuple[str, str]] = []
        self.position = 0

    def read(self, text: str) -> SymExpr:
        self.tokens = tokenize(text)
        self.position = 0
        if not self.tokens:
            return ZERO
        value = self._ternary()
        return value

    # -- token helpers ---------------------------------------------------
    def _peek(self) -> str:
        return self.tokens[self.position][1] if self.position < len(self.tokens) else ""

    def _peek_kind(self) -> str:
        return self.tokens[self.position][0] if self.position < len(self.tokens) else ""

    def _next(self) -> str:
        token = self._peek()
        self.position += 1
        return token

    def _accept(self, *values: str) -> str | None:
        if self._peek() in values:
            return self._next()
        return None

    # -- grammar ---------------------------------------------------------
    def _ternary(self) -> SymExpr:
        condition = self._logical()
        if self._accept("?"):
            left = self._ternary()
            self._accept(":")
            right = self._ternary()
            if left == right:
                return left
            return SymExpr.symbol(
                f"PHI({left.render()}|{right.render()})"
            )
        return condition

    def _logical(self) -> SymExpr:
        value = self._comparison()
        while self._peek() in {"&&", "||"}:
            operator = self._next()
            other = self._comparison()
            value = SymExpr.symbol(
                f"BOOL({value.render()}{operator}{other.render()})"
            )
        return value

    def _comparison(self) -> SymExpr:
        value = self._additive()
        while self._peek() in COMPARISONS:
            operator = self._next()
            other = self._additive()
            value = SymExpr.symbol(
                f"COND({value.render()}{operator}{other.render()})"
            )
        return value

    def _additive(self) -> SymExpr:
        value = self._multiplicative()
        while self._peek() in {"+", "-"}:
            operator = self._next()
            other = self._multiplicative()
            value = value + other if operator == "+" else value - other
        return value

    def _multiplicative(self) -> SymExpr:
        value = self._unary()
        while self._peek() in {"*", "/"} or self._peek() in OPAQUE_BINARY:
            operator = self._next()
            other = self._unary()
            if operator == "*":
                value = value * other
            elif operator == "/":
                value = value.divide(other)
            else:
                self.note(f"opaque operator '{operator}'")
                value = SymExpr.symbol(
                    f"OP{operator}({value.render()},{other.render()})"
                )
        return value

    def _unary(self) -> SymExpr:
        token = self._peek()
        if token in {"-", "!", "~", "*", "&", "+"}:
            self._next()
            value = self._unary()
            if token == "-":
                return -value
            if token in {"*", "&", "+"}:
                return value
            return SymExpr.symbol(f"NOT({value.render()})")
        return self._postfix()

    def _postfix(self) -> SymExpr:
        path = self._primary_path()
        if path is not None:
            return self._resolve_path(path)
        return self._primary_value()

    def _primary_path(self) -> str | None:
        """Read an access chain (`a.b[c]`) and return its canonical path."""
        if self._peek_kind() != "name":
            return None
        start = self.position
        path = self._next()
        while True:
            token = self._peek()
            if token == "." and self.position + 1 < len(self.tokens) \
                    and self.tokens[self.position + 1][0] == "name":
                self._next()
                path += "." + self._next()
                continue
            if token == "[":
                self._next()
                index = self._ternary()
                self._accept("]")
                path += f"[{index.render()}]"
                continue
            if token == "(":
                self.position = start
                return None
            break
        return path

    def _primary_value(self) -> SymExpr:
        kind = self._peek_kind()
        token = self._peek()
        if token == "(":
            self._next()
            value = self._ternary()
            while self._peek() == ",":
                self._next()
                self._ternary()
            self._accept(")")
            return value
        if kind == "number":
            self._next()
            value = _number(token)
            unit = self._peek()
            if unit in SOLIDITY_UNITS:
                self._next()
                value *= SOLIDITY_UNITS[unit]
            return SymExpr.const(value)
        if kind == "string":
            self._next()
            return SymExpr.symbol("LITERAL:string")
        if kind == "name":
            return self._call()
        self._next()
        return ZERO

    def _call(self) -> SymExpr:
        name = self._next()
        while self._peek() in {".", "::"} and self.position + 1 < len(self.tokens) \
                and self.tokens[self.position + 1][0] == "name":
            self._next()
            name += "." + self._next()
        if self._peek() == "{":
            depth = 0
            while self.position < len(self.tokens):
                token = self._next()
                depth += token == "{"
                depth -= token == "}"
                if depth == 0:
                    break
        if self._peek() != "(":
            return self._resolve_path(name)
        self._next()
        arguments: list[SymExpr] = []
        while self._peek() and self._peek() != ")":
            arguments.append(self._ternary())
            if not self._accept(","):
                break
        self._accept(")")
        simple = name.split(".")[-1]
        if _is_cast(simple) and len(arguments) == 1:
            return arguments[0]
        if simple in {"min", "max"} and arguments:
            return SymExpr.symbol(
                f"{simple.upper()}({','.join(a.render() for a in arguments)})"
            )
        self.note(f"unmodelled call '{name}'")
        rendered = ",".join(a.render() for a in arguments)
        value = SymExpr.symbol(f"{RETURN_PREFIX}{name}({rendered}){self.suffix}")
        while self._peek() == ".":
            self._next()
            if self._peek_kind() == "name":
                value = SymExpr.symbol(f"{value.render()}.{self._next()}")
        return value

    def _resolve_path(self, path: str) -> SymExpr:
        return self.resolve(path)


def _number(token: str) -> int:
    cleaned = token.replace("_", "")
    try:
        return int(cleaned, 16) if cleaned[:2].lower() == "0x" else int(cleaned)
    except ValueError:
        return 0


CAST_RE = re.compile(
    r"^(u?int\d*|address|bool|bytes\d*|string|payable|u\d+|i\d+|usize|isize)$"
)


def _is_cast(name: str) -> bool:
    return bool(CAST_RE.match(name))
