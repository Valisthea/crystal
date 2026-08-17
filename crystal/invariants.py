from dataclasses import dataclass

@dataclass
class InvariantCandidate:
    category: str
    expression: str
    confidence: float
    evidence: list[str]

def discover_invariants(contracts):
    candidates = []

    for c in contracts:
        names = {v.name for v in c.state_vars}
        lower = {n.lower(): n for n in names}

        # Paired accounting variables.
        pairs = [
            ("totalassets", "totalsupply", "asset/share consistency"),
            ("assets", "shares", "asset/share consistency"),
            ("totaldebt", "totalassets", "debt/asset accounting"),
            ("reserve", "totalsupply", "reserve/supply accounting"),
        ]
        for left, right, category in pairs:
            if left in lower and right in lower:
                candidates.append(InvariantCandidate(
                    category,
                    f"{lower[left]} and {lower[right]} should remain internally consistent",
                    0.76,
                    [f"{c.name}.{lower[left]}", f"{c.name}.{lower[right]}"],
                ))

        # Monotonic counters / totals.
        for var in names:
            lv = var.lower()
            if any(token in lv for token in ("nonce", "counter", "index")):
                candidates.append(InvariantCandidate(
                    "monotonicity",
                    f"{var} should not decrease during ordinary protocol transitions",
                    0.68,
                    [f"{c.name}.{var}"],
                ))

        # User balances and total accounting surfaces.
        has_balance = any("balance" in n.lower() for n in names)
        has_total = any(n.lower().startswith("total") for n in names)
        if has_balance and has_total:
            candidates.append(InvariantCandidate(
                "conservation",
                "aggregate user accounting should remain bounded by protocol totals/reserves",
                0.64,
                [c.name],
            ))

    return candidates
