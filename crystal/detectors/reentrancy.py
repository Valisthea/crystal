"""Ordering-based reentrancy signal.

The regex parser could not see the order of operations. With the statement IR
Crystal can say precisely: an external call at line N is followed by a write to
state read before the call at line M > N. That ordering is the mechanism, and
the trace is what a reviewer needs to falsify it.
"""

from __future__ import annotations

from ..ir import EXTERNAL_CALL_KINDS, flatten_events
from .base import DetectorSignal, REENTRANCY_GUARDS, has_modifier, signal

DETECTOR = "reentrancy-ordering"

FALSIFICATION = (
    "Is the external callee trusted and immutable (no attacker-controlled code)?",
    "Does a guard elsewhere on the path already prevent re-entry?",
    "Is the post-call write idempotent, so a nested call cannot benefit?",
    "Can the attacker actually reach the external call with a contract account?",
)

REFERENCES = (
    "SWC-107", "CWE-841", "checks-effects-interactions",
)

HIGH_RISK_CALLS = {"call", "delegatecall"}


def _trace(events, call_index, writes) -> tuple[str, ...]:
    window = [events[call_index]] + [events[i] for i in writes]
    return tuple(
        f"L{event.line} {event.kind}"
        + (f"[{event.variable}]" if event.variable else "")
        + f": {event.detail}"
        for event in window
    )


def analyze_function(function, cross_readers) -> list[DetectorSignal]:
    if function.ir is None or not function.ir.statements:
        return []
    events = flatten_events(function.ir)
    call_positions = [
        index for index, event in enumerate(events)
        if event.kind == "external_call"
    ]
    if not call_positions:
        return []

    guard = has_modifier(function, REENTRANCY_GUARDS)
    calls_by_line = {
        statement.call.line: statement.call
        for statement in function.ir.walk()
        if statement.call is not None
        and statement.call.kind in EXTERNAL_CALL_KINDS
    }

    out: list[DetectorSignal] = []
    for position in call_positions:
        call_event = events[position]
        reads_before = {
            event.variable for event in events[:position]
            if event.kind in {"state_read", "state_write"} and event.variable
        }
        writes_after = [
            index for index in range(position + 1, len(events))
            if events[index].kind == "state_write"
        ]
        if not writes_after:
            continue
        written = {events[index].variable for index in writes_after}
        checked_then_written = sorted(written & reads_before)

        call = calls_by_line.get(call_event.line)
        confidence = 0.62
        evidence = [
            f"external call `{call_event.detail}` at line {call_event.line}",
            "state written after the call: " + ", ".join(sorted(x for x in written if x)),
        ]
        if checked_then_written:
            confidence += 0.14
            evidence.append(
                "state read before the call and written after: "
                + ", ".join(checked_then_written)
            )
        if call is not None and call.value_attached:
            confidence += 0.08
            evidence.append("call forwards value to the callee")
        if call is not None and call.callee in HIGH_RISK_CALLS:
            confidence += 0.06
            evidence.append(f"low-level `{call.callee}` hands execution to the callee")

        reachable = sorted(
            f"{other}" for variable in written
            for other in cross_readers.get(variable, ())
            if other != f"{function.contract}.{function.name}"
        )
        if reachable:
            confidence += 0.04
            evidence.append(
                "same state is reachable from other entry points: "
                + ", ".join(dict.fromkeys(reachable))[:400]
            )

        reason = (
            "an external call transfers control before the state update, so a "
            "re-entrant call observes stale state"
        )
        if guard:
            confidence -= 0.35
            reason += f"; a reentrancy guard modifier `{guard}` is present"
            evidence.append(f"guard modifier present: {guard}")

        out.append(signal(
            DETECTOR,
            f"External call precedes state update in {function.contract}.{function.name}",
            function, confidence, reason,
            evidence=evidence,
            ordered_trace=_trace(events, position, writes_after),
            falsification=FALSIFICATION,
            references=REFERENCES,
            line=call_event.line,
        ))
    return out


def detect(contracts, engine=None) -> list[DetectorSignal]:
    cross_readers: dict[str, list[str]] = {}
    for contract in contracts:
        for function in contract.functions:
            if not function.is_entry_point:
                continue
            for variable in set(function.reads) | set(function.writes):
                cross_readers.setdefault(variable, []).append(
                    f"{contract.name}.{function.name}"
                )

    out: list[DetectorSignal] = []
    for contract in contracts:
        for function in contract.functions:
            out.extend(analyze_function(function, cross_readers))
    return sorted(out, key=lambda x: (-x.confidence, x.contract, x.function, x.line))
