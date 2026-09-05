"""Differential candidates derived from symbolic effects.

v1 compared function *names* against a hardcoded list of inverse pairs. v2
compares the symbolic state deltas: two entry points are an inverse pair when
their normalized deltas cancel, whatever they happen to be called. Name
matching is kept only as a weak corroborating signal.

v3 adds sequence-level comparison and enriched difference classification.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..naming import bare_name
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

# Comparison verdicts.
SAME_STATE = "SAME_STATE"
DIFFERENT_STATE = "DIFFERENT_STATE"
PARTIALLY_DIFFERENT = "PARTIALLY_DIFFERENT"
UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class StateDifference:
    """Precise difference between two state snapshots."""
    variable: str
    category: str
    delta_a: str
    delta_b: str


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
    verdict: str = UNKNOWN
    differences: tuple[StateDifference, ...] = ()


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

    # Add sequence-level comparisons from state deltas.
    if state_deltas:
        out.extend(_sequence_differentials(state_deltas))

    return sorted(out, key=lambda x: (-x.confidence, x.path_a, x.path_b))[:500]


# ── State category classification ──────────────────────────────────────

_CATEGORY_HINTS = {
    "balance": {"balance", "balances", "totalsupply", "totalassets", "reserve"},
    "ownership": {"owner", "ownerof", "admin", "operator"},
    "role": {"role", "roles", "hasrole", "minter", "pauser"},
    "nonce": {"nonce", "nonces", "counter"},
    "temporal": {"expiry", "deadline", "validuntil", "timestamp"},
    "approval": {"allowance", "approved", "approval"},
    "resolver": {"resolver", "records", "registry"},
    "implementation": {"implementation", "proxy", "beacon"},
}


def _categorize(name: str) -> str:
    # Category is about meaning, so the contract namespace comes off first.
    lower = bare_name(name).lower().replace("_", "")
    for category, hints in _CATEGORY_HINTS.items():
        if lower in hints or any(h in lower for h in hints):
            return category
    return "storage"


def _classify_differences(
    residual: dict[str, str] | None,
) -> tuple[str, tuple[StateDifference, ...]]:
    """Classify the verdict and produce per-variable differences."""
    if residual is None:
        return UNKNOWN, ()

    diffs: list[StateDifference] = []
    for var, expr in sorted(residual.items()):
        if expr not in ("0", "0.0", ""):
            diffs.append(StateDifference(
                variable=var,
                category=_categorize(var),
                delta_a=expr,
                delta_b="0",
            ))

    if not diffs:
        return SAME_STATE, ()
    if len(diffs) < len(residual):
        return PARTIALLY_DIFFERENT, tuple(diffs)
    return DIFFERENT_STATE, tuple(diffs)


def _sequence_differentials(
    state_deltas,
) -> list[DifferentialCandidate]:
    """Compare sequences that share functions but differ in order or composition."""
    from .statedelta import compare_deltas

    out: list[DifferentialCandidate] = []
    seen: set[frozenset] = set()

    for i, delta_a in enumerate(state_deltas):
        for delta_b in state_deltas[i + 1:]:
            funcs_a = frozenset(delta_a.sequence)
            funcs_b = frozenset(delta_b.sequence)
            if funcs_a != funcs_b:
                continue
            if tuple(delta_a.sequence) == tuple(delta_b.sequence):
                continue
            pair_key = frozenset({
                tuple(delta_a.sequence), tuple(delta_b.sequence),
            })
            if pair_key in seen:
                continue
            seen.add(pair_key)

            residual = compare_deltas(delta_a, delta_b)
            changed = [k for k, v in residual.items() if v != "0"]
            if not changed:
                continue

            shared = sorted(set(delta_a.touched) & set(delta_b.touched))
            diffs = tuple(
                StateDifference(
                    variable=k,
                    category=_categorize(k),
                    delta_a=delta_a.delta.get(k, "0"),
                    delta_b=delta_b.delta.get(k, "0"),
                )
                for k in changed
            )

            out.append(DifferentialCandidate(
                path_a=list(delta_a.sequence),
                path_b=list(delta_b.sequence),
                shared_state=shared,
                reason=f"same functions in different order produce different "
                       f"state on {', '.join(changed)}",
                confidence=round(min(0.80, 0.55 + 0.07 * len(changed)), 3),
                causal_state=changed,
                relation="order-dependent",
                delta_a=delta_a.delta,
                delta_b=delta_b.delta,
                residual=residual,
                verdict=DIFFERENT_STATE if changed else SAME_STATE,
                differences=diffs,
            ))

    return out
