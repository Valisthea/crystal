from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .ir import IRFunctionBody

SOLIDITY = "solidity"
RUST = "rust"
MOVE = "move"
VYPER = "vyper"


@dataclass
class Parameter:
    name: str
    type_name: str
    indexed: bool = False
    location: str = ""

    def __str__(self) -> str:
        return f"{self.type_name} {self.name}".strip()


@dataclass
class StateVar:
    contract: str
    name: str
    type_name: str
    visibility: str
    line: int
    # v2 additions — every field defaults so the v1 regex parser stays valid.
    constant: bool = False
    immutable: bool = False
    key_types: list[str] = field(default_factory=list)
    value_type: str = ""
    language: str = SOLIDITY
    initial_value: str = ""

    @property
    def is_mapping(self) -> bool:
        return bool(self.key_types) or self.type_name.strip().startswith("mapping")


@dataclass
class Function:
    contract: str
    name: str
    visibility: str
    mutability: str
    modifiers: list[str] = field(default_factory=list)
    reads: set[str] = field(default_factory=set)
    writes: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)
    line: int = 0
    body: str = ""
    # v2 additions.
    params: list[Parameter] = field(default_factory=list)
    returns: list[Parameter] = field(default_factory=list)
    kind: str = "function"
    language: str = SOLIDITY
    end_line: int = 0
    ir: IRFunctionBody | None = None
    parser: str = "regex"
    path: str = ""
    payable: bool = False
    # Test fixtures produce state deltas and unguarded writes that look exactly
    # like production defects. Classifying them is what keeps the signal clean.
    is_test: bool = False
    # Parameters an untrusted caller supplies (an extrinsic's arguments minus
    # `origin`, an external function's arguments). Empty means "not determined".
    user_inputs: list[str] = field(default_factory=list)

    @property
    def signature(self) -> str:
        return f"{self.name}({','.join(p.type_name for p in self.params)})"

    @property
    def qualified(self) -> str:
        return f"{self.contract}.{self.name}"

    @property
    def is_entry_point(self) -> bool:
        return self.visibility in {"public", "external"} or self.kind in {
            "extrinsic", "instruction", "entry"
        }

    @property
    def has_ir(self) -> bool:
        return self.ir is not None and bool(self.ir.statements)


@dataclass
class ContractEvent:
    name: str
    params: list[Parameter] = field(default_factory=list)
    line: int = 0


@dataclass
class ContractError:
    name: str
    params: list[Parameter] = field(default_factory=list)
    line: int = 0


@dataclass
class ContractType:
    name: str
    kind: str
    members: list[str] = field(default_factory=list)
    line: int = 0


@dataclass
class Contract:
    name: str
    path: str
    line: int
    bases: list[str] = field(default_factory=list)
    state_vars: list[StateVar] = field(default_factory=list)
    functions: list[Function] = field(default_factory=list)
    # v2 additions.
    kind: str = "contract"
    language: str = SOLIDITY
    end_line: int = 0
    events: list[ContractEvent] = field(default_factory=list)
    errors: list[ContractError] = field(default_factory=list)
    types: list[ContractType] = field(default_factory=list)
    modifier_definitions: list[Function] = field(default_factory=list)
    using_for: list[tuple[str, str]] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    parser: str = "regex"
    is_test: bool = False
    # Traits/interfaces this type implements, beyond declared inheritance.
    traits: list[str] = field(default_factory=list)
    # True when the type is decoded from untrusted input rather than built by
    # the runtime: a Substrate TransactionExtension is decoded straight from the
    # transaction, so every one of its fields is attacker-chosen by contract.
    user_decoded: bool = False
    module: str = ""

    @property
    def state_names(self) -> set[str]:
        return {v.name for v in self.state_vars}


@dataclass
class RuntimeWiring:
    """An ordered composition of modules declared at the runtime level.

    A Substrate `TxExtension` tuple is the clearest case: every listed extension
    runs on every transaction, in order, so two entries that disagree about what
    is allowed compose into a bypass. Crystal cannot see that by reading either
    module alone.
    """

    name: str
    kind: str
    members: list[str] = field(default_factory=list)
    path: str = ""
    line: int = 0
    language: str = RUST

    def index_of(self, member: str) -> int:
        for position, entry in enumerate(self.members):
            if entry == member or entry.rsplit("::", 1)[-1] == member:
                return position
        return -1


@dataclass
class ConfigBinding:
    """A runtime binding of a module's associated type to a concrete one.

        impl pallet_transaction_payment::Config for Runtime {
            type OnChargeTransaction = FungibleAdapter<Balances, ..>;
        }

    Without this, `T::OnChargeTransaction::withdraw_fee(..)` points nowhere and
    the debit appears to leave the analysed code.
    """

    module: str
    associated_type: str
    concrete_type: str
    runtime: str = ""
    path: str = ""
    line: int = 0

    @property
    def concrete_name(self) -> str:
        return re.sub(r"<.*", "", self.concrete_type).strip().rsplit("::", 1)[-1]


@dataclass
class Observation:
    kind: str
    title: str
    contract: str
    function: str | None
    path: str
    line: int
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class Hypothesis:
    category: str
    title: str
    rationale: str
    path: list[str]
    evidence: list[str]
    priority: float = 0.0
