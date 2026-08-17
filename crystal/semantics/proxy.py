"""Upgradeability and proxy signals.

v1 matched a word list against function and variable names. v2 also looks at
what the code does: `delegatecall` sites, EIP-1967 slot constants, unguarded
initializers, and upgrade entry points whose authority check could not be
resolved.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import ir as I
from ..detectors.base import has_sender_guard

UPGRADE_WORDS = {
    "upgradeTo", "upgradeToAndCall", "implementation",
    "proxy", "beacon", "admin", "delegatecall",
}

EIP1967_SLOTS = {
    "0x360894a13ba1a3210667c828492db98dca3e2076cc3735a920a3ca505d382bbc":
        "eip1967.proxy.implementation",
    "0xb53127684a568b3173ae13b9f8a6016e243e63b6e8ee1178d6a717850b5d6103":
        "eip1967.proxy.admin",
    "0xa3f0ad74e5423aebfd80d3ef4346578335a9a72aeaee59ff6cb3582b35133d50":
        "eip1967.proxy.beacon",
}

INITIALIZER_NAMES = {"initialize", "init", "__init", "setup", "initializer"}


@dataclass(frozen=True)
class ProxySignal:
    contract: str
    signal: str
    evidence: list[str]
    confidence: float = 0.6
    kind: str = "naming"


@dataclass
class ProxyReport:
    signals: list[ProxySignal] = field(default_factory=list)
    delegatecall_sites: list[str] = field(default_factory=list)
    unguarded_initializers: list[str] = field(default_factory=list)


def detect_proxy_signals(contracts) -> list[ProxySignal]:
    return report(contracts).signals


def report(contracts) -> ProxyReport:
    out = ProxyReport()

    for contract in contracts:
        naming_hits: list[str] = []
        for function in contract.functions:
            if function.name in UPGRADE_WORDS:
                naming_hits.append(f"{contract.name}.{function.name}")
        for variable in contract.state_vars:
            if any(word.lower() in variable.name.lower() for word in UPGRADE_WORDS):
                naming_hits.append(f"{contract.name}.{variable.name}")
        if naming_hits:
            out.signals.append(ProxySignal(
                contract.name, "upgrade/proxy-related surface",
                sorted(set(naming_hits)), 0.60, "naming",
            ))

        delegate_sites: list[str] = []
        slot_hits: list[str] = []
        for function in contract.functions:
            if function.ir is None:
                continue
            for statement in function.ir.walk():
                call = statement.call
                if call is not None and call.kind == I.DELEGATECALL:
                    location = f"{contract.name}.{function.name}:{call.line}"
                    delegate_sites.append(location)
                    out.delegatecall_sites.append(location)
                for slot, label in EIP1967_SLOTS.items():
                    if slot in (statement.text or "").lower():
                        slot_hits.append(f"{label} at line {statement.line}")
        for variable in contract.state_vars:
            for slot, label in EIP1967_SLOTS.items():
                if slot in (variable.initial_value or "").lower():
                    slot_hits.append(f"{label} in {variable.name}")

        if delegate_sites:
            out.signals.append(ProxySignal(
                contract.name,
                "delegatecall forwards execution into another contract's code",
                sorted(set(delegate_sites)), 0.86, "delegatecall",
            ))
        if slot_hits:
            out.signals.append(ProxySignal(
                contract.name, "EIP-1967 storage slot constant present",
                sorted(set(slot_hits)), 0.90, "eip1967",
            ))

        unguarded = []
        for function in contract.functions:
            if not function.is_entry_point:
                continue
            if function.name.lower() not in INITIALIZER_NAMES:
                continue
            if has_sender_guard(function):
                continue
            if any(re.search(r"initializ", modifier, re.IGNORECASE)
                   for modifier in function.modifiers):
                continue
            location = f"{contract.name}.{function.name}:{function.line}"
            unguarded.append(location)
            out.unguarded_initializers.append(location)
        if unguarded:
            out.signals.append(ProxySignal(
                contract.name,
                "initializer reachable without an observable guard",
                sorted(unguarded), 0.78, "initializer",
            ))

    out.signals.sort(key=lambda x: (-x.confidence, x.contract, x.kind))
    return out
