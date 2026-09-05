"""Structural detectors.

Detectors read the statement IR and the symbolic effects, never the raw text.
They emit research signals with an ordered trace and a falsification list; the
finding gate remains the only component allowed to weigh evidence.
"""

from __future__ import annotations

import dataclasses
import inspect

from . import (
    access_control,
    asymmetric_side_effect,
    first_depositor,
    ignored_outcome,
    oracle_manipulation,
    pipeline_guard_bypass,
    reentrancy,
    unbounded_input,
)
from .base import DetectorSignal

DETECTORS = {
    "reentrancy": reentrancy,
    "access-control": access_control,
    "first-depositor": first_depositor,
    "oracle-manipulation": oracle_manipulation,
    "unbounded-input": unbounded_input,
    "pipeline-bypass": pipeline_guard_bypass,
    "ignored-outcome": ignored_outcome,
    "asymmetric-side-effect": asymmetric_side_effect,
}

__all__ = ["DETECTORS", "DetectorSignal", "collapse_signals", "detector_names",
           "run_detectors"]


def detector_names() -> list[str]:
    return sorted(DETECTORS)


def _is_test_function(contracts, item) -> bool:
    for contract in contracts:
        if contract.name != item.contract:
            continue
        for function in contract.functions:
            if function.name == item.function:
                return bool(function.is_test)
    return False


def run_detectors(contracts, engine=None, selected=None,
                  include_tests: bool = False, wirings=(), bindings=(),
                  root=".", sources=()) -> list[DetectorSignal]:
    """Run the selected detectors over production code.

    Test fixtures are excluded by default. A mock runtime or an `ExtBuilder`
    mutates state and skips authority checks by design, so every detector fires
    on it and buries the production signal underneath.
    """
    wanted = set(selected) if selected else set(DETECTORS)
    targets = contracts if include_tests else [
        contract for contract in contracts
        if not getattr(contract, "is_test", False)
    ]
    signals: list[DetectorSignal] = []
    for name, module in sorted(DETECTORS.items()):
        if name not in wanted:
            continue
        # Composition detectors need the runtime wiring; the rest do not, and
        # declaring the parameter is how a detector opts in.
        parameters = inspect.signature(module.detect).parameters
        available = {"wirings": wirings, "bindings": bindings,
                     "root": root, "sources": sources}
        extra = {name: value for name, value in available.items()
                 if name in parameters}
        signals.extend(module.detect(targets, engine, **extra))
    if not include_tests:
        signals = [
            item for item in signals
            if not _is_test_function(contracts, item)
        ]

    return sorted(
        collapse_signals(signals),
        key=lambda x: (-x.confidence, x.detector, x.contract, x.function, x.line),
    )


def collapse_signals(signals) -> list[DetectorSignal]:
    """One (detector, contract, function) yields one signal.

    A detector that anchors on statements reports the same function once per
    matching statement: five `registerPegIn` reentrancy signals are one
    observation with five witnesses, not five observations. The highest
    confidence instance survives (lowest line breaks ties, so the id is
    stable) and the other instances fold into its evidence, keeping their
    lines and any evidence the survivor did not already carry.
    """
    groups: dict[tuple[str, str, str], list[DetectorSignal]] = {}
    for item in signals:
        groups.setdefault((item.detector, item.contract, item.function), []).append(item)
    out: list[DetectorSignal] = []
    for members in groups.values():
        members = sorted(members, key=lambda x: (-x.confidence, x.line, x.id))
        primary, rest = members[0], members[1:]
        if not rest:
            out.append(primary)
            continue
        evidence = list(primary.evidence)
        seen = set(evidence)
        for other in rest:
            extra = [e for e in other.evidence if e not in seen]
            if not extra:
                continue
            evidence.append(
                f"also at line {other.line} (confidence {other.confidence}):"
            )
            for line in extra:
                evidence.append(f"  {line}")
                seen.add(line)
        lines = sorted({m.line for m in members})
        evidence.append(
            f"{len(members)} instances collapsed; lines "
            + ", ".join(str(line) for line in lines)
        )
        out.append(dataclasses.replace(primary, evidence=tuple(evidence)))
    return out
