from collections import defaultdict
from .models import Observation

ACCOUNTING = {
    "balance", "balances", "shares", "assets", "asset", "liability",
    "liabilities", "debt", "reserve", "reserves", "reward", "rewards",
    "fee", "fees", "totalsupply", "price", "exchangerate", "index"
}
PRIMITIVES = {
    "deposit", "withdraw", "redeem", "mint", "burn", "borrow", "repay",
    "liquidate", "swap", "stake", "unstake", "claim", "harvest",
    "rebalance", "donate", "flashloan"
}

def analyze(contracts):
    observations = []

    for c in contracts:
        readers = defaultdict(list)
        writers = defaultdict(list)

        for f in c.functions:
            for x in f.reads:
                readers[x].append(f.name)
            for x in f.writes:
                writers[x].append(f.name)

        for var in set(readers) & set(writers):
            if len(set(readers[var]) | set(writers[var])) >= 2:
                observations.append(Observation(
                    "CROSS_FUNCTION_STATE",
                    f"State variable '{var}' is shared across multiple function paths",
                    c.name, None, c.path, c.line,
                    {
                        "variable": var,
                        "readers": sorted(set(readers[var])),
                        "writers": sorted(set(writers[var]))
                    }
                ))

        for f in c.functions:
            concepts = sorted({
                token for token in ACCOUNTING
                if token in f.name.lower()
                or any(token in x.lower() for x in f.reads | f.writes)
            })

            primitive = f.name.lower() if f.name.lower() in PRIMITIVES else None

            if concepts and f.visibility in {"public", "external"}:
                observations.append(Observation(
                    "ACCOUNTING_SURFACE",
                    f"Public entry point touches accounting-sensitive state: {f.name}",
                    c.name, f.name, c.path, f.line,
                    {"concepts": concepts, "primitive": primitive}
                ))

            if "/" in f.body or "%" in f.body:
                observations.append(Observation(
                    "ARITHMETIC_SURFACE",
                    f"Division/modulo occurs in {f.name}",
                    c.name, f.name, c.path, f.line,
                    {"operators": [x for x in ["/", "%"] if x in f.body]}
                ))

            if "<low-level-call>" in f.calls:
                observations.append(Observation(
                    "EXTERNAL_CALL_SURFACE",
                    f"Low-level/value-transfer operation occurs in {f.name}",
                    c.name, f.name, c.path, f.line,
                    {}
                ))

            if primitive:
                observations.append(Observation(
                    "PROTOCOL_PRIMITIVE",
                    f"Protocol primitive detected: {primitive}",
                    c.name, f.name, c.path, f.line,
                    {"primitive": primitive}
                ))

    return observations
