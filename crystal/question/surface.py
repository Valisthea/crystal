"""What a question's surface names, in the code Crystal actually parsed.

A surface is a claim about the target — `Order.isValidSignature` exists, the
state variable `Stonks::MARGIN_DIFFERENCE_IN_BASIS_POINTS` exists and something
touches it. This module checks that claim against the parse and turns each item
into the entry points it stands for, which is the only form the rest of the
pipeline can act on.

Three outcomes per item, kept apart because they mean different things:

* **resolved** — the item exists, and these functions are what it covers;
* **unresolved** — it does not exist in this world. A typo, a renamed function,
  a question written against another revision. Running the analysis anyway would
  answer a question about the rest of the code and report it under this one;
* **unsupported** — a kind Crystal cannot yet map onto functions (`asset_flow`,
  `state_transition`, `contract_family`). Said, not approximated.

Resolution runs on the same contract set research does: test fixtures and
scaffolding are excluded here for the same reason they are excluded there. A
surface that only resolves inside a mock is not a surface of the target.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..discovery import excluded_dir_reason
from ..naming import NAMESPACE_SEP, bare_name

# Kinds that map onto entry points today. The rest are declared by the schema
# and reported as unsupported rather than guessed at.
RESOLVABLE = frozenset({"function", "contract", "state_variable", "call_path"})

# `A.f -> B.g`, or `A.f>B.g`. Both spellings are accepted; order is kept.
CALL_PATH_SEPARATORS = ("->", ">")


@dataclass(frozen=True)
class ResolvedItem:
    kind: str
    identifier: str
    functions: tuple[str, ...]

    def as_dict(self) -> dict:
        return {"kind": self.kind, "identifier": self.identifier,
                "functions": list(self.functions)}


@dataclass
class SurfaceResolution:
    resolved: list[ResolvedItem] = field(default_factory=list)
    unresolved: list[dict] = field(default_factory=list)
    unsupported: list[dict] = field(default_factory=list)

    @property
    def functions(self) -> frozenset[str]:
        return frozenset(name for item in self.resolved for name in item.functions)

    @property
    def any_resolved(self) -> bool:
        return bool(self.resolved)

    def groups(self) -> list[tuple[str, frozenset[str]]]:
        """One group per resolved item, keyed by what the caller wrote."""
        return [(f"{item.kind}:{item.identifier}", frozenset(item.functions))
                for item in self.resolved]

    def as_dict(self) -> dict:
        return {
            "resolved": [item.as_dict() for item in self.resolved],
            "unresolved": list(self.unresolved),
            "unsupported": list(self.unsupported),
            "functions": sorted(self.functions),
        }


def research_contracts(contracts):
    """The contract set research runs on — fixtures and scaffolding removed."""
    return [
        contract for contract in contracts
        if not getattr(contract, "is_test", False)
        and not excluded_dir_reason(getattr(contract, "path", "") or "")
    ]


def _index(contracts):
    functions: dict[str, set[str]] = {}
    by_contract: dict[str, set[str]] = {}
    touching: dict[tuple[str, str], set[str]] = {}
    for contract in contracts:
        for function in contract.functions:
            qualified = f"{contract.name}.{function.name}"
            functions.setdefault(qualified, set()).add(qualified)
            by_contract.setdefault(contract.name, set()).add(qualified)
            for variable in set(function.reads) | set(function.writes):
                touching.setdefault((contract.name, bare_name(variable)), set()).add(qualified)
    return functions, by_contract, touching


def _state_variable(identifier, touching):
    if NAMESPACE_SEP in identifier:
        owner, name = identifier.split(NAMESPACE_SEP, 1)
        return set(touching.get((owner, name), ()))
    found: set[str] = set()
    for (_, name), members in touching.items():
        if name == identifier:
            found |= members
    return found


def _call_path(identifier, functions):
    for separator in CALL_PATH_SEPARATORS:
        if separator in identifier:
            steps = [part.strip() for part in identifier.split(separator)]
            break
    else:
        steps = [identifier.strip()]
    if not all(step in functions for step in steps):
        return set()
    return set(steps)


def resolve(surfaces, contracts) -> SurfaceResolution:
    """Map each surface item onto the entry points it covers, or say why not."""
    contracts = research_contracts(contracts)
    functions, by_contract, touching = _index(contracts)
    resolution = SurfaceResolution()

    for surface in surfaces or ():
        record = {"kind": surface.kind, "identifier": surface.identifier}
        if surface.kind not in RESOLVABLE:
            resolution.unsupported.append({
                **record,
                "reason": f"{surface.kind} surfaces are declared by the schema "
                          f"and not yet mapped onto entry points",
            })
            continue

        if surface.kind == "function":
            found = set(functions.get(surface.identifier, ()))
        elif surface.kind == "contract":
            found = set(by_contract.get(surface.identifier, ()))
        elif surface.kind == "state_variable":
            found = _state_variable(surface.identifier, touching)
        else:
            found = _call_path(surface.identifier, functions)

        if found:
            resolution.resolved.append(ResolvedItem(
                surface.kind, surface.identifier, tuple(sorted(found)),
            ))
        else:
            resolution.unresolved.append({
                **record,
                "reason": "nothing in the analysed contracts matches it",
            })
    return resolution
