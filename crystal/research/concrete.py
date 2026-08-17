"""Deterministic concrete validation of symbolic hypotheses.

The v1 engine was a regex micro-emulator that only understood `+=`/`-=`. v2
substitutes concrete boundary values into the polynomials produced by the
symbolic engine, so a counterexample is an arithmetic consequence of the parsed
code rather than a guess.

Every assumption Crystal had to make (unknown initial state, unresolved return
values, unverified guards) is written into the trace. A counterexample is still
evidence, never a confirmed finding.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from ..symbolic import SymbolicEngine
from ..symbolic.algebra import ARG_PREFIX, STATE_PREFIX, SymExpr
from ..symbolic.constraints import boundary_values

DEFAULT_STATE_VALUE = 1000
UNSIGNED_HINTS = ("uint", "u8", "u16", "u32", "u64", "u128", "storagevalue",
                  "storagemap", "balance", "hashmap")


@dataclass(frozen=True)
class FuzzParameter:
    name: str
    values: tuple[int, ...]
    reason: str
    type_name: str = ""
    owner: str = ""


@dataclass(frozen=True)
class ConcreteTrace:
    sequence: list[str]
    arguments: list[dict[str, int]]
    before: dict[str, int]
    after: dict[str, int]
    delta: dict[str, int]
    invariant_violations: list[str]
    reproducible: bool
    seed: int
    assumptions: tuple[str, ...] = ()


@dataclass(frozen=True)
class FuzzResult:
    hypothesis: list[str]
    trials: int
    counterexamples: list[ConcreteTrace]
    status: str
    backend: str = "symbolic-emulator"
    assumptions: tuple[str, ...] = ()
    parameters: dict[str, list[str]] = field(default_factory=dict)


def _fnmap(contracts):
    return {f"{c.name}.{f.name}": f for c in contracts for f in c.functions}


def _statemap(contracts):
    return {
        variable.name: variable
        for contract in contracts for variable in contract.state_vars
    }


def _params_for_function(function):
    """Typed parameters from the parsed signature; no fabricated names."""
    out: list[FuzzParameter] = []
    for parameter in function.params:
        if not parameter.name or parameter.name == "self":
            continue
        out.append(FuzzParameter(
            parameter.name, boundary_values(parameter.type_name),
            "boundary-first exploration of the declared type",
            parameter.type_name, f"{function.contract}.{function.name}",
        ))
    if function.payable:
        out.append(FuzzParameter(
            "msg.value", boundary_values("uint256"),
            "payable entry point: attacker-controlled call value", "uint256",
            f"{function.contract}.{function.name}",
        ))
    return out


def _is_unsigned(variable) -> bool:
    if variable is None:
        return True
    text = f"{variable.type_name} {variable.value_type}".lower()
    return any(hint in text for hint in UNSIGNED_HINTS) and "int256 " not in text


def _bindings(sequence, chosen, initial_state):
    mapping: dict[str, SymExpr] = {}
    for step, (name, arguments) in enumerate(zip(sequence, chosen), start=1):
        for parameter, value in arguments.items():
            mapping[f"{ARG_PREFIX}{parameter}#{step}"] = SymExpr.const(value)
            mapping[f"{ARG_PREFIX}{parameter}"] = SymExpr.const(value)
    for path, value in initial_state.items():
        mapping[f"{STATE_PREFIX}{path}"] = SymExpr.const(value)
    return mapping


def _evaluate(expression: SymExpr, mapping) -> int | None:
    """Return a concrete value, or None when a symbol stayed unresolved."""
    substituted = expression.substitute(mapping)
    if substituted.symbols:
        return None
    return substituted.constant_value


def fuzz_hypothesis(contracts, hypothesis, trials=256, seed=0, engine=None):
    functions = _fnmap(contracts)
    variables = _statemap(contracts)
    engine = engine or SymbolicEngine(contracts)
    generator = random.Random(seed)

    resolved = [functions.get(name) for name in hypothesis]
    if any(function is None for function in resolved):
        return FuzzResult(list(hypothesis), 0, [], "UNSUPPORTED",
                          assumptions=("one or more functions could not be resolved",))

    effect = engine.execute_sequence(list(hypothesis))
    if effect is None or not effect.expressions:
        return FuzzResult(list(hypothesis), 0, [], "NO_COUNTEREXAMPLE",
                          assumptions=("no symbolic effect available",))

    parameters = {
        name: _params_for_function(function)
        for name, function in zip(hypothesis, resolved)
    }
    guards = tuple(dict.fromkeys(c.render() for c in effect.constraints))[:32]

    assumptions: list[str] = []
    if guards:
        assumptions.append(
            "guards observed but not solved: " + "; ".join(guards[:8])
        )
    assumptions.extend(effect.unsupported)

    if effect.touched:
        assumptions.append(
            f"unknown initial state assumed as {DEFAULT_STATE_VALUE} for "
            + ", ".join(sorted(effect.touched))
        )

    initial_paths = {
        symbol[len(STATE_PREFIX):]: DEFAULT_STATE_VALUE
        for expression in effect.initial.values()
        for symbol in expression.symbols
        if symbol.startswith(STATE_PREFIX)
    }

    traces: list[ConcreteTrace] = []
    for _ in range(max(0, trials)):
        chosen: list[dict[str, int]] = []
        for name in hypothesis:
            chosen.append({
                parameter.name: generator.choice(parameter.values)
                for parameter in parameters.get(name, [])
            })
        mapping = _bindings(hypothesis, chosen, initial_paths)

        before: dict[str, int] = {}
        after: dict[str, int] = {}
        delta: dict[str, int] = {}
        undecided = False
        for base, expression in effect.expressions.items():
            start = _evaluate(effect.initial.get(base, SymExpr.zero()), mapping)
            change = _evaluate(expression, mapping)
            if start is None or change is None:
                undecided = True
                continue
            before[base] = start
            delta[base] = change
            after[base] = start + change
        if undecided and not delta:
            continue

        violations = _invariants(before, after, variables)
        if violations:
            traces.append(ConcreteTrace(
                list(hypothesis), chosen, before, after, delta, violations,
                not guards, seed, tuple(dict.fromkeys(assumptions)),
            ))
            if len(traces) >= 8:
                break

    return FuzzResult(
        list(hypothesis), trials, traces,
        "COUNTEREXAMPLE" if traces else "NO_COUNTEREXAMPLE",
        assumptions=tuple(dict.fromkeys(assumptions)),
        parameters={
            name: [f"{p.name}:{p.type_name}" for p in values]
            for name, values in parameters.items() if values
        },
    )


def _invariants(before, after, variables) -> list[str]:
    """Candidate invariants only. Never sufficient for a confirmed finding."""
    violations: list[str] = []
    for name, value in sorted(after.items()):
        if value < 0 and _is_unsigned(variables.get(name)):
            violations.append(f"negative-accounting-state:{name}")
    assets = after.get("totalAssets")
    supply = after.get("totalSupply")
    if assets is not None and supply is not None:
        if supply == 0 and assets > 0:
            violations.append("shares-zero-while-assets-positive")
    return violations
