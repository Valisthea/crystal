from dataclasses import dataclass

@dataclass(frozen=True)
class AccountingRelation:
    left: str
    right: str
    relation: str
    confidence: float
    evidence: list[str]

KEYS = {
    "assets": "assets",
    "totalassets": "assets",
    "shares": "shares",
    "totalsupply": "shares",
    "debt": "debt",
    "totaldebt": "debt",
    "reserve": "reserve",
    "reserves": "reserve",
    "fee": "fees",
    "fees": "fees",
}

def infer_accounting(contracts):
    out = []
    for c in contracts:
        grouped = {}
        for v in c.state_vars:
            k = KEYS.get(v.name.lower())
            if k:
                grouped.setdefault(k, []).append(v.name)

        if "assets" in grouped and "shares" in grouped:
            out.append(AccountingRelation(
                f"{c.name}.{grouped['assets'][0]}",
                f"{c.name}.{grouped['shares'][0]}",
                "asset/share conversion should remain coherent",
                .78,
                ["state-name-family:assets/shares"]
            ))

        if "debt" in grouped and "assets" in grouped:
            out.append(AccountingRelation(
                f"{c.name}.{grouped['debt'][0]}",
                f"{c.name}.{grouped['assets'][0]}",
                "debt/assets relationship should remain bounded",
                .66,
                ["state-name-family:debt/assets"]
            ))

        if "reserve" in grouped and "assets" in grouped:
            out.append(AccountingRelation(
                f"{c.name}.{grouped['reserve'][0]}",
                f"{c.name}.{grouped['assets'][0]}",
                "reserves/assets relationship should remain coherent",
                .64,
                ["state-name-family:reserve/assets"]
            ))
    return out
