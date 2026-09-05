"""Multi-language front-end dispatcher.

`parse_sources` routes every discovered file to the strongest parser available
for its language and degrades to a documented fallback instead of failing.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..models import GO, MOVE, RUST, SOLIDITY, VYPER, Contract
from . import go_regex, go_ts, move_ts, rust_ts, solidity_regex, solidity_ts, vyper_ts
from .base import (
    LANGUAGE_BY_SUFFIX,
    SUPPORTED_SUFFIXES,
    ParseDiagnostic,
    ParseResult,
    detect_language,
    is_test_source,
    looks_like_test_contract,
)

__all__ = [
    "GO_REGEX_LIMITATIONS",
    "LANGUAGE_BY_SUFFIX",
    "SOLIDITY_REGEX_LIMITATIONS",
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


def _go_backend():
    if treesitter_enabled() and go_ts.available():
        return go_ts, "tree-sitter"
    return go_regex, "regex"


BACKENDS = {
    SOLIDITY: _solidity_backend,
    RUST: lambda: (rust_ts, "tree-sitter") if treesitter_enabled() and rust_ts.available()
    else (rust_ts, "unavailable"),
    MOVE: lambda: (move_ts, move_ts.backend_name()),
    VYPER: lambda: (vyper_ts, vyper_ts.backend_name()),
    GO: _go_backend,
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
        detailed = getattr(module, "parse_file_detailed", None)
        # Front-ends that resolve casts and struct literals against declared
        # names get one catalog over every file in the group, so a contract
        # declared in another file is recognised where it is cast.
        catalog_builder = getattr(module, "type_catalog", None)
        catalog = catalog_builder(files) if catalog_builder is not None else None
        for path in files:
            try:
                if detailed is not None:
                    contracts, wirings, bindings = detailed(path)
                    result.contracts.extend(contracts)
                    result.wirings.extend(wirings)
                    result.bindings.extend(bindings)
                    continue
                if catalog is not None:
                    result.contracts.extend(module.parse_file(path, catalog))
                else:
                    result.contracts.extend(module.parse_file(path))
            except (OSError, ValueError, RecursionError) as exc:
                result.diagnostics.append(
                    ParseDiagnostic(str(path), "error", f"parse failed: {exc}")
                )
    result.parser = ",".join(sorted(set(used))) or "none"
    _mark_test_contracts(result.contracts)
    return result


def _mark_test_contracts(contracts) -> None:
    """Classify fixtures uniformly across languages.

    Rust carries `#[cfg(test)]`, but Solidity has no attribute for it: a Foundry
    test is a contract inheriting `Test` in a `*.t.sol` file. Both end up with
    the same flag so the pipeline can exclude them from research the same way.
    """
    for contract in contracts:
        if contract.is_test:
            continue
        if is_test_source(contract.path) or looks_like_test_contract(contract):
            contract.is_test = True
            for function in contract.functions:
                function.is_test = True


# What the Solidity regex front-end does not model. Naming the parser is not
# the same as naming what using it costs: a detector that stays quiet because
# the receiver's type could not be resolved looks exactly like a clean result.
# These are the shapes measured to behave differently from the tree-sitter
# front-end, so a report produced on the fallback can say which conclusions it
# is not entitled to draw.
SOLIDITY_REGEX_LIMITATIONS = (
    "import statements are not resolved, so a receiver typed by an imported "
    "contract or interface cannot be identified",
    "`using X for Y` bindings are not modelled, so a call through one is not "
    "attributed to the library it resolves to",
    "user-defined value types (`type X is …`) are treated as ordinary types",
    "a modifier's body is not followed, so guards it applies are seen only by "
    "name",
    "only the first call expression in a statement is followed outward",
    "no call is extracted from a `return` statement, so a helper whose "
    "whole body is `return f(x)` looks callless",
)

# What the Go regex front-end does not model, for the same reason. The
# tree-sitter Go front-end decides interface satisfaction by signature over
# the whole project and merges a package across its files; the fallback can
# do neither, so a call through an interface-typed field stays unresolved and
# the guard-asymmetry grade cannot leave "unresolved" on it.
GO_REGEX_LIMITATIONS = go_regex.GO_REGEX_LIMITATIONS


def parser_report() -> dict:
    """Machine-readable parser availability, used by `crystal doctor`."""
    solidity_backend = _solidity_backend()[1]
    go_backend = _go_backend()[1]
    return {
        "treesitter_enabled": treesitter_enabled(),
        "solidity": {
            "backend": solidity_backend,
            "treesitter_available": solidity_ts.available(),
            "status": solidity_ts.status(),
            "fallback": "regex",
            "reduced_fidelity": solidity_backend == "regex",
            "limitations": list(SOLIDITY_REGEX_LIMITATIONS)
            if solidity_backend == "regex" else [],
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
        "go": {
            "backend": go_backend,
            "treesitter_available": go_ts.available(),
            "status": go_ts.status() if treesitter_enabled() else
            "tree-sitter disabled (CRYSTAL_NO_TREESITTER); regex fallback in use",
            "fallback": "regex",
            "reduced_fidelity": go_backend == "regex",
            "limitations": list(GO_REGEX_LIMITATIONS) if go_backend == "regex" else [],
        },
    }
