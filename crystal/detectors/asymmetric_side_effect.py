"""Asymmetric side-effect detector.

Multiple code paths perform the same primary operation (e.g. increase_balance),
but some paths omit a companion side-effect (e.g. record_transfer). This is
not a bug in one function — it is an asymmetry between paths supposed to be
equivalent. The protocol expects that EVERY credit is accompanied by an
accounting record, but some paths forget.

F3 benchmark (High confirmed on Quantus): increase_balance is called in 4
sites; record_transfer is called in only 2 of them. The 2 sites without
record_transfer credit funds that have no zk-tree leaf, so they are frozen
forever in the bridge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .base import DetectorSignal, signal

DETECTOR = "asymmetric-side-effect"

# Operations that move value — the primary operations we scan for.
VALUE_OPERATIONS = frozenset({
    "increase_balance", "decrease_balance",
    "deposit", "withdraw",
    "mint_into", "burn_from", "mint", "burn",
    "transfer", "deposit_creating", "deposit_into_existing",
    "slash", "reward",
    "credit", "debit",
})

# Patterns that suggest an operation is initialisation/genesis (one-shot).
GENESIS_HINTS = re.compile(
    r"genesis|initialize|init|bootstrap|setup|on_genesis", re.IGNORECASE
)


@dataclass
class _CallSite:
    function: str
    contract: str
    line: int
    path: str
    callee: str
    companions: set[str] = field(default_factory=set)
    is_test: bool = False
    is_genesis: bool = False
    language: str = "rust"


class _Anchor:
    """Signals anchor on a function; a call site anchors on its enclosing one."""

    def __init__(self, site):
        self.contract = site.contract
        self.name = site.function.split(".")[-1]
        self.line = site.line
        self.path = site.path
        self.language = site.language
        self.is_entry_point = True


def _collect_call_sites(contracts, include_tests: bool = False):
    """Find all call sites of value operations with their companion calls."""
    sites: list[_CallSite] = []

    for contract in contracts:
        if contract.is_test and not include_tests:
            continue

        for function in contract.functions:
            if function.is_test and not include_tests:
                continue

            # Collect all callees in this function.
            callees: set[str] = set()
            value_calls: list[tuple[str, int]] = []

            if function.ir:
                for stmt in function.ir.walk():
                    if stmt.call is not None:
                        callee = stmt.call.callee.rsplit("::", 1)[-1]
                        callee_lower = callee.lower().replace("_", "")
                        callees.add(callee)

                        base_callee = callee.split("(")[0].strip()
                        if base_callee in VALUE_OPERATIONS:
                            value_calls.append((base_callee, stmt.call.line))
            else:
                for call_name in function.calls:
                    base = call_name.rsplit("::", 1)[-1].split("(")[0].strip()
                    callees.add(base)
                    if base in VALUE_OPERATIONS:
                        value_calls.append((base, function.line))

            is_genesis = bool(GENESIS_HINTS.search(function.name))

            for callee, line in value_calls:
                companions = callees - {callee} - VALUE_OPERATIONS
                sites.append(_CallSite(
                    function=f"{contract.name}.{function.name}",
                    contract=contract.name,
                    line=line,
                    path=function.path or contract.path,
                    callee=callee,
                    companions=companions,
                    is_test=function.is_test,
                    is_genesis=is_genesis,
                    language=getattr(function, "language", "rust"),
                ))

    return sites


def _find_asymmetries(sites: list[_CallSite]):
    """Find companion functions that appear in most but not all sites."""
    # Group sites by primary operation.
    by_operation: dict[str, list[_CallSite]] = {}
    for site in sites:
        by_operation.setdefault(site.callee, []).append(site)

    asymmetries: list[dict] = []

    for operation, op_sites in by_operation.items():
        # Filter out test and genesis sites.
        production_sites = [
            s for s in op_sites if not s.is_test and not s.is_genesis
        ]
        if len(production_sites) < 2:
            continue

        # Find companion functions present in >= 2 sites.
        companion_counts: dict[str, int] = {}
        for site in production_sites:
            for comp in site.companions:
                companion_counts[comp] = companion_counts.get(comp, 0) + 1

        for companion, count in companion_counts.items():
            if count < 2:
                continue
            total = len(production_sites)
            if count >= total:
                continue

            with_companion = [s for s in production_sites if companion in s.companions]
            without_companion = [s for s in production_sites if companion not in s.companions]

            if not without_companion:
                continue

            confidence = round(min(0.85, count / total), 3)

            asymmetries.append({
                "operation": operation,
                "companion": companion,
                "with": with_companion,
                "without": without_companion,
                "confidence": confidence,
                "total": total,
                "count": count,
            })

    return asymmetries


def detect(
    contracts,
    engine=None,
    include_tests: bool = False,
    **kwargs,
) -> list[DetectorSignal]:
    """Run the asymmetric side-effect detector."""
    sites = _collect_call_sites(contracts, include_tests=include_tests)
    asymmetries = _find_asymmetries(sites)
    signals: list[DetectorSignal] = []

    for asym in asymmetries:
        for missing_site in asym["without"]:
            evidence = [
                f"primary operation: {asym['operation']}",
                f"expected companion: {asym['companion']}",
                f"companion present in {asym['count']}/{asym['total']} sites",
            ]
            for ws in asym["with"][:3]:
                evidence.append(
                    f"  companion present: {ws.function} ({ws.path}:{ws.line})"
                )
            for wos in asym["without"][:3]:
                evidence.append(
                    f"  companion MISSING: {wos.function} ({wos.path}:{wos.line})"
                )

            observed_instead = sorted(
                missing_site.companions - {asym["companion"]}
            )[:3]
            if observed_instead:
                evidence.append(
                    f"observed instead: {', '.join(observed_instead)}"
                )
            else:
                evidence.append("observed instead: nothing")

            ordered_trace = [
                f"L{missing_site.line} call: {asym['operation']}",
                f"  expected: {asym['companion']} (present in "
                f"{asym['count']}/{asym['total']} equivalent sites)",
                f"  actual: {'none' if not observed_instead else ', '.join(observed_instead)}",
            ]

            falsification = [
                f"Is there a sweep/backfill that calls {asym['companion']} retroactively?",
                f"Is {asym['companion']} called elsewhere with the same key?",
                "Is the missing side-effect intentional (documented exception)?",
                f"Does {asym['operation']} have a different accounting path in this context?",
            ]

            signals.append(signal(
                detector=DETECTOR,
                title=(
                    f"{asym['operation']} without {asym['companion']} "
                    f"in {missing_site.function}"
                ),
                function=_Anchor(missing_site),
                line=missing_site.line,
                confidence=asym["confidence"],
                reason=(
                    f"{missing_site.function} calls {asym['operation']} without "
                    f"the companion {asym['companion']} that "
                    f"{asym['count']}/{asym['total']} equivalent sites include"
                ),
                evidence=evidence,
                ordered_trace=ordered_trace,
                falsification=falsification,
            ))

    return sorted(signals, key=lambda s: (-s.confidence, s.contract, s.function))
