"""Token entry points, matched against declared signatures rather than names.

`transfer(address,uint256)` is a promise published in an ABI, and matching it
is reading a standard. A function called `transfer` that takes one argument is
a different function that happens to share a spelling, and matching that is
guessing. The previous version matched the name alone, and labelled the result
`heuristic:function-name` — accurate about its own weakness, and still counted
at 0.72.

`mint` and `burn` are conventions, not standards: no ERC defines them. They are
kept because the supply invariant that depends on them is worth having, but
they are labelled as conventions, scored below the standards, and required to
have a body that writes state — which is what keeps interface declarations out.
"""

from dataclasses import dataclass, field

from .grounding import analysable_contracts, has_body, matches_signature

# Published signatures. ERC-20, the ERC-721 overloads that differ, and the
# ERC-4626 vault methods.
STANDARD_SIGNATURES = {
    "transfer(address,uint256)": ("transfer", "ERC-20"),
    "transferFrom(address,address,uint256)": ("transferFrom", "ERC-20"),
    "approve(address,uint256)": ("approval", "ERC-20"),
    "increaseAllowance(address,uint256)": ("approval", "ERC-20"),
    "decreaseAllowance(address,uint256)": ("approval", "ERC-20"),
    "safeTransferFrom(address,address,uint256)": ("transferFrom", "ERC-721"),
    "safeTransferFrom(address,address,uint256,bytes)": ("transferFrom", "ERC-721"),
    "setApprovalForAll(address,bool)": ("approval", "ERC-721"),
    "deposit(uint256,address)": ("deposit", "ERC-4626"),
    "mint(uint256,address)": ("mint", "ERC-4626"),
    "withdraw(uint256,address,address)": ("withdraw", "ERC-4626"),
    "redeem(uint256,address,address)": ("redeem", "ERC-4626"),
}

# Widespread, but nobody standardised them.
CONVENTIONAL_SIGNATURES = {
    "mint(address,uint256)": ("mint", "convention"),
    "mint(uint256)": ("mint", "convention"),
    "burn(uint256)": ("burn", "convention"),
    "burn(address,uint256)": ("burn", "convention"),
    "burnFrom(address,uint256)": ("burn", "convention"),
}

# `uint` and `int` are aliases the compiler resolves; a signature written
# either way is the same ABI entry.
ALIASES = {"uint": "uint256", "int": "int256", "uint[]": "uint256[]"}

@dataclass
class TokenFunction:
    contract: str
    function: str
    kind: str
    confidence: float
    evidence: list[str] = field(default_factory=list)
    heuristic: bool = False
    standard: str = ""

def _normalised(function) -> str:
    parts = [
        ALIASES.get(p.type_name.strip(), p.type_name.strip())
        for p in function.params
    ]
    return f"{function.name}({','.join(parts)})"

class _Normalised:
    """Adapter so `matches_signature` sees alias-resolved parameter types."""

    __slots__ = ("signature",)

    def __init__(self, function):
        self.signature = _normalised(function)

def classify_token_functions(contracts):
    result = []
    for c in analysable_contracts(contracts):
        for f in c.functions:
            view = _Normalised(f)
            hit = matches_signature(view, STANDARD_SIGNATURES)
            if hit:
                kind, standard = hit
                result.append(TokenFunction(
                    c.name, f.name, kind, .88,
                    [f"abi-signature:{view.signature}", f"standard:{standard}"],
                    False, standard,
                ))
                continue
            hit = matches_signature(view, CONVENTIONAL_SIGNATURES)
            # A convention with no body is a declaration of intent by whoever
            # wrote the interface, not a behaviour Crystal has seen.
            if hit and has_body(f) and f.writes:
                kind, standard = hit
                result.append(TokenFunction(
                    c.name, f.name, kind, .60,
                    [
                        f"convention:{view.signature}",
                        "writes state: " + ", ".join(sorted(f.writes)[:6]),
                    ],
                    True, standard,
                ))
    return result
