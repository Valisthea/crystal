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

from ..ir import EXTERNAL_CALL_KINDS
from ..vocabulary import AUTH_HELPERS, GUARD_CALL_HINTS, GUARD_NAME_HINTS

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
    # Concrete types this stage reaches through runtime-bound associated types.
    routed_to: list[str] = field(default_factory=list)
    routes: list[str] = field(default_factory=list)
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
    # `OnChargeTransaction` -> `FungibleAdapter`, as bound by the runtime.
    associated_types: dict[str, str] = field(default_factory=dict)

    def __bool__(self) -> bool:
        return bool(self.pipelines or self.edges)

    def resolve(self, reference: str) -> str:
        """`<T as Config>::OnChargeTransaction` -> `FungibleAdapter`."""
        for segment in reversed(re.split(r"::|\.|<|>|\s", reference or "")):
            segment = segment.strip()
            if segment in self.associated_types:
                return self.associated_types[segment]
        return ""


ASSOCIATED_REF_RE = re.compile(r"\bT\s*::\s*(\w+)|<\s*T\s+as\s+[\w:]+\s*>\s*::\s*(\w+)")


def resolve_associated_types(bindings) -> dict[str, str]:
    """Map each associated type to the concrete type the runtime bound to it."""
    resolved: dict[str, str] = {}
    for binding in bindings or ():
        name = binding.concrete_name
        if name and binding.associated_type not in resolved:
            resolved[binding.associated_type] = name
    return resolved


def _short(name: str) -> str:
    return re.sub(r"<[^>]*>", "", name or "").strip().rsplit("::", 1)[-1]


def _associated_name(reference: str) -> str:
    """`<<T as Config>::OnChargeTransaction as OnChargeTransaction<T>>` -> the name."""
    match = ASSOCIATED_REF_RE.search(reference or "")
    name = (match.group(1) or match.group(2)) if match else _short(reference)
    return re.split(r"\s+as\s+", name or "")[0].strip().strip("<>").strip()


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


def _routed_targets(contract, graph, by_name) -> tuple[list[str], list[str]]:
    """Concrete types this contract reaches through runtime-bound `T::X` paths."""
    routes: list[str] = []
    targets: list[str] = []
    for function in contract.functions:
        if function.ir is None:
            continue
        for call in function.ir.calls():
            resolved = graph.resolve(call.receiver or "")
            # Only routes that land on parsed code are reported: an unresolved
            # target says nothing about what the operation does.
            if not resolved or resolved == contract.name or resolved not in by_name:
                continue
            label = (f"{_associated_name(call.receiver or '')}::{call.callee}"
                     f" -> {resolved}")
            if label not in routes:
                routes.append(label)
            if resolved not in targets:
                targets.append(resolved)
    return routes, targets


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


def build_module_graph(contracts, wirings=(), bindings=()) -> ModuleGraph:
    graph = ModuleGraph()
    by_name = {contract.name: contract for contract in contracts}
    graph.modules = {
        contract.name: contract.module or contract.path for contract in contracts
    }
    graph.associated_types = resolve_associated_types(bindings)

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
                stage.value_evidence = _value_signals(contract)
                stage.routes, stage.routed_to = _routed_targets(
                    contract, graph, by_name
                )
                # A stage that hands the operation to a runtime-bound
                # implementation inherits what that implementation does: the
                # debit really happens there, and so would the missing check.
                for target in stage.routed_to:
                    routed_contract = by_name.get(target)
                    if routed_contract is None:
                        continue
                    routed_guards, routed_consulted = _guard_signals(routed_contract)
                    guard_evidence = guard_evidence + routed_guards
                    consulted = sorted(set(consulted) | set(routed_consulted))
                    stage.value_evidence.extend(_value_signals(routed_contract))
                stage.guard_evidence = guard_evidence
                stage.consulted_guards = consulted
                stage.is_guard = bool(guard_evidence)
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
                receiver = call.receiver or ""
                target = _short(receiver)
                if target and target in by_name and target != contract.name:
                    graph.edges.append(ModuleEdge(
                        contract.name, target, "calls", call.callee, 0.7,
                    ))
                    continue
                # `<T as Config>::OnChargeTransaction::withdraw_fee(..)` only
                # points somewhere once the runtime binding is known.
                routed = graph.resolve(receiver)
                if routed and routed != contract.name:
                    graph.edges.append(ModuleEdge(
                        contract.name, routed, "config-routed",
                        f"{_associated_name(receiver)}::{call.callee}", 0.85,
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
