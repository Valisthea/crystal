"""Path constraints collected during symbolic execution.

Crystal records the guards it actually observed in the source. It never invents
a precondition, so an empty constraint set means "nothing was proven", not
"no restriction applies".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REQUIRE = "require"
ASSERT = "assert"
BRANCH = "branch"
NEGATED_BRANCH = "negated-branch"
LOOP = "loop"

UINT_MAX = 2 ** 256 - 1
DEFAULT_SAMPLE_MAX = 2 ** 16 - 1

TYPE_DOMAINS = {
    "bool": (0, 1),
    "address": (0, 2 ** 160 - 1),
    "uint8": (0, 2 ** 8 - 1),
    "uint16": (0, 2 ** 16 - 1),
    "uint32": (0, 2 ** 32 - 1),
    "uint64": (0, 2 ** 64 - 1),
    "uint128": (0, 2 ** 128 - 1),
    "uint256": (0, UINT_MAX),
    "u8": (0, 2 ** 8 - 1),
    "u16": (0, 2 ** 16 - 1),
    "u32": (0, 2 ** 32 - 1),
    "u64": (0, 2 ** 64 - 1),
    "u128": (0, 2 ** 128 - 1),
}


@dataclass(frozen=True)
class Constraint:
    kind: str
    expression: str
    line: int
    origin: str = ""

    def render(self) -> str:
        prefix = {NEGATED_BRANCH: "not "}.get(self.kind, "")
        return f"{prefix}{self.expression}"


@dataclass(frozen=True)
class ConstraintSystem:
    """The v1 `ConstraintSet` shape plus provenance-carrying records."""

    expressions: tuple[str, ...] = ()
    parameter_ranges: dict[str, tuple[int, int]] = field(default_factory=dict)
    constraints: tuple[Constraint, ...] = ()
    parameter_types: dict[str, str] = field(default_factory=dict)
    unsupported: tuple[str, ...] = ()

    @property
    def guards(self) -> tuple[str, ...]:
        return tuple(c.render() for c in self.constraints)


def domain_for(type_name: str) -> tuple[int, int]:
    cleaned = re.sub(r"\s+", "", (type_name or "").lower())
    cleaned = cleaned.replace("memory", "").replace("calldata", "").replace("storage", "")
    if cleaned in TYPE_DOMAINS:
        return TYPE_DOMAINS[cleaned]
    if cleaned.startswith(("uint", "u")) and cleaned not in TYPE_DOMAINS:
        return 0, UINT_MAX
    if cleaned.startswith(("int", "i")):
        return -(2 ** 255), 2 ** 255 - 1
    return 0, DEFAULT_SAMPLE_MAX


def boundary_values(type_name: str) -> tuple[int, ...]:
    """Boundary-first sample points for a type; no random fabrication."""
    low, high = domain_for(type_name)
    cleaned = re.sub(r"\s+", "", (type_name or "").lower())
    if cleaned in {"bool"}:
        return 0, 1
    if cleaned.startswith("address"):
        return 0, 1, 2
    candidates = [low, low + 1, 2, 10, 100, high // 2, high - 1, high]
    return tuple(dict.fromkeys(value for value in candidates if low <= value <= high))
