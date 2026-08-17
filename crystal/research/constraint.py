"""Constraint derivation for a candidate sequence.

Only constraints that are provable from the parsed source are emitted: the
symbolic deltas themselves, and the guards (`require`, `assert`, branch
conditions, modifiers) that were actually observed on the executed path. No
exploit assumption is ever introduced here.
"""

from __future__ import annotations

from ..symbolic.constraints import ConstraintSystem

# v1 name kept so existing integrations keep importing successfully.
ConstraintSet = ConstraintSystem

SAMPLE_RANGE = (0, 2 ** 16 - 1)


def derive_constraints(hypothesis, state_deltas, engine=None) -> ConstraintSystem:
    expressions: list[str] = []
    ranges: dict[str, tuple[int, int]] = {}
    parameter_types: dict[str, str] = {}
    constraints: tuple = ()
    unsupported: tuple = ()

    for delta in state_deltas:
        if list(delta.sequence) != list(hypothesis):
            continue
        for state, expression in delta.delta.items():
            if expression != "0":
                expressions.append(f"delta({state}) == {expression}")
        expressions.extend(getattr(delta, "guards", ()) or ())
        break

    if engine is not None:
        system = engine.constraint_system(list(hypothesis))
        constraints = system.constraints
        parameter_types = dict(system.parameter_types)
        unsupported = system.unsupported
        expressions.extend(system.expressions)
        for name, domain in system.parameter_ranges.items():
            if name not in hypothesis:
                ranges[name] = domain

    # Sequence-level sampling window stays boundary-first and bounded.
    for name in hypothesis:
        ranges[name] = SAMPLE_RANGE

    return ConstraintSystem(
        tuple(dict.fromkeys(expressions)), ranges, constraints,
        parameter_types, unsupported,
    )
