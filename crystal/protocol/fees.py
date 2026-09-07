"""State variables that scale a caller-supplied amount on a value path.

The previous version fired on `"fee" in name`. It found fees that were spelled
that way, missed every fee that was not, and charged a signal for any getter
whose name contained the substring.

What is observable is the shape rather than the word: a state variable
multiplying or dividing something that is not state, in a function whose result
reaches somebody. A fee has that shape. So does a margin, a rate, a discount and a
slippage bound — and the same thing goes wrong in all of them, which is why
the shape is the better unit. The dataclass keeps its name because the report
schema is public; what it now holds is stated in `ScaledAmount`.
"""

from dataclasses import dataclass

from .grounding import analysable_contracts, scaled_amounts

@dataclass(frozen=True)
class FeeSignal:
    contract: str
    function: str
    confidence: float
    evidence: list[str]
    scalar: str = ""
    amount: str = ""
    line: int = 0

def detect_fee_signals(contracts):
    result = []
    for c in analysable_contracts(contracts):
        for scaled in scaled_amounts(c):
            # Arithmetic whose result reaches nothing changes nobody's
            # position. A quote that is returned does count: the caller acts
            # on it, which is how Lido's margin governs an order's validity.
            if not scaled.reaches_effect:
                continue
            result.append(FeeSignal(
                c.name, scaled.function, .66,
                [
                    f"state `{scaled.scalar}` scales `{scaled.amount}` "
                    f"at line {scaled.line}",
                    f"expression: {scaled.text}",
                    "the scaled result reaches state, a transfer or the return value",
                ],
                scaled.scalar, scaled.amount, scaled.line,
            ))
    return result
