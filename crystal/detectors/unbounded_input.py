"""Unbounded caller-supplied input reaching a value-bearing operation.

The mechanism this exists for: an amount that an untrusted caller chooses, with
no upper bound anywhere on the path, is added to a total that gets debited from
an account. The classic shape is a transaction tip — `fee = base + length +
weight + tip` where the first three are computed by the runtime and the fourth
is whatever the sender wrote.

What makes it reportable rather than noise is the combination Crystal can
actually see:

* the value is caller-chosen (an entry-point argument, or a field of a type the
  runtime decodes straight from the transaction);
* it reaches the amount position of an operation that moves value;
* no upper-bound guard was observed on the path;
* and, frequently, a *sibling* input on the same path IS explicitly capped,
  which is the codebase saying it knows the difference.

A plain `transfer(to, amount)` is deliberately NOT reported: a single-term
caller amount debited from the caller's own balance is ordinary, and flagging it
would bury the real signal.
"""

from __future__ import annotations

import re

from ..symbolic.algebra import ARG_PREFIX
from .base import DetectorSignal, signal

DETECTOR = "unbounded-input-in-value-op"

FALSIFICATION = (
    "Is an upper bound enforced by runtime configuration (a `Max*` associated "
    "type, a governance parameter) outside the parsed module?",
    "Does the caller's own balance already bound the amount in practice?",
    "Is the debited amount refunded on the failure path?",
    "Can the operation actually be reached by an unprivileged caller?",
    "Is a guard applied by a wrapper or a different extension in the pipeline?",
)

REFERENCES = ("CWE-20", "CWE-1284", "unbounded-value-input")

# Operations that move value. Matched on the callee name, not on prose.
VALUE_SINKS = {
    "withdraw", "withdraw_fee", "can_withdraw_fee", "withdraw_from",
    "transfer", "transfer_from", "transferfrom", "transfer_keep_alive",
    "transfer_all", "transfer_checked", "safetransfer", "safetransferfrom",
    "send", "sendvalue", "pay", "pay_fee", "charge", "charge_fee", "debit",
    "slash", "burn", "burn_from", "mint", "mint_into", "mint_to", "deposit",
    "deposit_into_existing", "deposit_creating", "settle", "repay", "redeem",
    "reserve", "unreserve", "hold", "release", "do_transfer", "do_withdraw",
}

# Tokens that constitute an observed upper bound on a value.
BOUND_PATTERNS = (
    re.compile(r"<=|<\s"),
    re.compile(r"\.min\s*\("),
    re.compile(r"\bclamp\s*\("),
    re.compile(r"\bmax_\w+|\bMax[A-Z]\w*|\bmaximum\b|\bceiling\b"),
    re.compile(r"\bcap\w*\s*\("),
)

# Saturating arithmetic protects the ADDITION from overflowing. It does not put
# a ceiling on the operand, and reading it as one is how this class gets missed.
NOT_A_BOUND = re.compile(r"saturating_|checked_|wrapping_|overflowing_")

AMOUNT_HINTS = ("amount", "fee", "value", "tip", "balance", "price", "cost",
                "sum", "total", "quantity", "qty", "wad", "shares", "assets")


def _tainted_symbols(expression) -> list[str]:
    return [symbol for symbol in expression.symbols if symbol.startswith(ARG_PREFIX)]


def _observed_bounds(function, symbols, constraints, label: str = "") -> list[str]:
    """Guards on the path that actually cap one of `symbols`.

    The guard names the source-level binding (`ensure!(tip <= MaxTip)`), while
    the symbol is the decoded field it resolves to (`ARG:self.0`), whose stem is
    the useless `0`. Matching on the argument's own text as well is what lets a
    real cap be recognised.
    """
    stems = {_stem(symbol) for symbol in symbols}
    stems |= {token.lower() for token in re.findall(r"[A-Za-z_]\w*", label or "")}
    stems.discard("")
    stems = {stem for stem in stems if len(stem) > 1 and not stem.isdigit()}
    found: list[str] = []
    texts = [constraint.render() for constraint in constraints]
    if function.ir is not None:
        texts.extend(
            statement.text for statement in function.ir.walk()
            if statement.kind in {"require", "if"}
        )
    for text in texts:
        lowered = (text or "").lower()
        if not any(stem in lowered for stem in stems if stem):
            continue
        if NOT_A_BOUND.search(lowered):
            continue
        if any(pattern.search(text or "") for pattern in BOUND_PATTERNS):
            found.append(text)
    return found


def _stem(symbol: str) -> str:
    name = symbol[len(ARG_PREFIX):] if symbol.startswith(ARG_PREFIX) else symbol
    name = name.split("#")[0]
    return name.rsplit(".", 1)[-1].lower()


def _sibling_caps(contract) -> list[str]:
    """Places in the same type where an input IS explicitly capped."""
    found: list[str] = []
    for function in contract.functions:
        if function.ir is None:
            continue
        for statement in function.ir.walk():
            text = statement.text or ""
            if NOT_A_BOUND.search(text):
                continue
            if re.search(r"\.min\s*\(|\bclamp\s*\(", text):
                found.append(f"{contract.name}.{function.name}:{statement.line} {text[:120]}")
    return found


