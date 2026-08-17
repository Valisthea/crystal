"""Common front-end contract shared by every Crystal parser."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import MOVE, RUST, SOLIDITY, VYPER, Contract

LANGUAGE_BY_SUFFIX = {
    ".sol": SOLIDITY,
    ".rs": RUST,
    ".move": MOVE,
    ".vy": VYPER,
    ".vyi": VYPER,
}

SUPPORTED_SUFFIXES = tuple(LANGUAGE_BY_SUFFIX)

IDENTIFIER_RE = re.compile(r"[A-Za-z_]\w*")

COMPOUND_OPS = ("+=", "-=", "*=", "/=", "%=", "|=", "&=", "^=", "<<=", ">>=")


@dataclass
class ParseDiagnostic:
    path: str
    severity: str
    message: str


@dataclass
class ParseResult:
    contracts: list[Contract] = field(default_factory=list)
    parser: str = "regex"
    language: str = SOLIDITY
    diagnostics: list[ParseDiagnostic] = field(default_factory=list)
    wirings: list = field(default_factory=list)

    def extend(self, other: "ParseResult") -> None:
        self.contracts.extend(other.contracts)
        self.diagnostics.extend(other.diagnostics)
        self.wirings.extend(other.wirings)


TEST_PATH_PARTS = {"test", "tests", "mock", "mocks", "testing", "benches",
                   "fixtures", "__tests__", "spec", "specs"}
TEST_STEMS = {"tests", "test", "mock", "mocks", "benchmarking", "fixtures",
              "conftest", "testing"}
TEST_BASE_CONTRACTS = {"test", "dstest", "stdcheats", "stdassertions",
                       "stdinvariant", "basetest", "foundrytest"}
# Deliberately narrow. Excluding production code from research is a worse
# failure than researching a fixture, so only unambiguous markers qualify:
# `Builder`, `Stub` and `Fake` were dropped because real protocols use them.
TEST_NAME_SUFFIXES = ("test", "tests", "mock", "mocks", "harness", "fixture")


def detect_language(path) -> str | None:
    return LANGUAGE_BY_SUFFIX.get(Path(path).suffix.lower())


def is_test_source(path) -> bool:
    """True when the file is a fixture rather than production code."""
    source = Path(path)
    name = source.name.lower()
    if name.endswith((".t.sol", ".spec.sol", "_test.sol", ".test.sol")):
        return True
    if source.stem.lower() in TEST_STEMS:
        return True
    return any(part.lower() in TEST_PATH_PARTS for part in source.parts)


def looks_like_test_contract(contract) -> bool:
    """Fixture by inheritance or by name, for languages without attributes."""
    lowered = contract.name.lower()
    if any(base.lower() in TEST_BASE_CONTRACTS for base in contract.bases):
        return True
    return lowered.endswith(TEST_NAME_SUFFIXES)


def identifiers_in(text: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(IDENTIFIER_RE.findall(text or "")))


def base_identifier(text: str) -> str | None:
    """Root identifier of an lvalue: `a.b[c].d` -> `a`, `self.x` -> `x`."""
    text = (text or "").strip()
    if text.startswith("self."):
        text = text[5:]
    match = IDENTIFIER_RE.match(text)
    return match.group(0) if match else None


def strip_comments(text: str) -> str:
    out = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif ch == "/" and i + 1 < n and text[i + 1] == "*":
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(c if c == "\n" else " " for c in text[i:j]))
            i = j
        elif ch in "\"'":
            quote = ch
            j = i + 1
            while j < n and text[j] != quote:
                j += 2 if text[j] == "\\" else 1
            j = min(j + 1, n)
            out.append(text[i:j])
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def matching_brace(text: str, opening: int) -> int:
    """Index just past the brace matching the one at `opening`."""
    depth = 0
    i = opening
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in "\"'":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
            i += 1
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def split_arguments(text: str) -> list[str]:
    """Split a parameter list on top-level commas.

    Angle brackets are tracked separately from the other bracket kinds: the
    `>` of a Solidity `mapping(a => b)` must not close a generic that was never
    opened, or the split point is lost.
    """
    parts: list[str] = []
    depth = 0
    angle = 0
    current: list[str] = []
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == "<":
            angle += 1
        elif char == ">" and angle > 0 and (index == 0 or text[index - 1] != "="):
            angle -= 1
        if char == "," and depth == 0 and angle == 0:
            parts.append("".join(current).strip())
            current = []
            continue
        current.append(char)
    tail = "".join(current).strip()
    if tail:
        parts.append(tail)
    return [part for part in parts if part]
