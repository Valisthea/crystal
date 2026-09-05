"""Symbolic state deltas for candidate transaction sequences.

v1 derived deltas with regexes over the function body and could only express
`+ARG` / `-ARG`. v2 executes the sequence on the symbolic engine, so a delta is
a canonical polynomial: `ARG:msg.value#1 + ARG:msg.value#2 - ARG:x#3`.

The regex reader survives as a fallback for functions without a statement IR.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ..symbolic import SymbolicEngine


@dataclass(frozen=True)
class StateDelta:
    sequence: list[str]
    before: dict[str, str]
    after: dict[str, str]
    delta: dict[str, str]
    touched: list[str]
    changed: list[str]
    confidence: float
    model: str = "symbolic-expression"
    unsupported: tuple[str, ...] = ()
    branch_dependent: bool = False
    guards: tuple[str, ...] = ()


def _fnmap(contracts):
    return {f"{c.name}.{f.name}": f for c in contracts for f in c.functions}


def _regex_effect(function):
    """Fallback used only when a function carries no statement IR."""
    effects: dict[str, str] = {}
    body = function.body or ""
    for state in function.writes:
        plus, minus = [], []
        if re.search(r"\b" + re.escape(state) + r"\s*\+=", body):
            plus.append("ARG")
        if re.search(r"\b" + re.escape(state) + r"\s*-=", body):
            minus.append("ARG")
        if f"{state}++" in body:
            plus.append("1")
        if f"{state}--" in body:
            minus.append("1")
        if plus or minus:
            positive = " + ".join(plus)
            negative = " + ".join(minus)
            if positive and negative:
                effects[state] = f"({positive}) - ({negative})"
            elif positive:
                effects[state] = f"+({positive})"
            else:
                effects[state] = f"-({negative})"
    return effects


def _combine(old: str, new: str) -> str:
    if old in ("0", "+0"):
        return new
    if new in ("0", "+0"):
        return old
    return f"({old}) + ({new})"


def _fallback_delta(functions, sequence, score, namespace):
    touched: set[str] = set()
    delta: dict[str, str] = {}
    for function in functions:
        # Namespaced like the symbolic path, so a fallback delta for a
        # cross-contract sequence does not merge two contracts' variables.
        slot = {
            name: namespace.qualify(function.contract, name)
            for name in set(function.reads) | set(function.writes)
        }
        touched |= set(slot.values())
        for state, expression in _regex_effect(function).items():
            key = slot.get(state, state)
            delta[key] = _combine(delta.get(key, "0"), expression)
    for state in touched:
        delta.setdefault(state, "0")
    before = {state: "0" for state in sorted(touched)}
    after = {
        state: ("0" if value == "0" else f"0 + ({value})")
        for state, value in sorted(delta.items())
    }
    changed = sorted(state for state, value in delta.items() if value != "0")
    return StateDelta(
        list(sequence), before, after, dict(sorted(delta.items())),
        sorted(touched), changed,
        round(min(.94, score + .07 * bool(changed)), 3),
        model="regex-fallback",
        unsupported=("no statement IR available; regex approximation used",),
    )


def derive_state_deltas(contracts, sequence_hypotheses, engine=None, limit=150):
    functions = _fnmap(contracts)
    engine = engine or SymbolicEngine(contracts)
    out: list[StateDelta] = []

    for hypothesis in sequence_hypotheses[:limit]:
        sequence = list(hypothesis.sequence)
        resolved = [functions.get(name) for name in sequence]
        if any(function is None for function in resolved):
            continue

        if all(function.has_ir for function in resolved):
            effect = engine.execute_sequence(sequence)
            if effect is None:
                continue
            changed = list(effect.changed)
            out.append(StateDelta(
                sequence, dict(effect.before), dict(effect.after),
                dict(effect.deltas), list(effect.touched), changed,
                round(min(.94, hypothesis.score + .07 * bool(changed)), 3),
                model=effect.model,
                unsupported=effect.unsupported,
                branch_dependent=effect.branch_dependent,
                guards=tuple(dict.fromkeys(
                    constraint.render() for constraint in effect.constraints
                ))[:32],
            ))
        else:
            out.append(_fallback_delta(
                resolved, sequence, hypothesis.score, engine.namespace,
            ))

    return sorted(out, key=lambda x: (-x.confidence, -len(x.changed), x.sequence))


def compare_deltas(a, b):
    keys = sorted(set(a.delta) | set(b.delta))
    return {
        key: f"({a.delta.get(key, '0')}) - ({b.delta.get(key, '0')})"
        for key in keys
    }
