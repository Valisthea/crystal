"""Filesystem walking that never hands a directory to a reader.

Foundry writes broadcast artifacts into directories named after the script that
produced them — `broadcast/Deploy.s.sol/` is a directory, not a Solidity file.
So `rglob("*.sol")` matches it, and the `read_text()` that follows raises
`IsADirectoryError` on Unix or `PermissionError` on Windows, killing the whole
pass on any Foundry project with deployment history.

The same shape has now bitten twice in two different modules, which is one time
too many for a convention. Every glob that feeds a read goes through here.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

# Directories that never hold target sources, only build output or vendored code.
BUILD_ARTIFACTS = frozenset({
    ".git", "node_modules", "lib", "out", "cache", "artifacts", "target",
})


def rglob_files(root, pattern: str,
                exclude: frozenset[str] = frozenset()) -> Iterator[Path]:
    """Recursively match `pattern` under `root`, yielding real files only.

    `exclude` drops any path with one of those names among its parts.
    """
    for path in Path(root).rglob(pattern):
        if not path.is_file():
            continue
        if exclude and any(part in exclude for part in path.parts):
            continue
        yield path


def glob_files(directory, pattern: str) -> Iterator[Path]:
    """Match `pattern` directly inside `directory`, yielding real files only."""
    for path in Path(directory).glob(pattern):
        if path.is_file():
            yield path
