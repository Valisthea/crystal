"""Boundary engine: auto-propose x-1, x, x+1 test cases for numeric comparisons.

When Crystal sees a comparison (< <= > >= == !=) on a temporal, numeric, or
authorization value, it generates boundary test cases. This is the difference
between "expiry is checked" and "expiry is checked at the exact boundary".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True)
class BoundaryCase:
    """A single boundary test case."""
    variable: str
    category: str
    operator: str
    threshold: str
    test_values: tuple[str, ...]
    location: str
    function: str
    rationale: str


@dataclass(frozen=True)
class BoundaryProposal:
    """Collection of boundary cases for a function."""
    function: str
    contract: str
    cases: tuple[BoundaryCase, ...]
    confidence: float


COMPARISON_RE = re.compile(
    r"(\w[\w.]*)\s*([<>!=]=?)\s*(\w[\w.]*)"
)

TEMPORAL_HINTS = frozenset({
    "expiry", "expiration", "deadline", "validuntil", "timestamp",
    "lockeduntil", "cooldown", "duration", "lastupdated", "block.timestamp",
    "block.number", "now",
})

NUMERIC_HINTS = frozenset({
    "balance", "amount", "value", "price", "fee", "tip", "nonce",
    "counter", "index", "threshold", "limit", "max", "min",
    "allowance", "supply", "premium", "duration",
})


def _is_temporal(name: str) -> bool:
    lower = name.lower().replace("_", "")
    return lower in TEMPORAL_HINTS or any(h in lower for h in TEMPORAL_HINTS)


def _is_numeric(name: str) -> bool:
    lower = name.lower().replace("_", "")
    return lower in NUMERIC_HINTS or any(h in lower for h in NUMERIC_HINTS)


def _category(name: str) -> str:
    if _is_temporal(name):
        return "temporal"
    if _is_numeric(name):
        return "numeric"
    return "other"


def _boundary_values(var: str, op: str, threshold: str) -> tuple[str, ...]:
    """Generate boundary test values for a comparison."""
    return (
        f"{threshold} - 1",
        f"{threshold}",
        f"{threshold} + 1",
    )


def propose_boundaries(contracts) -> list[BoundaryProposal]:
    """Scan contracts for comparisons and propose boundary test cases."""
    proposals: list[BoundaryProposal] = []

    for contract in contracts:
        for function in contract.functions:
            if not function.body and not function.has_ir:
                continue

            source = function.body or ""
            if function.ir:
                source = function.ir.source or source

            cases: list[BoundaryCase] = []
            seen: set[tuple[str, str, str]] = set()

            for match in COMPARISON_RE.finditer(source):
                left, op, right = match.group(1), match.group(2), match.group(3)

                # Skip trivial comparisons (0, 1, true, false).
                if left in ("0", "1", "true", "false"):
                    continue
                if right in ("0", "1", "true", "false"):
                    continue

                # Determine which side is the threshold.
                if _is_temporal(left) or _is_numeric(left):
                    variable, threshold = left, right
                elif _is_temporal(right) or _is_numeric(right):
                    variable, threshold = right, left
                else:
                    continue

                key = (variable, op, threshold)
                if key in seen:
                    continue
                seen.add(key)

                cat = _category(variable)
                test_vals = _boundary_values(variable, op, threshold)

                rationale = f"{variable} {op} {threshold}"
                if cat == "temporal":
                    rationale = f"temporal boundary: {rationale}"
                elif cat == "numeric":
                    rationale = f"numeric boundary: {rationale}"

                cases.append(BoundaryCase(
                    variable=variable,
                    category=cat,
                    operator=op,
                    threshold=threshold,
                    test_values=test_vals,
                    location=f"{contract.path}:{function.line}",
                    function=f"{contract.name}.{function.name}",
                    rationale=rationale,
                ))

            if cases:
                proposals.append(BoundaryProposal(
                    function=f"{contract.name}.{function.name}",
                    contract=contract.name,
                    cases=tuple(cases),
                    confidence=min(0.80, 0.50 + 0.05 * len(cases)),
                ))

    return sorted(proposals, key=lambda p: (-p.confidence, p.function))
