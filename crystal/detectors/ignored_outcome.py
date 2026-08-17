"""A settlement path that charges regardless of the outcome it was handed.

Signal 3 of the F5 benchmark. The mechanism is visible in the signature alone:

    fn post_dispatch_details(.., _result: &DispatchResult) -> .. {
        let actual_fee_with_tip = compute_actual_fee(len, info, &post_info, tip);
        T::OnChargeTransaction::correct_and_deposit_fee(.., actual_fee_with_tip, tip, ..)
    }

`_result` carries whether the dispatch succeeded and is bound to an
underscore-prefixed name, which is Rust for "deliberately ignored". The tip is
then re-added to the corrected fee and charged on every path, including the one
where the user got nothing.

Being handed an outcome and discarding it is the reportable part. A function
that never receives the outcome is not making that mistake, and is not flagged.
"""

from __future__ import annotations

import re

from ..symbolic.algebra import ARG_PREFIX
from .base import DetectorSignal, signal

DETECTOR = "ignored-outcome-in-settlement"

FALSIFICATION = (
    "Is the outcome consumed by the callee, so this frame legitimately ignores it?",
    "Is charging on failure the intended economics (paying for the attempt)?",
    "Does a later stage refund the amount that was charged here?",
    "Does the failure path revert the whole transaction, making the charge moot?",
    "Is the ignored parameter genuinely the outcome, or an unrelated argument?",
)

REFERENCES = ("CWE-252", "CWE-390", "unchecked-outcome")

# Types and names that carry whether the operation succeeded.
OUTCOME_TYPE_HINTS = ("dispatchresult", "result<", "dispatcherror", "postdispatchinfo",
                      "postdispatchinfoof", "outcome", "dispatchresultwithinfo")
OUTCOME_NAME_HINTS = ("result", "outcome", "success", "succeeded", "ok", "status",
                      "post_info", "postinfo", "error", "err", "failed", "failure")

# Settlement operations: they finalize a charge rather than starting one.
SETTLEMENT_CALLS = (
    "correct_and_deposit_fee", "correct_and_deposit", "settle", "finalize_fee",
    "refund", "deposit_fee", "charge", "withdraw", "transfer", "pay",
    "correct_fee", "adjust_fee", "post_dispatch", "deposit_event",
)
SETTLEMENT_FUNCTIONS = ("post_dispatch", "post_dispatch_details", "settle",
                        "finalize", "correct_and_deposit_fee", "on_charge",
                        "refund", "complete", "conclude")

REFUND_TOKENS = ("refund", "return_", "give_back", "restore", "credit_back",
                 "reimburse", "unreserve")


def _outcome_parameters(function):
    """Parameters that carry the outcome of the operation being settled."""
    found = []
    for parameter in function.params:
        name = (parameter.name or "").lower()
        type_name = (parameter.type_name or "").lower().replace(" ", "")
        stem = name.lstrip("_")
        if not stem:
            continue
        if any(hint in type_name for hint in OUTCOME_TYPE_HINTS) or \
                stem in OUTCOME_NAME_HINTS:
            found.append(parameter)
    return found


def _is_ignored(function, parameter) -> bool:
    """Underscore-prefixed, or never mentioned in the body."""
    name = parameter.name or ""
    if name.startswith("_"):
        return True
    body = function.body or ""
    return not re.search(rf"\b{re.escape(name)}\b", body)


def _settlement_calls(function, engine):
    try:
        records = engine.call_records(function)
    except RecursionError:
        return []
    return [
        record for record in records
        if any(hint in (record.call.callee or "").lower()
               for hint in SETTLEMENT_CALLS)
    ]


def _branches_on(function, parameters) -> bool:
    """Does any guard actually test one of these parameters?"""
    if function.ir is None:
        return False
    stems = {(p.name or "").lstrip("_").lower() for p in parameters}
    stems.discard("")
    for statement in function.ir.walk():
        if statement.kind not in {"if", "require"}:
            continue
        text = (statement.text or "").lower()
        if any(stem in text for stem in stems):
            return True
    return False


def _has_refund_path(function) -> bool:
    if function.ir is None:
        return False
    for statement in function.ir.walk():
        text = (statement.text or "").lower()
        call = statement.call
        callee = (call.callee or "").lower() if call is not None else ""
        if any(token in callee for token in REFUND_TOKENS):
            return True
        if any(token in text for token in REFUND_TOKENS):
            return True
    return False


def detect(contracts, engine=None) -> list[DetectorSignal]:
    if engine is None:
        return []
    out: list[DetectorSignal] = []

    for contract in contracts:
        if getattr(contract, "is_test", False):
            continue
        for function in contract.functions:
            if function.is_test or function.ir is None:
                continue
            outcome_parameters = _outcome_parameters(function)
            if not outcome_parameters:
                continue
            ignored = [p for p in outcome_parameters if _is_ignored(function, p)]
            if not ignored:
                continue
            if _branches_on(function, outcome_parameters):
                continue

            settlements = _settlement_calls(function, engine)
            settles_by_name = any(
                hint in function.name.lower() for hint in SETTLEMENT_FUNCTIONS
            )
            if not settlements and not settles_by_name:
                continue

            tainted = sorted({
                symbol
                for record in settlements
                for argument in record.arguments
                for symbol in argument.symbols
                if symbol.startswith(ARG_PREFIX)
            })

            confidence = 0.54
            evidence = [
                "outcome parameter(s) received and never read: "
                + ", ".join(
                    f"`{p.name}: {p.type_name}`" for p in ignored
                ),
                "no guard in this function tests the outcome",
            ]
            if settlements:
                confidence += 0.12
                evidence.append(
                    "value is settled on this path regardless: "
                    + "; ".join(
                        f"{r.call.callee} at line {r.call.line}"
                        for r in settlements[:4]
                    )
                )
            if tainted:
                confidence += 0.10
                evidence.append(
                    "caller-controlled values still flowing into the settlement: "
                    + ", ".join(tainted[:8])
                )
            if settles_by_name:
                confidence += 0.06
                evidence.append(
                    f"`{function.name}` is a settlement entry point, so this is the "
                    f"frame that decides what is finally charged"
                )
            if not _has_refund_path(function):
                confidence += 0.08
                evidence.append(
                    "no refund or reimbursement path observed in this function"
                )
            else:
                confidence -= 0.10
                evidence.append("a refund path exists in this function")

            out.append(signal(
                DETECTOR,
                f"{contract.name}.{function.name} settles value without reading the "
                f"outcome it was given",
                function, confidence,
                "the function receives the result of the operation it is settling "
                "and discards it, while still moving value, so the failure path is "
                "charged exactly like the success path",
                evidence=evidence,
                ordered_trace=tuple(
                    [f"signature: {function.name}("
                     + ", ".join(f"{p.name}: {p.type_name}" for p in function.params)
                     + ")"]
                    + [f"L{r.call.line} settle: {r.call.callee}("
                       + ", ".join(a.render()[:40] for a in r.arguments) + ")"
                       for r in settlements[:4]]
                ),
                falsification=FALSIFICATION,
                references=REFERENCES,
                line=function.line,
            ))

    merged: dict[str, DetectorSignal] = {}
    for item in out:
        previous = merged.get(item.id)
        if previous is None or item.confidence > previous.confidence:
            merged[item.id] = item
    return sorted(merged.values(),
                  key=lambda x: (-x.confidence, x.contract, x.function, x.line))
