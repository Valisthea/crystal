"""Property IR: the forms Crystal can compile *exactly*, and nothing else.

A campaign invariant is text attached to a candidate. Before it can reach a
backend it has to become one of the forms below, each of which names the
contract-qualified state it reads and the entry points it exercises. Anything
the statement says that does not fit a form is not approximated: the invariant
compiles to `Unsupported` with the precise reason, which is a correct result.

The forms:

* `Relation`   — an arithmetic relation between sums over mappings, scalars
                 and a contract's ether balance (`P1`-shaped conservation).
* `Exclusion`  — at most one of several entry points succeeds for one key
                 (`P2`-shaped single settlement).
* `Replay`     — an entry point cannot succeed twice for one key, and a state
                 value cannot be reached twice for one key (`P4`-shaped).
* `Independence` — the success of a settlement entry point never depends on a
                 piece of state it reaches through a bound call: no revert of
                 that entry point may be conditioned on the state (`P5`-shaped).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..naming import qualify

FOUNDRY = "foundry"
MEDUSA = "medusa"
HALMOS = "halmos"
BACKENDS = (FOUNDRY, MEDUSA, HALMOS)


@dataclass(frozen=True)
class StateRef:
    """A state variable, qualified by the contract that declares it, and the
    public surface through which a harness can actually read it."""

    contract: str
    name: str
    type_name: str
    key_types: tuple[str, ...] = ()
    value_type: str = ""
    getter: str = ""
    getter_kind: str = ""  # "auto-getter" | "view-function" | ""

    @property
    def qualified(self) -> str:
        return qualify(self.contract, self.name)

    @property
    def is_mapping(self) -> bool:
        return bool(self.key_types)

    def access(self) -> str:
        """How the harness reads this state, for the audit record."""
        if not self.getter:
            return f"{self.qualified} (no public accessor)"
        keys = ",".join(self.key_types)
        return f"{self.qualified} via {self.getter}({keys}) [{self.getter_kind}]"


@dataclass(frozen=True)
class FunctionRef:
    contract: str
    name: str
    params: tuple[tuple[str, str], ...] = ()  # (type, name)
    payable: bool = False
    role: str | None = None  # `onlyRole(X)` argument, when the entry point has one

    @property
    def qualified(self) -> str:
        return f"{self.contract}.{self.name}"


@dataclass(frozen=True)
class Term:
    kind: str  # "sum" | "scalar" | "balance"
    text: str  # the statement fragment this term came from
    state: StateRef | None = None
    contract: str = ""

    def describe(self) -> str:
        if self.kind == "balance":
            return f"balance({self.contract})"
        if self.kind == "sum":
            return f"sum({self.state.qualified})"
        return self.state.qualified


@dataclass(frozen=True)
class Relation:
    lhs: tuple[Term, ...]
    op: str  # "==" | ">=" | "<="
    rhs: tuple[Term, ...]

    @property
    def form(self) -> str:
        return "relation"

    def states(self) -> tuple[StateRef, ...]:
        return tuple(t.state for t in self.lhs + self.rhs if t.state is not None)

    def contracts(self) -> tuple[str, ...]:
        seen: list[str] = []
        for term in self.lhs + self.rhs:
            name = term.contract or (term.state.contract if term.state else "")
            if name and name not in seen:
                seen.append(name)
        return tuple(seen)

    def describe(self) -> str:
        left = " + ".join(t.describe() for t in self.lhs)
        right = " + ".join(t.describe() for t in self.rhs)
        return f"{left} {self.op} {right}"


@dataclass(frozen=True)
class Exclusion:
    key: str
    functions: tuple[FunctionRef, ...]

    @property
    def form(self) -> str:
        return "exclusion"

    def describe(self) -> str:
        names = ", ".join(f.qualified for f in self.functions)
        return f"at most one of [{names}] succeeds per {self.key}"


@dataclass(frozen=True)
class Replay:
    key: str
    once: tuple[FunctionRef, ...]
    reach: StateRef | None = None
    reach_value: str = ""
    reach_writers: tuple[FunctionRef, ...] = ()

    @property
    def form(self) -> str:
        return "replay"

    def describe(self) -> str:
        parts = [f"{f.qualified} succeeds at most once per {self.key}" for f in self.once]
        if self.reach is not None:
            writers = ", ".join(f.qualified for f in self.reach_writers)
            parts.append(
                f"{self.reach.qualified} reaches {self.reach_value} at most once per "
                f"{self.key} (written by {writers})"
            )
        return "; ".join(parts)


@dataclass(frozen=True)
class Guard:
    """A revert of `function` whose condition depends on the watched state."""

    function: str  # qualified entry point
    line: int
    condition: str
    error_owner: str  # contract/interface/library declaring the error, "" for Error(string)
    error_name: str  # "" for a plain require/revert with a string message
    message: str = ""

    def selector_expr(self) -> str:
        """Solidity expression for the revert selector this guard produces."""
        if self.error_name:
            owner = f"{self.error_owner}." if self.error_owner else ""
            return f"{owner}{self.error_name}.selector"
        return 'bytes4(keccak256("Error(string)"))'

    def describe(self) -> str:
        what = f"{self.error_owner}.{self.error_name}" if self.error_name else "Error(string)"
        return f"{self.function}:{self.line} `{self.condition}` -> {what}"


@dataclass(frozen=True)
class Independence:
    functions: tuple[FunctionRef, ...]
    state: StateRef
    guards: tuple[Guard, ...]
    arithmetic_on_state: bool = False  # a bound writer does `-=`/`+=` on the state

    @property
    def form(self) -> str:
        return "independence"

    def describe(self) -> str:
        names = ", ".join(f.qualified for f in self.functions)
        return f"success of [{names}] never depends on {self.state.qualified}"


@dataclass(frozen=True)
class Unsupported:
    reason: str

    @property
    def form(self) -> str:
        return "unsupported"

    def describe(self) -> str:
        return f"UNSUPPORTED: {self.reason}"


PropertyIR = Relation | Exclusion | Replay | Independence | Unsupported


@dataclass
class CompiledProperty:
    """One invariant compiled for one backend.

    `source` is the harness (empty when unsupported); `reads` lists every piece
    of contract-qualified state the harness reads and how, so the harness can
    be audited against `statement`.
    """

    backend: str
    source: str
    filename: str
    unsupported_reason: str | None
    name: str
    campaign_id: str
    statement: str
    form: str
    reads: tuple[str, ...] = ()
    exercises: tuple[str, ...] = ()
    extra_files: dict[str, str] = field(default_factory=dict)
    notes: tuple[str, ...] = ()

    @property
    def supported(self) -> bool:
        return self.unsupported_reason is None
