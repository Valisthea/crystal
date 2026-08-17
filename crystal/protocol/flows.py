from dataclasses import dataclass

@dataclass(frozen=True)
class ValueFlow:
    source: str
    target: str
    asset: str
    operation: str
    confidence: float
    evidence: str

TRANSFER_NAMES = {"transfer", "transferFrom", "safeTransferFrom"}
MINT_NAMES = {"mint"}
BURN_NAMES = {"burn"}

def build_value_flows(contracts):
    flows = []
    for c in contracts:
        for f in c.functions:
            fn = f"{c.name}.{f.name}"
            if f.name in TRANSFER_NAMES:
                flows.append(ValueFlow(fn, "<recipient>", "<token>", "transfer", .90, f"function:{f.name}"))
            elif f.name in MINT_NAMES:
                flows.append(ValueFlow("<protocol>", fn, "<token>", "mint", .82, "function:mint"))
            elif f.name in BURN_NAMES:
                flows.append(ValueFlow(fn, "<protocol>", "<token>", "burn", .82, "function:burn"))
            elif f.name.lower() in {"deposit", "withdraw", "redeem"}:
                operation = f.name.lower()
                flows.append(ValueFlow(
                    "<user>" if operation == "deposit" else "<protocol>",
                    fn,
                    "<asset>",
                    operation,
                    .68,
                    f"protocol-primitive:{operation}"
                ))
    return flows
