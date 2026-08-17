from .models import Hypothesis

def generate(observations):
    result = []

    for o in observations:
        ref = f"{o.path}:{o.line}"

        if o.kind == "CROSS_FUNCTION_STATE":
            funcs = sorted(set(o.evidence["readers"] + o.evidence["writers"]))
            result.append(Hypothesis(
                "STATE_DEPENDENCY",
                f"Check ordering/invariant consistency for {o.contract}.{o.evidence['variable']}",
                "A shared state variable is read and written across multiple entry points. "
                "The interesting behavior may require a sequence rather than one call.",
                [f"{o.contract}.{x}" for x in funcs],
                [ref], 0.86
            ))

        elif o.kind == "ACCOUNTING_SURFACE":
            result.append(Hypothesis(
                "ACCOUNTING",
                f"Derive and test accounting invariants around {o.contract}.{o.function}",
                "The entry point touches accounting-sensitive state. Test conservation, "
                "monotonicity, share/asset consistency and fee boundaries.",
                [f"{o.contract}.{o.function}"],
                [ref], 0.80
            ))

        elif o.kind == "ARITHMETIC_SURFACE":
            result.append(Hypothesis(
                "ROUNDING_PRECISION",
                f"Test rounding direction and boundaries in {o.contract}.{o.function}",
                "Division/modulo creates possible asymmetric conversion and boundary behavior. "
                "Exercise zero, one, near-denominator and repeated/inverse paths.",
                [f"{o.contract}.{o.function}"],
                [ref], 0.74
            ))

        elif o.kind == "EXTERNAL_CALL_SURFACE":
            result.append(Hypothesis(
                "CALLBACK_ORDERING",
                f"Validate state-before-call ordering in {o.contract}.{o.function}",
                "An external call is an attack-surface signal. Compare state changes around "
                "the call and investigate callback-reachable paths.",
                [f"{o.contract}.{o.function}"],
                [ref], 0.70
            ))

        elif o.kind == "PROTOCOL_PRIMITIVE":
            result.append(Hypothesis(
                "ECONOMIC_SEQUENCE",
                f"Generate adversarial sequences around {o.contract}.{o.function}",
                f"The function is classified as '{o.evidence['primitive']}'. Explore "
                "interactions with other protocol primitives and accounting state.",
                [f"{o.contract}.{o.function}"],
                [ref], 0.68
            ))

    return result
