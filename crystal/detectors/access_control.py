"""Missing-authority signal for privileged state writes.

Flags entry points that write authority-bearing or price-bearing state without
any observable check on the caller. Writes to state keyed by the caller
(`balances[msg.sender]`) are self-service and are not flagged.
"""

from __future__ import annotations

from ..ir import ASSIGN, DELETE
from .base import (
    DetectorSignal,
    entry_points,
    has_sender_guard,
    privileged_variables,
    signal,
)

DETECTOR = "missing-access-control"

FALSIFICATION = (
    "Is authority enforced by an inherited modifier the parser did not resolve?",
    "Is the contract itself only reachable through an access-controlled proxy?",
    "Is the written value already constrained so any caller is harmless?",
    "Is the function protected by an initializer or one-shot guard?",
)

REFERENCES = ("SWC-105", "SWC-106", "CWE-284")

SELF_SCOPED_TOKENS = ("msg.sender", "[sender]", "self.sender", "who", "caller")

CRITICAL_HINTS = ("owner", "admin", "implementation", "authority", "governance",
                  "minter", "role", "oracle", "treasury")


def _write_sites(function, privileged):
    sites = []
    if function.ir is None:
        return sites
    for statement in function.ir.walk():
        if statement.kind not in {ASSIGN, DELETE}:
            continue
        for name in statement.writes:
            if name not in privileged:
                continue
            target = (statement.target.text if statement.target else "").lower()
            if any(token in target for token in SELF_SCOPED_TOKENS):
                continue
            sites.append((name, statement))
    return sites


def detect(contracts, engine=None) -> list[DetectorSignal]:
    out: list[DetectorSignal] = []
    for contract in contracts:
        privileged = privileged_variables(contract)
        if not privileged:
            continue
        for function in entry_points(contract):
            if function.kind == "constructor":
                continue
            sites = _write_sites(function, privileged)
            if not sites:
                continue
            guard = has_sender_guard(function)
            if guard:
                continue

            names = sorted({name for name, _ in sites})
            critical = [
                name for name in names
                if any(hint in name.lower() for hint in CRITICAL_HINTS)
            ]
            confidence = 0.58 + 0.10 * bool(critical) + 0.04 * min(len(names), 3)
            if function.name.lower() in {"initialize", "init", "setup"}:
                confidence += 0.06

            out.append(signal(
                DETECTOR,
                f"Unguarded privileged write in {contract.name}.{function.name}",
                function, confidence,
                "a publicly reachable entry point writes authority-bearing state "
                "without any observable check on the caller",
                evidence=[
                    f"{name} ({privileged[name]}) written at line {statement.line}: "
                    f"{statement.text}"
                    for name, statement in sites[:8]
                ] + [f"declared modifiers: {function.modifiers or 'none'}"],
                ordered_trace=tuple(
                    f"L{statement.line} write[{name}]: {statement.text}"
                    for name, statement in sites[:8]
                ),
                falsification=FALSIFICATION,
                references=REFERENCES,
                line=sites[0][1].line,
            ))
    return sorted(out, key=lambda x: (-x.confidence, x.contract, x.function, x.line))
