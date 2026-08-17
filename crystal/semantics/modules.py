"""Cross-module composition.

Crystal analysed every type in isolation, which is exactly the blind spot that
loses a chain: a guard lives in one module and the operation it should cover
lives in another, and neither file is wrong on its own.

A Substrate `TxExtension` tuple is the clearest instance. Every extension listed
in it runs on every transaction, in order. If one entry rejects a transfer for a
protected account and a later entry moves value without consulting that check,
the pipeline as a whole has a hole that neither module contains.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..detectors.base import AUTH_HELPERS
from ..ir import EXTERNAL_CALL_KINDS

# Storage and helpers whose purpose is to restrict who or what may proceed.
GUARD_NAME_HINTS = (
    "highsecurity", "high_security", "whitelist", "allowlist", "blocklist",
    "blacklist", "denylist", "frozen", "freeze", "paused", "pause", "blocked",
    "restricted", "reversible", "guardian", "locked", "banned", "sanction",
    "permission", "authorized", "authorised", "approved", "eligib",
)
GUARD_CALL_HINTS = (
    "is_allowed", "is_call_allowed", "is_high_security", "is_whitelisted",
    "is_frozen", "is_paused", "is_blocked", "is_authorized", "is_authorised",
    "can_transfer", "ensure_allowed", "check_allowed", "is_permitted",
    "is_restricted", "is_reversible",
)

# Operations that move value. Matched on whole name segments: `count_transfers`
# contains "transfer" and moves nothing, and reading it as a value operation is
# what turned an event-scanning extension into a false bypass.
VALUE_CALL_RE = re.compile(
    r"^(?:do_|try_|force_|unchecked_|_)?"
    r"(?:transfer|withdraw|deposit|mint|burn|slash|settle|repay|redeem|reserve"
    r"|unreserve|charge|pay|debit|send|refund)"
    r"(?:_\w+)?$"
)
VALUE_CALL_EXACT = {
    "correct_and_deposit_fee", "correct_and_deposit", "can_withdraw_fee",
    "withdraw_fee", "deposit_into_existing", "deposit_creating",
    "make_free_balance_be", "resolve_into_existing",
}


def _moves_value(callee: str) -> bool:
    name = (callee or "").lower()
    return name in VALUE_CALL_EXACT or bool(VALUE_CALL_RE.match(name))

REJECTION_TOKENS = ("err(", "return err", "revert", "invalid", "reject",
                    "ensure!", "assert", "throw")


@dataclass
class StageRole:
    """What one member of a composition pipeline actually does."""

    name: str
    index: int
    resolved: bool
    is_guard: bool = False
    moves_value: bool = False
    guard_evidence: list[str] = field(default_factory=list)
    value_evidence: list[str] = field(default_factory=list)
    consulted_guards: list[str] = field(default_factory=list)
    path: str = ""
    line: int = 0


@dataclass
class Pipeline:
    name: str
    kind: str
    path: str
    line: int
    stages: list[StageRole] = field(default_factory=list)

    @property
    def guards(self) -> list[StageRole]:
        return [stage for stage in self.stages if stage.is_guard]

    @property
    def movers(self) -> list[StageRole]:
        return [stage for stage in self.stages if stage.moves_value]


@dataclass(frozen=True)
class ModuleEdge:
    source: str
    target: str
    kind: str
    via: str = ""
    confidence: float = 0.6


@dataclass
class ModuleGraph:
    pipelines: list[Pipeline] = field(default_factory=list)
    edges: list[ModuleEdge] = field(default_factory=list)
    modules: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.pipelines or self.edges)


def _short(name: str) -> str:
    return re.sub(r"<[^>]*>", "", name or "").strip().rsplit("::", 1)[-1]


def _guard_signals(contract) -> tuple[list[str], list[str]]:
    """Guard checks the type performs, and guard names it consults."""
    evidence: list[str] = []
    consulted: list[str] = []
    for function in contract.functions:
        if function.ir is None:
            continue
        for statement in function.ir.walk():
            text = (statement.text or "")
            lowered = text.lower()
            call = statement.call
            callee = (call.callee or "").lower() if call is not None else ""
            hit = ""
            if callee and (callee in AUTH_HELPERS
                           or any(hint in callee for hint in GUARD_CALL_HINTS)):
                hit = callee
            elif any(hint in lowered for hint in GUARD_NAME_HINTS) and \
                    statement.kind in {"require", "if", "revert", "call", "var_decl"}:
                hit = next(hint for hint in GUARD_NAME_HINTS if hint in lowered)
            if not hit:
                continue
            rejects = statement.kind in {"require", "revert"} or any(
                token in lowered for token in REJECTION_TOKENS
            ) or _rejects_nearby(function, statement)
            if rejects:
                evidence.append(
                    f"{contract.name}.{function.name}:{statement.line} {text[:140]}"
                )
                consulted.append(hit)
    return evidence, sorted(set(consulted))


def _rejects_nearby(function, statement) -> bool:
    """An `if <guard> { return Err(..) }` rejects even without a require."""
    for child in list(statement.body) + list(statement.orelse):
        lowered = (child.text or "").lower()
        if child.kind in {"revert", "return"} and any(
            token in lowered for token in REJECTION_TOKENS
        ):
            return True
    return False


def _value_signals(contract) -> list[str]:
    evidence: list[str] = []
    for function in contract.functions:
        if function.ir is None:
            continue
        for statement in function.ir.walk():
            call = statement.call
            if call is None:
                continue
            callee = (call.callee or "").lower()
            if _moves_value(callee):
                evidence.append(
                    f"{contract.name}.{function.name}:{call.line} "
                    f"{(statement.text or '')[:140]}"
                )
            elif call.kind in EXTERNAL_CALL_KINDS and call.value_attached:
                evidence.append(
                    f"{contract.name}.{function.name}:{call.line} value-bearing "
                    f"external call {(statement.text or '')[:110]}"
                )
    return evidence


def build_module_graph(contracts, wirings=()) -> ModuleGraph:
    graph = ModuleGraph()
    by_name = {contract.name: contract for contract in contracts}
    graph.modules = {
        contract.name: contract.module or contract.path for contract in contracts
    }

    for wiring in wirings or ():
        pipeline = Pipeline(wiring.name, wiring.kind, wiring.path, wiring.line)
        for index, member in enumerate(wiring.members):
            short = _short(member)
            contract = by_name.get(short)
            stage = StageRole(short, index, contract is not None)
            if contract is not None:
                stage.path = contract.path
                stage.line = contract.line
                guard_evidence, consulted = _guard_signals(contract)
                stage.guard_evidence = guard_evidence
                stage.consulted_guards = consulted
                stage.is_guard = bool(guard_evidence)
                stage.value_evidence = _value_signals(contract)
                stage.moves_value = bool(stage.value_evidence)
            pipeline.stages.append(stage)
        graph.pipelines.append(pipeline)

        for stage in pipeline.stages:
            for other in pipeline.stages:
                if stage.index >= other.index:
                    continue
                graph.edges.append(ModuleEdge(
                    stage.name, other.name, "runs-before", wiring.name, 0.9,
                ))

    for contract in contracts:
        for function in contract.functions:
            if function.ir is None:
                continue
            for call in function.ir.calls():
                target = _short(call.receiver or "")
                if target and target in by_name and target != contract.name:
                    graph.edges.append(ModuleEdge(
                        contract.name, target, "calls", call.callee, 0.7,
                    ))

    seen = set()
    unique = []
    for edge in graph.edges:
        key = (edge.source, edge.target, edge.kind, edge.via)
        if key not in seen:
            seen.add(key)
            unique.append(edge)
    graph.edges = sorted(unique, key=lambda x: (x.source, x.kind, x.target))
    return graph
