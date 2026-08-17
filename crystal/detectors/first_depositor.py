"""First-depositor / share-inflation signal (ERC-4626 family).

The mechanism needs three ingredients, and Crystal only raises the signal when
it can point at all three in the parsed code:

1. a zero-supply branch that mints shares 1:1;
2. a conversion that divides by the supply or by total assets;
3. a path that increases assets without minting shares (donation).
"""

from __future__ import annotations

import re

from .base import DetectorSignal, signal

DETECTOR = "first-depositor-inflation"

FALSIFICATION = (
    "Does the constructor or deployment script seed a non-zero supply?",
    "Are virtual shares/assets offsets applied to the conversion?",
    "Is a minimum first deposit enforced anywhere on the path?",
    "Is the donation path actually reachable by an unprivileged caller?",
)

REFERENCES = ("ERC-4626 inflation attack", "CWE-682")

SUPPLY_NAMES = ("totalsupply", "totalshares", "supply", "shares")
ASSET_NAMES = ("totalassets", "totalbalance", "assets", "reserve", "pool")

ZERO_BRANCH_RE = re.compile(
    r"\b(total\w*supply|total\w*shares|supply|shares)\b\s*(==|<=)\s*0", re.IGNORECASE
)
DIV_BY_SUPPLY_RE = re.compile(r"DIV\([^/]*/[^)]*S0:(\w+)", re.IGNORECASE)


def _is_supply(name: str) -> bool:
    return any(token in name.lower() for token in SUPPLY_NAMES)


def _is_asset(name: str) -> bool:
    return any(token in name.lower() for token in ASSET_NAMES)


def _zero_supply_branch(function):
    if function.ir is None:
        return None
    for statement in function.ir.walk():
        if statement.kind not in {"if", "require"}:
            continue
        text = statement.condition.text if statement.condition else statement.text
        if ZERO_BRANCH_RE.search(text or ""):
            return statement
    return None


def _donation_paths(contract, engine):
    """Entry points that raise assets without raising supply."""
    found = []
    for function in contract.functions:
        if not function.is_entry_point or function.mutability in {"view", "pure"}:
            continue
        effect = engine.execute_function(function)
        asset_change = [
            name for name, value in effect.deltas.items()
            if _is_asset(name) and value != "0"
        ]
        supply_change = [
            name for name, value in effect.deltas.items()
            if _is_supply(name) and value != "0"
        ]
        if asset_change and not supply_change:
            found.append((function, asset_change))
    return found


def detect(contracts, engine=None) -> list[DetectorSignal]:
    if engine is None:
        return []
    out: list[DetectorSignal] = []
    for contract in contracts:
        names = {variable.name for variable in contract.state_vars}
        if not (any(_is_supply(n) for n in names) and any(_is_asset(n) for n in names)):
            continue
        donations = _donation_paths(contract, engine)

        for function in contract.functions:
            if not function.is_entry_point:
                continue
            branch = _zero_supply_branch(function)
            effect = engine.execute_function(function)
            divisions = sorted({
                match.group(1)
                for value in effect.deltas.values()
                for match in DIV_BY_SUPPLY_RE.finditer(value)
            })
            if branch is None and not divisions:
                continue

            confidence = 0.50
            evidence = []
            if branch is not None:
                confidence += 0.14
                evidence.append(
                    f"zero-supply branch at line {branch.line}: {branch.text}"
                )
            if divisions:
                confidence += 0.14
                evidence.append(
                    "conversion divides by protocol state: " + ", ".join(divisions)
                )
                evidence.extend(
                    f"delta({name}) = {value}"
                    for name, value in sorted(effect.deltas.items())
                    if "DIV(" in value
                )
            if donations:
                confidence += 0.12
                evidence.extend(
                    f"asset-only path {donor.contract}.{donor.name} changes "
                    f"{', '.join(changed)} without minting shares"
                    for donor, changed in donations[:4]
                )
            if branch is None or not divisions:
                confidence -= 0.12

            out.append(signal(
                DETECTOR,
                f"Share conversion is manipulable in {contract.name}.{function.name}",
                function, confidence,
                "share issuance depends on a supply/asset ratio that an early "
                "depositor can skew before other depositors enter",
                evidence=evidence,
                ordered_trace=(
                    (f"L{branch.line} branch: {branch.text}",)
                    if branch is not None else ()
                ),
                falsification=FALSIFICATION,
                references=REFERENCES,
                line=branch.line if branch is not None else function.line,
            ))
    return sorted(out, key=lambda x: (-x.confidence, x.contract, x.function, x.line))
