"""Symbolic execution over the language-neutral IR.

Replaces the v1 regex delta reader. The engine walks statements in order,
forks on branches, unrolls constant-bounded loops, inlines shallow internal
calls and tracks every state variable as a polynomial over attacker-controlled
symbols.

What it deliberately refuses to model is recorded in `unsupported` instead of
being approximated: unbounded loops, inline assembly, unresolved storage
mutations and opaque external return values.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .. import ir as I
from ..models import Function
from .algebra import ARG_PREFIX, ENV_PREFIX, SENDER, ExpressionReader, SymExpr
from .constraints import (
    ASSERT,
    BRANCH,
    Constraint,
    ConstraintSystem,
    NEGATED_BRANCH,
    REQUIRE,
    domain_for,
)
from .state import FunctionEffect, PathEffect, SequenceEffect, SymbolicState

MAX_PATHS = 8
MAX_UNROLL = 8
MAX_INLINE_DEPTH = 2

ENVIRONMENT = {
    "msg.sender": SENDER,
    "tx.origin": f"{ENV_PREFIX}tx.origin",
    "block.timestamp": f"{ENV_PREFIX}block.timestamp",
    "block.number": f"{ENV_PREFIX}block.number",
    "block.difficulty": f"{ENV_PREFIX}block.difficulty",
    "block.prevrandao": f"{ENV_PREFIX}block.prevrandao",
    "block.coinbase": f"{ENV_PREFIX}block.coinbase",
    "block.chainid": f"{ENV_PREFIX}block.chainid",
    "now": f"{ENV_PREFIX}block.timestamp",
    "this": f"{ENV_PREFIX}this",
}

ATTACKER_INPUTS = {"msg.value", "msg.data"}

DECLARED_NAME_RE = re.compile(r"([A-Za-z_]\w*)\s*$")


@dataclass(frozen=True)
class CallRecord:
    """A call as executed: its arguments resolved to symbolic values."""

    call: object
    arguments: tuple[SymExpr, ...]
    constraints: tuple[Constraint, ...]
    caller: str


@dataclass
class _Context:
    state: SymbolicState
    locals: dict[str, SymExpr] = field(default_factory=dict)
    params: set[str] = field(default_factory=set)
    state_names: set[str] = field(default_factory=set)
    constraints: list[Constraint] = field(default_factory=list)
    unsupported: list[str] = field(default_factory=list)
    external_calls: list[str] = field(default_factory=list)
    suffix: str = ""
    origin: str = ""
    feasible: bool = True
    depth: int = 0
    stack: tuple[str, ...] = ()
    # True when the enclosing type is decoded from untrusted input, which makes
    # its fields attacker-chosen rather than protocol state.
    user_decoded: bool = False
    call_records: list[CallRecord] = field(default_factory=list)

    def fork(self) -> "_Context":
        clone = _Context(
            self.state.clone(), dict(self.locals), set(self.params),
            set(self.state_names), list(self.constraints), list(self.unsupported),
            list(self.external_calls), self.suffix, self.origin, self.feasible,
            self.depth, self.stack, self.user_decoded,
        )
        clone.call_records = list(self.call_records)
        return clone

    def note(self, message: str) -> None:
        if message not in self.unsupported:
            self.unsupported.append(message)


def _normalize(path: str) -> str:
    cleaned = (path or "").strip()
    for prefix in ("self.", "Self.", "*"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix):]
    return cleaned


def _base_of(path: str) -> str:
    return re.split(r"[\[.]", _normalize(path), maxsplit=1)[0]


class SymbolicEngine:
    def __init__(self, contracts, max_paths: int = MAX_PATHS,
                 max_unroll: int = MAX_UNROLL,
                 max_inline_depth: int = MAX_INLINE_DEPTH):
        self.contracts = list(contracts)
        self.max_paths = max_paths
        self.max_unroll = max_unroll
        self.max_inline_depth = max_inline_depth
        self.functions: dict[str, Function] = {}
        self.by_contract: dict[str, dict[str, Function]] = {}
        self.state_names: dict[str, set[str]] = {}
        self.user_decoded: dict[str, bool] = {}
        for contract in self.contracts:
            names = {variable.name for variable in contract.state_vars}
            self.state_names[contract.name] = names
            self.user_decoded[contract.name] = getattr(contract, "user_decoded", False)
            table = self.by_contract.setdefault(contract.name, {})
            for function in list(contract.functions) + list(contract.modifier_definitions):
                self.functions[f"{contract.name}.{function.name}"] = function
                table.setdefault(function.name, function)

    # -- public API ------------------------------------------------------
    def execute_function(self, function: Function) -> FunctionEffect:
        state = SymbolicState()
        contexts = self._execute(function, state, suffix="", depth=0)
        feasible = [c for c in contexts if c.feasible] or contexts[:1]

        paths = tuple(
            PathEffect(tuple(c.constraints), _render(c.state.deltas()), c.feasible)
            for c in feasible
        )
        merged = _merge_paths(paths)
        primary = feasible[0]
        return FunctionEffect(
            function=f"{function.contract}.{function.name}",
            deltas=merged,
            reads=tuple(sorted(function.reads)),
            writes=tuple(sorted(function.writes)),
            constraints=tuple(primary.constraints),
            paths=paths,
            external_calls=tuple(dict.fromkeys(
                call for c in feasible for call in c.external_calls
            )),
            unsupported=tuple(dict.fromkeys(
                note for c in feasible for note in c.unsupported
            )),
            branch_dependent=len(paths) > 1,
            expressions=primary.state.deltas(),
        )

    def call_records(self, function: Function) -> list[CallRecord]:
        """Every call the function makes, with arguments resolved symbolically.

        This is what makes "an unbounded user input reaches a debit" answerable:
        the amount argument carries `ARG:` symbols exactly when it is derived
        from caller-supplied data.
        """
        state = SymbolicState()
        contexts = self._execute(function, state, suffix="", depth=0)
        feasible = [c for c in contexts if c.feasible] or contexts[:1]
        records: list[CallRecord] = []
        seen = set()
        for context in feasible:
            for record in context.call_records:
                key = (record.call.line, record.call.callee,
                       tuple(a.render() for a in record.arguments))
                if key not in seen:
                    seen.add(key)
                    records.append(record)
        return records

    def execute_sequence(self, names) -> SequenceEffect | None:
        functions = [self.functions.get(name) for name in names]
        if any(function is None for function in functions):
            return None

        state = SymbolicState()
        constraints: list[Constraint] = []
        unsupported: list[str] = []
        branch_dependent = False

        for step, function in enumerate(functions, start=1):
            contexts = self._execute(function, state, suffix=f"#{step}", depth=0)
            feasible = [c for c in contexts if c.feasible] or contexts[:1]
            branch_dependent = branch_dependent or len(feasible) > 1
            chosen = feasible[0]
            state = chosen.state
            constraints.extend(chosen.constraints)
            unsupported.extend(chosen.unsupported)

        deltas = state.deltas()
        before = {
            base: _initial_render(state, base) for base in sorted(state.touched)
        }
        after: dict[str, str] = {}
        for base in sorted(state.touched):
            delta = deltas.get(base, SymExpr.zero())
            after[base] = before[base] if delta.is_zero else \
                f"{before[base]} + ({delta.render()})"
        expressions = {
            base: deltas.get(base, SymExpr.zero()) for base in sorted(state.touched)
        }
        return SequenceEffect(
            sequence=tuple(names),
            before=before,
            after=after,
            deltas=_render(expressions),
            touched=tuple(sorted(state.touched)),
            constraints=tuple(constraints),
            unsupported=tuple(dict.fromkeys(unsupported)),
            branch_dependent=branch_dependent,
            expressions=expressions,
            initial={
                base: _initial_expr(state, base) for base in sorted(state.touched)
            },
        )

    def constraint_system(self, names) -> ConstraintSystem:
        effect = self.execute_sequence(names)
        expressions: list[str] = []
        ranges: dict[str, tuple[int, int]] = {}
        types: dict[str, str] = {}
        if effect is not None:
            for base, delta in sorted(effect.deltas.items()):
                if delta != "0":
                    expressions.append(f"delta({base}) == {delta}")
            for constraint in effect.constraints:
                expressions.append(constraint.render())
        for name in names:
            function = self.functions.get(name)
            ranges[name] = (0, 2 ** 16 - 1)
            if function is None:
                continue
            for parameter in function.params:
                if not parameter.name:
                    continue
                key = f"{name}.{parameter.name}"
                ranges[key] = domain_for(parameter.type_name)
                types[key] = parameter.type_name
        return ConstraintSystem(
            tuple(dict.fromkeys(expressions)), ranges,
            tuple(effect.constraints) if effect else (), types,
            tuple(effect.unsupported) if effect else (),
        )

    # -- execution core --------------------------------------------------
    def _execute(self, function: Function, state: SymbolicState,
                 suffix: str, depth: int,
                 arguments: dict[str, SymExpr] | None = None,
                 stack: tuple[str, ...] = ()) -> list[_Context]:
        context = _Context(
            state=state,
            params={p.name for p in function.params if p.name},
            state_names=set(self.state_names.get(function.contract, set())),
            suffix=suffix,
            origin=f"{function.contract}.{function.name}",
            depth=depth,
            stack=stack + (f"{function.contract}.{function.name}",),
            user_decoded=self.user_decoded.get(function.contract, False),
        )
        if arguments:
            context.locals.update(arguments)
        for modifier in function.modifiers:
            context.constraints.append(
                Constraint("modifier", modifier, function.line, context.origin)
            )
        body = function.ir
        if body is None:
            context.note(f"{context.origin}: no statement IR available")
            return [context]
        if body.has_assembly:
            context.note(f"{context.origin}: inline assembly not modelled")
        for reason in body.unsupported:
            context.note(f"{context.origin}: {reason}")
        return self._run(body.statements, [context])

    def _run(self, statements, contexts: list[_Context]) -> list[_Context]:
        for statement in statements:
            next_contexts: list[_Context] = []
            for context in contexts:
                if not context.feasible:
                    next_contexts.append(context)
                    continue
                next_contexts.extend(self._step(statement, context))
            if len(next_contexts) > self.max_paths:
                kept = next_contexts[:self.max_paths]
                kept[0].note(
                    f"path cap reached ({self.max_paths}); "
                    f"{len(next_contexts) - self.max_paths} path(s) not explored"
                )
                next_contexts = kept
            contexts = next_contexts
        return contexts

    def _step(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        handler = {
            I.ASSIGN: self._assign,
            I.VAR_DECL: self._declare,
            I.REQUIRE: self._require,
            I.REVERT: self._revert,
            I.IF: self._branch,
            I.LOOP: self._loop,
            I.CALL: self._call,
            I.BLOCK: self._block,
            I.DELETE: self._delete,
            I.ASSEMBLY: self._assembly,
        }.get(statement.kind)
        if handler is None:
            self._record_call(statement, context)
            return [context]
        return handler(statement, context)

    # -- statement handlers ----------------------------------------------
    def _assign(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        self._record_call(statement, context)
        target_text = statement.target.text if statement.target else ""
        path = self._target_path(context, target_text)
        if not path:
            return [context]
        base = _base_of(path)
        if base not in context.state_names:
            # Front-ends that resolve field access themselves (Rust `self.x`,
            # Move `pool.total`) already named the state variable.
            declared = [w for w in statement.writes if w in context.state_names]
            if declared:
                path = base = declared[0]
        value = self._read(context, statement.value.text if statement.value else "0")
        operator = statement.operator or "="

        if operator == "?=":
            context.note(
                f"{context.origin}: unmodelled mutation of {base} "
                f"at line {statement.line}"
            )
            current = self._current(context, path, base)
            value = current + SymExpr.symbol(f"MUTATE:{base}{context.suffix}")
        elif operator != "=":
            current = self._current(context, path, base)
            value = {
                "+=": lambda: current + value,
                "-=": lambda: current - value,
                "*=": lambda: current * value,
                "/=": lambda: current.divide(value),
            }.get(operator, lambda: current + value)()
            if operator not in {"+=", "-=", "*=", "/="}:
                context.note(
                    f"{context.origin}: opaque compound operator "
                    f"'{operator}' at line {statement.line}"
                )
                value = current + SymExpr.symbol(f"OPAQUE:{base}{context.suffix}")

        if base in context.state_names:
            context.state.write(path, base, value)
        else:
            context.locals[path] = value
        return [context]

    def _declare(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        self._record_call(statement, context)
        value = self._read(context, statement.value.text if statement.value else "0")
        target_text = statement.target.text if statement.target else ""
        for name in _declared_names(target_text):
            context.locals[name] = value
        return [context]

    def _require(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        expression = statement.condition.text if statement.condition else statement.text
        context.constraints.append(Constraint(
            ASSERT if statement.note == "assert" else REQUIRE,
            expression, statement.line, context.origin,
        ))
        return [context]

    def _revert(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        context.feasible = False
        return [context]

    def _branch(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        condition = statement.condition.text if statement.condition else statement.text
        # Fork before executing either side so the two paths never share state.
        skipped = context.fork()
        context.constraints.append(
            Constraint(BRANCH, condition, statement.line, context.origin)
        )
        skipped.constraints.append(
            Constraint(NEGATED_BRANCH, condition, statement.line, context.origin)
        )
        return (self._run(statement.body, [context])
                + self._run(statement.orelse, [skipped]))

    def _loop(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        bound = statement.loop_bound
        if bound is None:
            context.note(
                f"{context.origin}: unbounded loop at line {statement.line} "
                f"executed once"
            )
            iterations = 1
        else:
            iterations = min(bound, self.max_unroll)
            if bound > self.max_unroll:
                context.note(
                    f"{context.origin}: loop at line {statement.line} unrolled "
                    f"{self.max_unroll}/{bound} times"
                )
        contexts = [context]
        for _ in range(max(iterations, 0)):
            contexts = self._run(statement.body, contexts)
        return contexts

    def _block(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        return self._run(statement.body, [context])

    def _delete(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        path = self._target_path(context, statement.target.text if statement.target else "")
        if not path:
            return [context]
        base = _base_of(path)
        if base in context.state_names:
            context.state.write(path, base, SymExpr.zero())
        else:
            context.locals[path] = SymExpr.zero()
        return [context]

    def _assembly(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        context.note(
            f"{context.origin}: inline assembly at line {statement.line} "
            f"(storage effects not resolved)"
        )
        return [context]

    def _call(self, statement: I.IRStmt, context: _Context) -> list[_Context]:
        self._record_call(statement, context)
        call = statement.call
        if call is None:
            return self._run(statement.body, [context])
        if call.kind in I.EXTERNAL_CALL_KINDS:
            return self._run(statement.body, [context])
        inlined = self._inline(call, context)
        if inlined is not None:
            return inlined
        return self._run(statement.body, [context])

    def _inline(self, call: I.IRCall, context: _Context) -> list[_Context] | None:
        if context.depth >= self.max_inline_depth:
            return None
        contract = context.origin.split(".", 1)[0]
        callee = self.by_contract.get(contract, {}).get(call.callee)
        if callee is None or callee.ir is None:
            return None
        qualified = f"{contract}.{callee.name}"
        if qualified in context.stack:
            context.note(f"{context.origin}: recursive call to {qualified} not unrolled")
            return None
        arguments: dict[str, SymExpr] = {}
        for parameter, argument in zip(callee.params, call.arguments):
            if parameter.name:
                arguments[parameter.name] = self._read(context, argument.text)
        results = self._execute(
            callee, context.state, context.suffix, context.depth + 1,
            arguments, context.stack,
        )
        feasible = [result for result in results if result.feasible] or results
        if not feasible:
            return None
        if len(results) > 1:
            context.note(
                f"{context.origin}: {qualified} has {len(results)} paths; "
                f"first feasible path inlined"
            )
        chosen = feasible[0]
        context.state = chosen.state
        context.constraints.extend(
            c for c in chosen.constraints if c not in context.constraints
        )
        for note in chosen.unsupported:
            context.note(note)
        context.external_calls.extend(chosen.external_calls)
        return [context]

    def _record_call(self, statement: I.IRStmt, context: _Context) -> None:
        call = statement.call
        if call is None:
            return
        # Resolve every argument on the path that reaches this call, so a
        # detector can ask "what actually flows into this amount?" rather than
        # re-reading the source text.
        context.call_records.append(CallRecord(
            call,
            tuple(self._read(context, argument.text) for argument in call.arguments),
            tuple(context.constraints),
            context.origin,
        ))
        if call.kind not in I.EXTERNAL_CALL_KINDS:
            return
        label = f"{call.receiver}.{call.callee}" if call.receiver else call.callee
        context.external_calls.append(f"{label}@{call.line}")

    # -- expression evaluation -------------------------------------------
    def _reader(self, context: _Context, capture: list[str] | None = None):
        def resolve(path: str) -> SymExpr:
            if capture is not None:
                capture.append(path)
            return self._resolve(context, path)
        return ExpressionReader(resolve, context.note, context.suffix)

    def _read(self, context: _Context, text: str) -> SymExpr:
        if not (text or "").strip():
            return SymExpr.zero()
        return self._reader(context).read(text)

    def _target_path(self, context: _Context, text: str) -> str | None:
        cleaned = _normalize(text)
        if not cleaned or "(" in cleaned.split("[")[0]:
            return None
        captured: list[str] = []
        self._reader(context, captured).read(cleaned)
        if not captured:
            return _normalize(cleaned) or None
        return _normalize(captured[-1])

    def _current(self, context: _Context, path: str, base: str) -> SymExpr:
        if base in context.state_names:
            return context.state.read(path, base)
        return context.locals.get(path, SymExpr.symbol(f"LOCAL:{path}{context.suffix}"))

    def _resolve(self, context: _Context, raw_path: str) -> SymExpr:
        path = _normalize(raw_path)
        if path in ENVIRONMENT:
            return SymExpr.symbol(ENVIRONMENT[path])
        if path in ATTACKER_INPUTS:
            return SymExpr.symbol(f"{ARG_PREFIX}{path}{context.suffix}")
        if path in context.locals:
            return context.locals[path]
        base = _base_of(path)
        if base in context.state_names:
            if context.user_decoded:
                # Fields of a transaction-decoded type are chosen by whoever
                # signed the transaction, not by the protocol.
                return SymExpr.symbol(f"{ARG_PREFIX}self.{path}{context.suffix}")
            return context.state.read(path, base)
        if base in context.params:
            return SymExpr.symbol(f"{ARG_PREFIX}{path}{context.suffix}")
        if base in context.locals:
            return context.locals[base]
        field = path.split(".")[-1].split("[")[0]
        if field != base and field in context.state_names:
            return context.state.read(field, field)
        if path.startswith(("block.", "msg.", "tx.")):
            return SymExpr.symbol(f"{ENV_PREFIX}{path}")
        return SymExpr.symbol(f"{ENV_PREFIX}{path}{context.suffix}")


def _declared_names(text: str) -> list[str]:
    cleaned = _normalize(text).strip().strip("()")
    names: list[str] = []
    for part in cleaned.split(","):
        stripped = part.split(":")[0].strip()
        if not stripped:
            continue
        match = DECLARED_NAME_RE.search(stripped)
        if match:
            names.append(match.group(1))
    return names


def _render(deltas: dict[str, SymExpr]) -> dict[str, str]:
    return {name: value.render() for name, value in sorted(deltas.items())}


def _initial_expr(state: SymbolicState, base: str) -> SymExpr:
    total = SymExpr.zero()
    for path, mapped in state.bases.items():
        if mapped == base:
            total = total + state.initial.get(path, SymExpr.zero())
    return total


def _initial_render(state: SymbolicState, base: str) -> str:
    return _initial_expr(state, base).render()


def _merge_paths(paths: tuple[PathEffect, ...]) -> dict[str, str]:
    if not paths:
        return {}
    if len(paths) == 1:
        return dict(paths[0].deltas)
    names = sorted({name for path in paths for name in path.deltas})
    merged: dict[str, str] = {}
    for name in names:
        values = [path.deltas.get(name, "0") for path in paths]
        unique = sorted(set(values))
        merged[name] = unique[0] if len(unique) == 1 else f"PHI({'|'.join(unique)})"
    return merged
