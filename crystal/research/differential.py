"""Differential candidates derived from symbolic effects.

v1 compared function *names* against a hardcoded list of inverse pairs. v2
compares the symbolic state deltas: two entry points are an inverse pair when
their normalized deltas cancel, whatever they happen to be called. Name
matching is kept only as a weak corroborating signal.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..symbolic import SymbolicEngine
from ..symbolic.algebra import ARG_PREFIX, SymExpr

CANONICAL_ARGUMENT = SymExpr.symbol("ARG:*")

KNOWN_INVERSE_NAMES = {
    frozenset({"deposit", "withdraw"}),
    frozenset({"mint", "burn"}),
    frozenset({"borrow", "repay"}),
    frozenset({"stake", "unstake"}),
    frozenset({"lock", "unlock"}),
    frozenset({"add", "remove"}),
    frozenset({"supply", "redeem"}),
}


@dataclass(frozen=True)
class DifferentialCandidate:
    path_a: list[str]
    path_b: list[str]
    shared_state: list[str]
    reason: str
    confidence: float
    causal_state: list[str]
    relation: str = "shared-state"
    delta_a: dict[str, str] | None = None
    delta_b: dict[str, str] | None = None
    residual: dict[str, str] | None = None


def _normalize(expressions: dict[str, SymExpr]) -> dict[str, SymExpr]:
    """Collapse every attacker-controlled input to one symbol.

    `deposit(msg.value)` and `withdraw(x)` then become comparable: the question
    is whether the *shape* of the effect cancels, not whether the parameters
    happen to share a name.
    """
    normalized: dict[str, SymExpr] = {}
    for name, expression in expressions.items():
        mapping = {
            symbol: CANONICAL_ARGUMENT
            for symbol in expression.symbols if symbol.startswith(ARG_PREFIX)
        }
        normalized[name] = expression.substitute(mapping)
    return normalized


def _residual(left: dict[str, SymExpr], right: dict[str, SymExpr]):
    keys = sorted(set(left) | set(right))
    return {
        key: left.get(key, SymExpr.zero()) + right.get(key, SymExpr.zero())
        for key in keys
    }


def generate_differential_candidates(contracts, state_deltas=None, engine=None):
    engine = engine or SymbolicEngine(contracts)
    out: list[DifferentialCandidate] = []

    for contract in contracts:
        functions = [
            f for f in contract.functions
            if f.visibility in {"public", "external"}
        ]
        effects = {}
        for function in functions:
            try:
                effects[function.name] = engine.execute_function(function)
            except RecursionError:
                continue

        for index, left in enumerate(functions):
            for right in functions[index + 1:]:
                shared = sorted(
                    (set(left.reads) | set(left.writes))
                    & (set(right.reads) | set(right.writes))
                )
                if not shared:
                    continue
                causal = sorted(set(left.writes) & set(right.reads))
                names = frozenset({left.name.lower(), right.name.lower()})
                known_names = names in KNOWN_INVERSE_NAMES

                left_effect = effects.get(left.name)
                right_effect = effects.get(right.name)
                relation = "shared-state"
                residual = None
                confidence = 0.48
                reason = ("shared-state operations may exhibit path-dependent "
                          "behavior")

                if left_effect is not None and right_effect is not None:
                    left_normalized = _normalize(left_effect.expressions)
                    right_normalized = _normalize(right_effect.expressions)
                    residual_expressions = _residual(left_normalized, right_normalized)
                    residual = {
                        key: value.render()
                        for key, value in residual_expressions.items()
                    }
                    changed = [
                        key for key, value in residual_expressions.items()
                        if not value.is_zero
                    ]
                    non_trivial = any(
                        not value.is_zero for value in left_normalized.values()
                    ) or any(
                        not value.is_zero for value in right_normalized.values()
                    )
                    if non_trivial and not changed:
                        relation = "symbolic-inverse"
                        confidence = 0.82
                        reason = (
                            "normalized state deltas cancel exactly; the pair "
                            "should conserve every shared accounting variable"
                        )
                    elif non_trivial and len(changed) < len(residual_expressions):
                        relation = "partial-inverse"
                        confidence = 0.71
                        reason = (
                            "deltas cancel on "
                            f"{len(residual_expressions) - len(changed)} of "
                            f"{len(residual_expressions)} shared variables; "
                            f"residual on {', '.join(sorted(changed))}"
                        )
                    elif causal:
                        relation = "producer-consumer"
                        confidence = 0.67
                        reason = ("first operation produces state consumed by "
                                  "the second")
                elif causal:
                    relation = "producer-consumer"
                    confidence = 0.67
                    reason = "first operation produces state consumed by the second"

                if known_names:
                    confidence = min(0.94, confidence + 0.06)
                    reason += "; function names also match a known inverse pair"

                out.append(DifferentialCandidate(
                    [f"{contract.name}.{left.name}"],
                    [f"{contract.name}.{right.name}"],
                    shared, reason, round(confidence, 3), causal, relation,
                    dict(left_effect.deltas) if left_effect else None,
                    dict(right_effect.deltas) if right_effect else None,
                    residual,
                ))

    return sorted(out, key=lambda x: (-x.confidence, x.path_a, x.path_b))[:500]
