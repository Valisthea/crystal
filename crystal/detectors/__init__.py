"""Structural detectors.

Detectors read the statement IR and the symbolic effects, never the raw text.
They emit research signals with an ordered trace and a falsification list; the
finding gate remains the only component allowed to weigh evidence.
"""

from __future__ import annotations

from . import access_control, first_depositor, oracle_manipulation, reentrancy
from .base import DetectorSignal

DETECTORS = {
    "reentrancy": reentrancy,
    "access-control": access_control,
    "first-depositor": first_depositor,
    "oracle-manipulation": oracle_manipulation,
}

__all__ = ["DETECTORS", "DetectorSignal", "detector_names", "run_detectors"]


def detector_names() -> list[str]:
    return sorted(DETECTORS)


def run_detectors(contracts, engine=None, selected=None) -> list[DetectorSignal]:
    wanted = set(selected) if selected else set(DETECTORS)
    signals: list[DetectorSignal] = []
    for name, module in sorted(DETECTORS.items()):
        if name not in wanted:
            continue
        signals.extend(module.detect(contracts, engine))

    merged: dict[str, DetectorSignal] = {}
    for item in signals:
        previous = merged.get(item.id)
        if previous is None or item.confidence > previous.confidence:
            merged[item.id] = item
    return sorted(
        merged.values(),
        key=lambda x: (-x.confidence, x.detector, x.contract, x.function, x.line),
    )
