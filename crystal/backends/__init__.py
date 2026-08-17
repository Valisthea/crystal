"""Optional execution backends.

These are execution backends, not an orchestration layer: Crystal generates the
harness and the properties itself, then asks the tool to run them. It never
consumes another tool's findings — cross-tool correlation belongs to Arcadia.

Backends are opt-in (`crystal validate`) so that `crystal scan` stays a pure
analyzer.
"""

from __future__ import annotations

from . import echidna, halmos, medusa
from .base import BackendCapabilities, BackendExecution, Property

BACKENDS = {
    "medusa": medusa,
    "echidna": echidna,
    "halmos": halmos,
}

__all__ = [
    "BACKENDS",
    "BackendCapabilities",
    "BackendExecution",
    "Property",
    "backend_names",
    "capabilities",
    "run_backend",
]


def backend_names() -> list[str]:
    return sorted(BACKENDS)


def capabilities() -> dict[str, BackendCapabilities]:
    return {name: module.detect() for name, module in sorted(BACKENDS.items())}


def run_backend(name: str, project, contracts, protocol_invariants=(),
                generate_only: bool = False, timeout: int = 300):
    module = BACKENDS.get(name)
    if module is None:
        raise ValueError(
            f"unknown backend '{name}'. Available: {', '.join(backend_names())}"
        )
    if generate_only:
        return module.generate(project, contracts, protocol_invariants)
    return module.run(project, contracts, protocol_invariants, timeout)
