"""Multi-language front-end dispatcher.

`parse_sources` routes every discovered file to the strongest parser available
for its language and degrades to a documented fallback instead of failing.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..models import MOVE, RUST, SOLIDITY, VYPER, Contract
from . import move_ts, rust_ts, solidity_regex, solidity_ts, vyper_ts
from .base import (
    LANGUAGE_BY_SUFFIX,
    SUPPORTED_SUFFIXES,
    ParseDiagnostic,
    ParseResult,
    detect_language,
)

__all__ = [
    "LANGUAGE_BY_SUFFIX",
    "SUPPORTED_SUFFIXES",
    "ParseResult",
    "detect_language",
    "parse_sources",
    "parser_report",
    "treesitter_enabled",
]


def treesitter_enabled() -> bool:
    return os.environ.get("CRYSTAL_NO_TREESITTER", "").strip() not in {"1", "true", "yes"}


def _solidity_backend():
    if treesitter_enabled() and solidity_ts.available():
        return solidity_ts, "tree-sitter"
    return solidity_regex, "regex"


BACKENDS = {
    SOLIDITY: _solidity_backend,
    RUST: lambda: (rust_ts, "tree-sitter") if treesitter_enabled() and rust_ts.available()
    else (rust_ts, "unavailable"),
    MOVE: lambda: (move_ts, move_ts.backend_name()),
    VYPER: lambda: (vyper_ts, vyper_ts.backend_name()),
}


def parse_sources(paths) -> list[Contract]:
    """Parse every supported source file into Crystal contract records."""
    return parse_project(paths).contracts


def parse_project(paths) -> ParseResult:
    result = ParseResult([], "mixed", "mixed")
    grouped: dict[str, list[Path]] = {}
    for path in paths:
        language = detect_language(path)
        if language is None:
            continue
        grouped.setdefault(language, []).append(Path(path))

    used: list[str] = []
    for language, files in sorted(grouped.items()):
        factory = BACKENDS.get(language)
        if factory is None:
            continue
        module, backend = factory()
        if backend == "unavailable":
            result.diagnostics.append(ParseDiagnostic(
                str(files[0].parent), "warning",
                f"{language}: no parser available ({module.status()}); "
                f"{len(files)} file(s) skipped",
            ))
            continue
        used.append(f"{language}:{backend}")
        for path in files:
            try:
                result.contracts.extend(module.parse_file(path))
            except (OSError, ValueError, RecursionError) as exc:
                result.diagnostics.append(
                    ParseDiagnostic(str(path), "error", f"parse failed: {exc}")
                )
    result.parser = ",".join(sorted(set(used))) or "none"
    return result


def parser_report() -> dict:
    """Machine-readable parser availability, used by `crystal doctor`."""
    solidity_backend = _solidity_backend()[1]
    return {
        "treesitter_enabled": treesitter_enabled(),
        "solidity": {
            "backend": solidity_backend,
            "treesitter_available": solidity_ts.available(),
            "status": solidity_ts.status(),
            "fallback": "regex",
        },
        "rust": {
            "backend": "tree-sitter" if rust_ts.available() else "unavailable",
            "treesitter_available": rust_ts.available(),
            "status": rust_ts.status(),
            "fallback": None,
        },
        "move": {
            "backend": move_ts.backend_name(),
            "treesitter_available": move_ts.available(),
            "status": move_ts.status(),
            "fallback": "regex",
        },
        "vyper": {
            "backend": vyper_ts.backend_name(),
            "treesitter_available": vyper_ts.available(),
            "status": vyper_ts.status(),
            "fallback": "regex",
        },
    }
