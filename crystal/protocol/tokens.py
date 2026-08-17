from dataclasses import dataclass, field

@dataclass
class TokenFunction:
    contract: str
    function: str
    kind: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    heuristic: bool = True

ERC20_PATTERNS = {
    "transfer": "transfer",
    "transferFrom": "transferFrom",
    "approve": "approval",
    "increaseAllowance": "approval",
    "decreaseAllowance": "approval",
    "mint": "mint",
    "burn": "burn",
}

def classify_token_functions(contracts):
    result = []
    for c in contracts:
        for f in c.functions:
            if f.name in ERC20_PATTERNS:
                kind = ERC20_PATTERNS[f.name]
                result.append(TokenFunction(
                    c.name, f.name, kind,
                    0.72,
                    [f"heuristic:function-name:{f.name}"],
                    True,
                ))
    return result