def _argument_label(record, index: int) -> str:
    call = record.call
    if index < len(call.arguments):
        return call.arguments[index].text[:60]
    return f"argument {index}"


def _is_amount_position(expression, label: str, user_decoded: bool) -> bool:
    lowered = label.lower()
    if any(hint in lowered for hint in AMOUNT_HINTS):
        return True
    if user_decoded and any(
        symbol.startswith(f"{ARG_PREFIX}self.") for symbol in expression.symbols
    ):
        # A decoded field passed to a value operation is worth naming whatever
        # the parameter happens to be called.
        return True
    return any(hint in symbol.lower() for symbol in expression.symbols
               for hint in AMOUNT_HINTS)


def _tainted_argument_map(record) -> list[str]:
    """Every caller-controlled argument of the call, for the evidence trail."""
    out: list[str] = []
    for index, argument in enumerate(record.arguments):
        tainted = _tainted_symbols(argument)
        if not tainted:
            continue
        label = _argument_label(record, index)
        decoded = [s for s in tainted if s.startswith(f"{ARG_PREFIX}self.")]
        note = " (decoded from the transaction)" if decoded else ""
        out.append(f"arg{index} `{label}` = {argument.render()[:120]}{note}")
    return out


def detect(contracts, engine=None) -> list[DetectorSignal]:
    if engine is None:
        return []
    out: list[DetectorSignal] = []

    for contract in contracts:
        if getattr(contract, "is_test", False):
            continue
        user_decoded = getattr(contract, "user_decoded", False)
        caps = _sibling_caps(contract)

        for function in contract.functions:
            if function.is_test or function.ir is None:
                continue
            try:
                records = engine.call_records(function)
            except RecursionError:
                continue

            for record in records:
                callee = (record.call.callee or "").lower()
                if callee not in VALUE_SINKS:
                    continue
                for index, argument in enumerate(record.arguments):
                    tainted = _tainted_symbols(argument)
                    if not tainted:
                        continue
                    label = _argument_label(record, index)
                    if not _is_amount_position(argument, label, user_decoded):
                        continue
                    # A single caller-chosen term debited directly is ordinary.
                    # The reportable shape is a decoded field, or a caller value
                    # combined into a larger total.
                    composite = len(argument.terms) > 1
                    if not (user_decoded or composite):
                        continue

                    bounds = _observed_bounds(
                        function, tainted, record.constraints, label
                    )
                    if bounds:
                        continue

                    confidence = 0.56
                    evidence = [
                        f"`{record.call.callee}` at line {record.call.line} receives "
                        f"`{label}` = {argument.render()[:200]}",
                        "caller-controlled symbols reaching this argument: "
                        + ", ".join(sorted(tainted)),
                        "no upper-bound guard observed on this path"
                        + (f"; guards seen: {len(record.constraints)}"
                           if record.constraints else "; no guards at all on this path"),
                    ]
                    every_tainted = _tainted_argument_map(record)
                    if len(every_tainted) > 1:
                        evidence.append(
                            "every caller-controlled argument of this call: "
                            + " | ".join(every_tainted)
                        )
                    if user_decoded:
                        confidence += 0.16
                        evidence.append(
                            f"{contract.name} implements "
                            f"{', '.join(contract.traits) or 'a decoding trait'}, so the "
                            f"field is decoded from the transaction and chosen by "
                            f"whoever signed it"
                        )
                    if composite:
                        confidence += 0.08
                        evidence.append(
                            "the caller value is one term of a larger total, so it can "
                            "dominate the terms the protocol computes"
                        )
                    if caps:
                        confidence += 0.10
                        evidence.append(
                            "the same type caps other inputs explicitly, so the missing "
                            "cap here is a difference in treatment: " + caps[0]
                        )
                    if function.is_entry_point:
                        confidence += 0.04
                        evidence.append("the operation sits on an externally reachable path")

                    out.append(signal(
                        DETECTOR,
                        f"Unbounded caller input reaches {record.call.callee} in "
                        f"{contract.name}.{function.name}",
                        function, confidence,
                        "a value chosen by an untrusted caller reaches a "
                        "value-bearing operation with no observed upper bound, so "
                        "the amount moved is limited only by the caller's balance",
                        evidence=evidence,
                        ordered_trace=tuple(
                            [f"L{record.call.line} {record.call.callee}("
                             f"{', '.join(a.render()[:40] for a in record.arguments)})"]
                            + [f"guard: {c.render()[:120]}" for c in record.constraints[:6]]
                        ),
                        falsification=FALSIFICATION,
                        references=REFERENCES,
                        line=record.call.line,
                    ))

    merged: dict[str, DetectorSignal] = {}
    for item in out:
        previous = merged.get(item.id)
        if previous is None or item.confidence > previous.confidence:
            merged[item.id] = item
    return sorted(merged.values(),
                  key=lambda x: (-x.confidence, x.contract, x.function, x.line))
