"""Associated-type resolution across crates.

A pallet declares `type OnChargeTransaction` and the runtime binds it somewhere
else entirely. Until that binding is read, `T::OnChargeTransaction::withdraw_fee`
points nowhere and the debit looks like it leaves the analysed code.

Resolution is reported with its own provenance: a type bound to something
outside the scan is `external`, not `unguarded`. Those are different claims and
conflating them would manufacture confidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

ASSOCIATED_REF_RE = re.compile(
    r"\bT\s*::\s*(\w+)|<\s*T\s+as\s+[\w:]+\s*>\s*::\s*(\w+)|\bSelf\s*::\s*(\w+)"
)

EXTERNAL_PREFIXES = ("frame_support::", "frame_system::", "sp_", "pallet_",
                     "polkadot_", "cumulus_", "xcm")


@dataclass(frozen=True)
class Resolution:
    reference: str
    associated_type: str
    concrete_type: str
    contract: str = ""
    external: bool = False
    reason: str = ""

    @property
    def resolved(self) -> bool:
        return bool(self.contract)

    def describe(self) -> str:
        if self.contract:
            return f"{self.associated_type} -> {self.contract}"
        if self.concrete_type:
            return (f"{self.associated_type} -> {self.concrete_type} "
                    f"(external implementation, guard status unknown)")
        return f"{self.associated_type} -> unresolved"


def associated_name(reference: str) -> str:
    """`<<T as Config>::OnChargeTransaction as OnChargeTransaction<T>>` -> the name."""
    match = ASSOCIATED_REF_RE.search(reference or "")
    if match:
        name = match.group(1) or match.group(2) or match.group(3) or ""
    else:
        name = re.sub(r"<[^>]*>", "", reference or "").strip().rsplit("::", 1)[-1]
    return re.split(r"\s+as\s+", name)[0].strip().strip("<>").strip()


def concrete_name(type_text: str) -> str:
    return re.sub(r"<.*", "", type_text or "").strip().rsplit("::", 1)[-1]


class ConfigResolver:
    """Maps `T::Foo` to the concrete type the runtime bound to it."""

    def __init__(self, bindings=(), contracts=(), topology=None):
        self.contracts = {contract.name: contract for contract in contracts}
        self.topology = topology
        self.bindings: dict[str, str] = {}
        self.sources: dict[str, object] = {}
        for binding in bindings or ():
            name = binding.associated_type
            if name not in self.bindings:
                self.bindings[name] = binding.concrete_type
                self.sources[name] = binding

    def _alias(self, name: str) -> str:
        """`Balances` is a runtime alias for `pallet_balances::Pallet<Runtime>`."""
        if self.topology is None:
            return name
        module = self.topology.by_alias.get(name)
        return module.crate if module else name

    def resolve(self, reference: str) -> Resolution:
        name = associated_name(reference)
        if not name:
            return Resolution(reference, "", "", reason="no associated type in reference")
        concrete = self.bindings.get(name)
        if concrete is None:
            return Resolution(reference, name, "",
                              reason="no runtime binding found for this associated type")

        short = concrete_name(concrete)
        # `type WeightInfo = ()` binds to the unit type. It is a real binding and
        # a useless route, and reporting it as one dilutes the real ones.
        if not short or short in {"()", "_"}:
            return Resolution(reference, name, concrete, "", False,
                              "bound to the unit type; no implementation to analyse")
        if short in self.contracts:
            return Resolution(reference, name, concrete, short, False, "bound in scope")

        aliased = self._alias(short)
        if aliased in self.contracts:
            return Resolution(reference, name, concrete, aliased, False,
                              "bound in scope through a runtime alias")

        external = concrete.startswith(EXTERNAL_PREFIXES) or short not in self.contracts
        return Resolution(
            reference, name, concrete, "", external,
            "external implementation, guard status unknown" if external
            else "concrete type not parsed",
        )

    def resolutions_for(self, contract) -> list[Resolution]:
        """Every associated-type call this contract makes."""
        out: list[Resolution] = []
        seen: set[str] = set()
        for function in contract.functions:
            if function.ir is None:
                continue
            for call in function.ir.calls():
                receiver = call.receiver or ""
                if not receiver:
                    continue
                # The binding table is the authority, not the shape of the
                # reference: parsers hand back receivers in several forms
                # (`T::Foo`, `<T as Config>::Foo`, or an already-trimmed
                # `Foo as Trait>`), and matching on syntax loses most of them.
                name = associated_name(receiver)
                if name not in self.bindings:
                    continue
                resolution = self.resolve(receiver)
                key = f"{resolution.associated_type}:{call.callee}"
                if key in seen:
                    continue
                seen.add(key)
                out.append(resolution)
        return out

    def as_map(self) -> dict[str, str]:
        return {
            name: concrete_name(concrete)
            for name, concrete in self.bindings.items()
        }
