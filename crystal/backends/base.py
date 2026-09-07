"""Shared contract for the optional property-testing backends.

Every backend follows the same discipline as the Foundry one: detect the tool,
generate artifacts only from properties Crystal can actually express, run it
when available, and report UNSUPPORTED with a reason instead of inventing a
property that would pass vacuously.

A tool's exit code is not a verdict. `EXECUTED_PASS` means the tool reported
green *and* at least one property carries an execution witness; a green run
in which nothing ever succeeded is `EXECUTED_VACUOUS`, and the per-property
verdicts (`crystal/backends/verdict.py`) say what never happened. A run the
pre-flight refused before launch is `REFUSED`, with the trap named.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field

from .. import process
from ..protocol.grounding import monotonic_variables

UNAVAILABLE = "UNAVAILABLE"
UNSUPPORTED = "UNSUPPORTED"
EXECUTED_PASS = "EXECUTED_PASS"
EXECUTED_FAIL = "EXECUTED_FAIL"
EXECUTED_VACUOUS = "EXECUTED_VACUOUS"
REFUSED = "REFUSED"
TOOL_ERROR = "TOOL_ERROR"
TIMEOUT = "TIMEOUT"
GENERATED = "GENERATED"


def execution_status(verdicts, evaluated: bool) -> str:
    """The run's status from its verdicts, never from the exit code alone.

    Medusa 1.5.1 prints `[PASSED]` for every property after `calls: 0` when
    it finds no method to call, and exits 6; an exit code cannot be trusted in
    either direction. `evaluated` is whether the tool reported evaluating any
    property at all.
    """
    from .verdict import HELD, VIOLATED

    if not evaluated:
        return TOOL_ERROR
    outcomes = {item.verdict for item in verdicts}
    if VIOLATED in outcomes:
        return EXECUTED_FAIL
    if HELD in outcomes:
        return EXECUTED_PASS
    return EXECUTED_VACUOUS


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    available: bool
    version: str | None
    reason: str


@dataclass(frozen=True)
class BackendTraits:
    """What a backend can and cannot do to the environment, declared up front so
    the pre-flight can say which properties it cannot decide.

    * `advances_block` — the tool moves `block.number`/`block.timestamp` during
      a run (Medusa: `blockNumberDelayMax`; Echidna: `maxBlockDelay`). Halmos
      pins both, so any branch behind a delay is unreachable.
    * `funds_senders` — actors start with a native balance the tool provides.
    * `fuzzes_nested_deployments` — the tool calls entry points of contracts the
      harness deploys itself. Measured false on Medusa 1.5.1 ("no methods to
      call"), so the harness must forward every entry point explicitly.
    * `kind` — `fuzzer` picks calls from the harness's own entry points, so a
      harness without handlers can never transition; `symbolic` runs explicit
      tests that call the target themselves.
    * `unmodelled_precompiles` — EVM precompiles the tool cannot execute. A
      symbolic engine that cannot model `sha256` does not fail on a path that
      calls it; it explores forever. Measured on halmos 0.3.3: a settlement
      path through `sha256` ran 26 minutes without a verdict. A property whose
      reachable actions cross one is UNSUPPORTED on that backend, which is a
      result; a run that never terminates is not.
    """

    name: str
    advances_block: bool
    funds_senders: bool
    fuzzes_nested_deployments: bool
    note: str = ""
    kind: str = "fuzzer"
    unmodelled_precompiles: tuple[str, ...] = ()


@dataclass(frozen=True)
class Property:
    name: str
    expression: str
    origin: str
    kind: str
    comment: str = ""
    subject: str = ""


@dataclass(frozen=True)
class BackendExecution:
    backend: str
    status: str
    contract: str
    properties: tuple[str, ...] = ()
    artifacts: dict[str, str] = field(default_factory=dict)
    returncode: int | None = None
    stdout: str = ""
    stderr: str = ""
    counterexamples: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()
    reason: str = ""
    # True only when a property verdict is HELD or VIOLATED, i.e. the run
    # produced evidence someone could reproduce. A vacuous green is not.
    reproducible: bool = False
    # Per-property verdicts with their witness (`verdict.PropertyVerdict`).
    verdicts: tuple = ()
    # Pre-flight lines that applied to this contract, refusals included.
    preflight: tuple[str, ...] = ()

    def verdict_of(self, name: str):
        for item in self.verdicts:
            if item.property == name:
                return item
        return None


def probe(executable: str, *args: str) -> BackendCapabilities:
    path = shutil.which(executable)
    if not path:
        return BackendCapabilities(executable, False, None,
                                   f"{executable} executable not installed")
    result = process.run([path, *(args or ("--version",))], timeout=20)
    if not result.ok:
        return BackendCapabilities(executable, True, None,
                                   f"{executable} probe failed: {result.error}")
    return BackendCapabilities(executable, True, result.first_line() or None, "ok")


def _identifier(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", text or "")
    return re.sub(r"_+", "_", cleaned).strip("_") or "unnamed"


def derive_properties(contract, protocol_invariants=()) -> tuple[list[Property], list[str]]:
    """Mechanically expressible properties, plus the ones Crystal refuses to guess."""
    names = {variable.name for variable in contract.state_vars}
    readable = {
        variable.name for variable in contract.state_vars
        if variable.visibility == "public" and not variable.key_types
    }
    properties: list[Property] = []
    unsupported: list[str] = []

    pairs = []
    for invariant in protocol_invariants:
        if invariant.category != "accounting":
            continue
        found = re.findall(r"([A-Za-z_]\w*)\.([A-Za-z_]\w*)", invariant.expression)
        members = [name for owner, name in found if owner == contract.name]
        if len(members) >= 2:
            pairs.append((members[0], members[1], invariant.expression))

    for left, right, expression in pairs:
        if left in readable and right in readable:
            properties.append(Property(
                f"property_{_identifier(left)}_{_identifier(right)}_coherent",
                f"!(target.{left}() > 0 && target.{right}() == 0)",
                expression, "accounting",
                "shares must not be zero while assets are positive",
            ))
        else:
            unsupported.append(
                f"accounting pair {left}/{right} is not publicly readable; "
                f"no getter to build a property from"
            )

    # Monotonicity earned from the writes, not from the name. `nonce`,
    # `counter` and `index` used to qualify a variable for this property on
    # spelling alone — which asserts monotonicity of anything so named and
    # misses every counter called something else. A variable whose every
    # observed write increments it is monotone because of what the code does.
    # One assigned with a bare `=` is excluded even where it happens to be
    # monotone: the operator does not show it, so Crystal does not claim it.
    for name, writes in sorted(monotonic_variables(contract).items()):
        if name in readable:
            ghost = f"_ghost_{_identifier(name)}"
            properties.append(Property(
                f"property_{_identifier(name)}_monotonic",
                f"target.{name}() >= {ghost}",
                f"{contract.name}.{name}", "monotonicity",
                "every observed write increments it: "
                + ", ".join(f"{w.function} L{w.line}" for w in writes[:4]),
                name,
            ))
        else:
            unsupported.append(
                f"{name} increments on every observed write but is not "
                f"publicly readable; no getter to build a property from"
            )

    if any("balance" in name.lower() for name in names) and not properties:
        unsupported.append(
            "aggregate conservation (sum of balances vs totals) needs an "
            "enumerable holder set; Crystal will not approximate it"
        )
    return properties, unsupported


def ghost_declarations(properties) -> list[str]:
    return [
        f"    uint256 internal _ghost_{_identifier(prop.subject)};"
        for prop in properties if prop.kind == "monotonicity" and prop.subject
    ]
