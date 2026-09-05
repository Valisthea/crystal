"""Contract-qualified state variable names.

A state variable is unique only inside its contract. `Registry.owner` and
`Vault.owner` are two different storage slots that happen to share a spelling,
and a cross-contract sequence touches both. Anything that merges state across
contracts — the symbolic engine's shared `SymbolicState`, the deltas derived
from it, the causal state graph — therefore keys on the qualified name
`Registry::owner` rather than the bare `owner`.

Two rules keep this coherent:

* **Identity is qualified.** Dictionary keys, set members and graph nodes carry
  the namespace, so two contracts never collide and never spuriously connect.
* **Meaning is bare.** Anything asking what a variable *is* rather than which
  one it is — category classification, accounting-pair matching — reads through
  `bare_name` first.

Inheritance is the exception that makes `StateNamespace` necessary: an
inherited variable is one slot, not two, so `Child.balances` and
`Parent.balances` must both resolve to `Parent::balances` or the graph loses
every edge that crosses the inheritance boundary.
"""

from __future__ import annotations

NAMESPACE_SEP = "::"


def qualify(contract: str, name: str) -> str:
    """`("Vault", "balances")` -> `"Vault::balances"`.

    A missing contract or an already-qualified name is returned untouched, so
    the call is idempotent and safe on mixed input.
    """
    if not contract or not name or is_qualified(name):
        return name
    return f"{contract}{NAMESPACE_SEP}{name}"


def is_qualified(name: str) -> bool:
    return NAMESPACE_SEP in (name or "")


def bare_name(name: str) -> str:
    """`"Vault::balances[msg.sender]"` -> `"balances[msg.sender]"`.

    Splits on the *first* separator, because qualification adds exactly one
    prefix and the remainder may legitimately contain `::` (Rust paths).
    Unqualified names pass through unchanged.
    """
    return (name or "").split(NAMESPACE_SEP, 1)[-1]


def contract_of(name: str) -> str:
    """`"Vault::balances"` -> `"Vault"`; `""` when the name carries no namespace."""
    text = name or ""
    return text.split(NAMESPACE_SEP, 1)[0] if NAMESPACE_SEP in text else ""


def same_contract(left: str, right: str) -> bool:
    """True when two names live in the same namespace, or in none at all.

    Accounting pairs are only meaningful within one contract: comparing
    `Vault::totalAssets` against `Registry::totalSupply` reports an asymmetry
    between two unrelated protocols.
    """
    return contract_of(left) == contract_of(right)


class StateNamespace:
    """Resolves a state access to the contract that owns the storage slot.

    A name declared in the accessing contract belongs to it. A name it does not
    declare is looked up through the inheritance chain, so an inherited slot
    keeps one identity across every contract that shares it. A name declared
    nowhere in the scanned set — a base contract that was not scanned — is
    attributed to the accessing contract, which keeps its intra-contract edges
    rather than silently merging it with an unrelated contract's variable of
    the same name.
    """

    def __init__(self, contracts=()):
        self._declared: dict[str, set[str]] = {}
        self._bases: dict[str, tuple[str, ...]] = {}
        for contract in contracts:
            name = contract.name
            self._declared.setdefault(name, set()).update(
                variable.name for variable in contract.state_vars
            )
            self._bases.setdefault(name, tuple(getattr(contract, "bases", ()) or ()))

    def owner(self, contract: str, name: str) -> str:
        """The contract whose storage `contract.name` actually refers to."""
        return self._search(contract, name, set()) or contract

    def _search(self, contract: str, name: str, seen: set[str]) -> str:
        if not contract or contract in seen:
            return ""
        seen.add(contract)
        # Bases first: a front-end that flattens inheritance into the child's
        # `state_vars` would otherwise split one inherited slot into two.
        for base in self._bases.get(contract, ()):
            found = self._search(base, name, seen)
            if found:
                return found
        return contract if name in self._declared.get(contract, ()) else ""

    def qualify(self, contract: str, name: str) -> str:
        """Namespace a state access, resolving inheritance to the owning slot."""
        if not name or is_qualified(name):
            return name
        return qualify(self.owner(contract, name), name)
