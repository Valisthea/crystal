"""Backwards-compatible entry point for the regex Solidity parser.

The implementation moved to `crystal.parsers.solidity_regex` in v2. This module
keeps the v1 import path working (`from crystal.parser import parse_sources`).
"""

from .parsers.base import matching_brace
from .parsers.solidity_regex import (
    CONTRACT_RE,
    FUNCTION_RE,
    LOWLEVEL_RE,
    MUT_RE,
    VAR_RE,
    VIS_RE,
    build_ir,
    parse_file,
    parse_sources,
    parse_text,
)

__all__ = [
    "CONTRACT_RE",
    "FUNCTION_RE",
    "LOWLEVEL_RE",
    "MUT_RE",
    "VAR_RE",
    "VIS_RE",
    "build_ir",
    "matching_brace",
    "parse_file",
    "parse_sources",
    "parse_text",
]
