"""Optional execution backends.

These are execution backends, not an orchestration layer: Crystal generates the
harness and the properties itself, then asks the tool to run them. It never
consumes another tool's findings — cross-tool correlation belongs to Arcadia.

Backends are opt-in (`crystal validate`) so that `crystal scan` stays a pure
analyzer.

Two rules hold across every backend here:

* **No verdict without a witness.** A property is `HELD` only when the tool
  evaluated it and at least one transition mutating the state it is about
  succeeded; a green run that cannot show that is `VACUOUS`, with the reason
  naming what never succeeded (`verdict.py`).
* **Pre-flight before launch.** Homonym artifacts, zero-balance actors and a
  frozen block are detected from the target, the harness and the config —
  before the tool exists on the machine, let alone runs (`preflight.py`).
"""

from __future__ import annotations

from . import echidna, halmos, medusa, preflight
from .base import (
    BackendCapabilities,
    BackendExecution,
    BackendTraits,
    Property,
    derive_properties,
)
from .preflight import PreflightIssue, PreflightReport
from .verdict import (
    HELD,
    UNSUPPORTED,
    VACUOUS,
    VIOLATED,
    ActionOutcome,
    ExecutionWitness,
    PropertyVerdict,
    WitnessRequired,
)

BACKENDS = {
    "medusa": medusa,
    "echidna": echidna,
    "halmos": halmos,
}

__all__ = [
    "BACKENDS",
    "HELD",
    "UNSUPPORTED",
    "VACUOUS",
    "VIOLATED",
    "ActionOutcome",
    "BackendCapabilities",
    "BackendExecution",
    "BackendTraits",
    "ExecutionWitness",
    "PreflightIssue",
    "PreflightReport",
    "Property",
    "PropertyVerdict",
    "WitnessRequired",
    "backend_names",
    "capabilities",
    "preflight_report",
    "run_backend",
    "traits",
]


def backend_names() -> list[str]:
    return sorted(BACKENDS)


def capabilities() -> dict[str, BackendCapabilities]:
    return {name: module.detect() for name, module in sorted(BACKENDS.items())}


def traits() -> dict[str, BackendTraits]:
    return {name: module.TRAITS for name, module in sorted(BACKENDS.items())}


def preflight_report(project, contracts, backend: str | None = None,
                     protocol_invariants=()) -> PreflightReport:
    """The pre-flight on its own, for callers that want the traps before
    choosing a backend. Without a backend only the target is examined — the
    homonym pass needs nothing else; with one, the properties, harness and
    config that backend would use are checked too."""
    if backend is None:
        homonyms = preflight.find_homonyms(project)
        issues = preflight.homonym_issues(homonyms, list(contracts), "any backend", project)
        return PreflightReport(str(project), "", tuple(issues), homonyms)
    module = BACKENDS.get(backend)
    if module is None:
        raise ValueError(
            f"unknown backend '{backend}'. Available: {', '.join(backend_names())}"
        )
    generated = module.generate(project, contracts, protocol_invariants)
    properties = {}
    harnesses = {}
    configs = {}
    for result in generated:
        contract = next((c for c in contracts if c.name == result.contract), None)
        if contract is None:
            continue
        properties[contract.name], _ = derive_properties(contract, protocol_invariants)
        for filename, content in result.artifacts.items():
            if filename.endswith(".sol"):
                harnesses[contract.name] = content
            elif filename.endswith((".json", ".yaml", ".yml")):
                configs[contract.name] = content
    targets = [c for c in contracts if c.name in properties]
    return preflight.run(
        project, targets, backend, module.TRAITS, properties, harnesses, configs,
        all_contracts=list(contracts),
    )


def run_backend(name: str, project, contracts, protocol_invariants=(),
                generate_only: bool = False, timeout: int = 300):
    """Generate, pre-flight, then — only if nothing blocks — run.

    Every returned `BackendExecution` carries the pre-flight lines that applied
    to its contract, and an executed one carries a `PropertyVerdict` per
    property with its witness attached.
    """
    module = BACKENDS.get(name)
    if module is None:
        raise ValueError(
            f"unknown backend '{name}'. Available: {', '.join(backend_names())}"
        )
    if generate_only:
        return module.generate(project, contracts, protocol_invariants)
    return module.run(project, contracts, protocol_invariants, timeout)
