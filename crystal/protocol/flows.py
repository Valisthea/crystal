"""Where value moves, from the calls that move it.

The previous version listed a flow when a function was *named* `transfer`,
`mint`, `deposit` or `withdraw`. That records what a contract offers and misses
what it does: a swap that calls `IERC20(token).transfer(...)` halfway through
its body moved value and produced no flow, because the function was called
`swap`.

Two sources now. The entry points the contract declares, matched against
published signatures by `classify_token_functions`; and every outbound value
transfer observed in a body, which is the movement itself.
"""

from dataclasses import dataclass

from ..ir import VALUE_TRANSFER
from .grounding import analysable_contracts, has_body
from .tokens import classify_token_functions

@dataclass(frozen=True)
class ValueFlow:
    source: str
    target: str
    asset: str
    operation: str
    confidence: float
    evidence: str

# What the declared entry point means for direction.
_DECLARED = {
    "transfer": ("{fn}", "<recipient>"),
    "transferFrom": ("<holder>", "<recipient>"),
    "mint": ("<protocol>", "{fn}"),
    "burn": ("{fn}", "<protocol>"),
    "deposit": ("<user>", "{fn}"),
    "withdraw": ("{fn}", "<user>"),
    "redeem": ("{fn}", "<user>"),
}

def build_value_flows(contracts):
    flows = []
    analysable = analysable_contracts(contracts)

    for token_function in classify_token_functions(analysable):
        direction = _DECLARED.get(token_function.kind)
        if direction is None:
            continue
        qualified = f"{token_function.contract}.{token_function.function}"
        source, target = (part.format(fn=qualified) for part in direction)
        flows.append(ValueFlow(
            source, target, "<token>", token_function.kind,
            token_function.confidence,
            token_function.evidence[0] if token_function.evidence else "declared",
        ))

    for c in analysable:
        for f in c.functions:
            if not has_body(f):
                continue
            for call in f.ir.calls():
                # A value transfer the body performs, wherever it sits and
                # whatever the enclosing function is called.
                if call.kind != VALUE_TRANSFER:
                    continue
                flows.append(ValueFlow(
                    f"{c.name}.{f.name}",
                    f"{call.receiver or '<external>'}.{call.callee}",
                    "<token>", "outbound-transfer", .80,
                    f"line {call.line}: {(call.text or '').strip()[:80]}",
                ))
    return flows
