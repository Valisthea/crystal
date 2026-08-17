"""Shared contract for the optional property-testing backends.

Every backend follows the same discipline as the Foundry one: detect the tool,
generate artifacts only from properties Crystal can actually express, run it
when available, and report UNSUPPORTED with a reason instead of inventing a
property that would pass vacuously.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field

from .. import process

UNAVAILABLE = "UNAVAILABLE"
UNSUPPORTED = "UNSUPPORTED"
EXECUTED_PASS = "EXECUTED_PASS"
EXECUTED_FAIL = "EXECUTED_FAIL"
TIMEOUT = "TIMEOUT"
GENERATED = "GENERATED"


@dataclass(frozen=True)
class BackendCapabilities:
    name: str
    available: bool
    version: str | None
    reason: str


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
    reproducible: bool = False


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

    for variable in contract.state_vars:
        lowered = variable.name.lower()
        if any(token in lowered for token in ("nonce", "counter", "index")):
            if variable.name in readable:
                ghost = f"_ghost_{_identifier(variable.name)}"
                properties.append(Property(
                    f"property_{_identifier(variable.name)}_monotonic",
                    f"target.{variable.name}() >= {ghost}",
                    f"{contract.name}.{variable.name}", "monotonicity",
                    "counters must never decrease", variable.name,
                ))
            else:
                unsupported.append(
                    f"counter {variable.name} is not publicly readable"
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
