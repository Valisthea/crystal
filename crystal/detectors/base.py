"""Shared record type for Crystal's structural detectors.

A detector emits a research signal with an ordered, line-anchored trace and an
explicit falsification list. It never emits a confirmed finding: `status` is
always `RESEARCH`, and the finding gate remains the only place where evidence
is weighed.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..ir import EXTERNAL_CALL_KINDS, IRStmt
from ..quality.normalize import stable_id
from ..vocabulary import (
    AUTH_HELPERS,
    PRIVILEGED_NAME_HINTS,
    REENTRANCY_GUARDS,
    SENDER_TOKENS,
)

STATUS = "RESEARCH"

__all__ = [
    "AUTH_HELPERS", "PRIVILEGED_NAME_HINTS", "REENTRANCY_GUARDS",
    "SENDER_TOKENS", "STATUS", "DetectorSignal", "entry_points",
    "external_call_statements", "guards", "has_modifier", "has_sender_guard",
    "privileged_variables", "signal",
]


@dataclass(frozen=True)
class DetectorSignal:
    id: str
    detector: str
    title: str
    contract: str
    function: str
    path: str
    line: int
    confidence: float
    reason: str
    evidence: tuple[str, ...] = ()
    ordered_trace: tuple[str, ...] = ()
    falsification: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    status: str = STATUS
    validation_required: bool = True
    language: str = "solidity"


def signal(detector: str, title: str, function, confidence: float, reason: str,
           evidence=(), ordered_trace=(), falsification=(), references=(),
           line: int | None = None) -> DetectorSignal:
    anchor = function.line if line is None else line
    return DetectorSignal(
        stable_id(detector, function.contract, function.name, str(anchor)),
        detector, title, function.contract, function.name,
        function.path or "", anchor,
        round(max(0.0, min(1.0, confidence)), 3), reason,
        tuple(evidence), tuple(ordered_trace), tuple(falsification),
        tuple(references), STATUS, True, function.language,
    )


def has_modifier(function, needles) -> str | None:
    for modifier in function.modifiers:
        lowered = modifier.lower()
        if any(needle in lowered for needle in needles):
            return modifier
    return None


def guards(function) -> list[IRStmt]:
    if function.ir is None:
        return []
    return [
        statement for statement in function.ir.walk()
        if statement.kind in {"require", "if"}
    ]


def has_sender_guard(function) -> str | None:
    """True when the function itself checks who is calling."""
    modifier = has_modifier(function, ("only", "auth", "role", "admin", "owner",
                                       "restricted", "governance", "permission"))
    if modifier:
        return f"modifier {modifier}"
    if function.ir is None:
        return None
    for statement in function.ir.walk():
        text = (statement.text or "").lower()
        if statement.kind in {"require", "if", "revert"} and any(
            token in text for token in SENDER_TOKENS
        ):
            return f"inline check at line {statement.line}"
        call = statement.call
        if call is not None and call.callee.lower() in AUTH_HELPERS:
            return f"auth helper {call.callee}() at line {call.line}"
    return None


def external_call_statements(function):
    if function.ir is None:
        return []
    return [
        statement for statement in function.ir.walk()
        if statement.call is not None and statement.call.kind in EXTERNAL_CALL_KINDS
    ]


def privileged_variables(contract) -> dict[str, str]:
    """State variables whose modification changes protocol authority or price."""
    out: dict[str, str] = {}
    for variable in contract.state_vars:
        if variable.constant or variable.immutable:
            continue
        lowered = variable.name.lower()
        type_text = f"{variable.type_name} {variable.value_type}".lower()
        reason = ""
        if any(hint in lowered for hint in PRIVILEGED_NAME_HINTS):
            reason = "privileged name"
        elif "address" in type_text and not variable.key_types:
            reason = "address-typed configuration"
        elif "bool" in type_text and not variable.key_types:
            reason = "boolean switch"
        if reason:
            out[variable.name] = reason
    return out


def entry_points(contract):
    return [
        function for function in contract.functions
        if function.is_entry_point and function.kind not in {"modifier"}
        and function.mutability not in {"view", "pure"}
    ]
