"""Structural detectors.

Detectors read the statement IR and the symbolic effects, never the raw text.
They emit research signals with an ordered trace and a falsification list; the
finding gate remains the only component allowed to weigh evidence.
"""

from __future__ import annotations

import inspect

from . import (
    access_control,
    first_depositor,
    ignored_outcome,
    oracle_manipulation,
    pipeline_bypass,
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
    "pipeline-bypass": pipeline_bypass,
    "ignored-outcome": ignored_outcome,
}

__all__ = ["DETECTORS", "DetectorSignal", "detector_names", "run_detectors"]


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
                  include_tests: bool = False, wirings=()) -> list[DetectorSignal]:
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
        if "wirings" in inspect.signature(module.detect).parameters:
            signals.extend(module.detect(targets, engine, wirings=wirings))
        else:
            signals.extend(module.detect(targets, engine))
    if not include_tests:
        signals = [
            item for item in signals
            if not _is_test_function(contracts, item)
        ]

    merged: dict[str, DetectorSignal] = {}
    for item in signals:
        previous = merged.get(item.id)
        if previous is None or item.confidence > previous.confidence:
            merged[item.id] = item
    return sorted(
        merged.values(),
        key=lambda x: (-x.confidence, x.detector, x.contract, x.function, x.line),
    )
