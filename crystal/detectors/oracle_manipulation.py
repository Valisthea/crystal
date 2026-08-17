"""Price-source manipulation signal.

Raised when a price-like value is read from an external source and consumed by
protocol accounting on the same path, with no observable freshness or bounds
check. Spot sources (AMM reserves, live balances) are weighted higher than
feed sources because they are manipulable inside a single transaction.
"""

from __future__ import annotations

from ..ir import EXTERNAL_CALL_KINDS
from .base import DetectorSignal, signal

DETECTOR = "oracle-manipulation-surface"

FALSIFICATION = (
    "Is the price source a TWAP or an aggregated feed rather than a spot read?",
    "Is a freshness/deviation check applied outside the parsed function?",
    "Is the consumed value bounded so manipulation cannot change the outcome?",
    "Can an attacker actually move the source within one transaction?",
)

REFERENCES = ("SWC-114", "CWE-345", "flash-loan price manipulation")

SPOT_SOURCES = {
    "getreserves", "slot0", "getamountsout", "getamountsin", "balanceof",
    "totalsupply", "getvirtualprice", "price0cumulativelast",
    "price1cumulativelast", "getspotprice", "quote", "observe",
}
FEED_SOURCES = {
    "latestrounddata", "latestanswer", "getprice", "getlatestprice",
    "getassetprice", "peek", "read", "consult", "getrate", "exchangerate",
}
FRESHNESS_TOKENS = (
    "updatedat", "answeredinround", "roundid", "staleness", "heartbeat",
    "timestamp", "deviation", "twap", "maxage", "freshness",
)


def _price_calls(function):
    if function.ir is None:
        return []
    out = []
    for statement in function.ir.walk():
        call = statement.call
        if call is None or call.kind not in EXTERNAL_CALL_KINDS:
            continue
        name = (call.callee or "").lower()
        if name in SPOT_SOURCES:
            out.append((statement, call, "spot"))
        elif name in FEED_SOURCES:
            out.append((statement, call, "feed"))
    return out


def _has_freshness_check(function) -> bool:
    if function.ir is None:
        return False
    for statement in function.ir.walk():
        if statement.kind not in {"require", "if"}:
            continue
        text = (statement.text or "").lower()
        if any(token in text for token in FRESHNESS_TOKENS):
            return True
    return False


def detect(contracts, engine=None) -> list[DetectorSignal]:
    out: list[DetectorSignal] = []
    for contract in contracts:
        for function in contract.functions:
            calls = _price_calls(function)
            if not calls:
                continue
            writes = sorted(function.writes)
            returns_value = bool(function.returns)
            if not writes and not returns_value:
                continue

            fresh = _has_freshness_check(function)
            spot = [entry for entry in calls if entry[2] == "spot"]
            confidence = 0.52 + (0.14 if spot else 0.0) + (0.06 if writes else 0.0)
            if fresh:
                confidence -= 0.22

            evidence = [
                f"{kind} price source `{call.receiver or ''}.{call.callee}` "
                f"at line {call.line}"
                for _, call, kind in calls[:6]
            ]
            if writes:
                evidence.append("value influences state: " + ", ".join(writes[:12]))
            if returns_value and not writes:
                evidence.append("value is returned to callers and may drive accounting")
            evidence.append(
                "freshness/bounds check observed" if fresh
                else "no freshness or deviation check observed on this path"
            )

            out.append(signal(
                DETECTOR,
                f"Externally sourced price consumed in {contract.name}.{function.name}",
                function, confidence,
                "protocol accounting consumes a value read from an external "
                "contract that an attacker may be able to move within one "
                "transaction",
                evidence=evidence,
                ordered_trace=tuple(
                    f"L{call.line} {kind}-read: {statement.text}"
                    for statement, call, kind in calls[:6]
                ),
                falsification=FALSIFICATION,
                references=REFERENCES,
                line=calls[0][1].line,
            ))
    return sorted(out, key=lambda x: (-x.confidence, x.contract, x.function, x.line))
