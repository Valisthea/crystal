"""From a question to the strategies Crystal would run for it.

This is the first consumer of the contract, and its job is §20: a question
selects strategies, rather than starting a fixed pipeline that would have run
the same way whatever was asked.

Three things it deliberately does **not** do:

* **It does not invent an information gain.** Where nothing measures the value
  of running a strategy, the record says so. A number stands in for a
  measurement, and a made-up one is worse than a gap because it will be summed,
  sorted and believed.
* **It does not always select something.** "No strategy is available for this
  question" is a real answer, given with the reason. §23 — a planner that must
  return a plan will return a bad one.
* **It does not re-plan.** Adaptive depth is in the schema and is not built, so
  a question asking for it is planned as an obstruction rather than quietly
  downgraded to `standard`. Silently answering an easier question is the
  failure this whole contract exists to prevent.

The trace is the deliverable as much as the selection: every strategy considered
appears, with why it was or was not chosen, so an executed analysis can be
attributed to the question that asked for it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .capabilities import availability
from .model import DEPTHS, ResearchQuestion

# What each depth permits, in capabilities. The single place a named posture
# becomes a knob — see `DEPTHS` for what each one promises a caller.
DEPTH_CAPABILITIES = {
    "shallow": ("static", "dataflow"),
    "standard": ("static", "dataflow", "symbolic", "differential", "composition"),
    "deep": ("static", "dataflow", "symbolic", "differential", "composition",
             "fuzzing", "runtime"),
    "adaptive": (),
}

# The symbolic sequence budget each posture spends, before any constraint or
# budget in the question narrows it. `standard` is Build 018's default.
DEPTH_SYMBOLIC_BUDGET = {"shallow": 0, "standard": 150, "deep": 300, "adaptive": 0}

# Crystal's real strategies, each with the capability it needs. A strategy is
# named for the analysis it performs, never for the tool that performs it.
STRATEGIES = {
    "detector-pass": ("static", "run the detector suite over the parsed IR"),
    "dataflow-trace": ("dataflow", "follow value, taint and influence to the surface"),
    "symbolic-sequence": ("symbolic", "execute sequence hypotheses as canonical state deltas"),
    "differential-compare": ("differential", "compare behaviour across comparable functions"),
    "composition-chain": ("composition", "compose multi-step and cross-contract chains"),
    "property-fuzz": ("fuzzing", "fuzz a derived property for a counterexample witness"),
    "runtime-harness": ("runtime", "execute a generated harness on a concrete EVM"),
}

# Stated where a caller would otherwise read a zero as a measurement.
UNMEASURED = "not measured"


@dataclass(frozen=True)
class StrategyRecord:
    question_id: str
    strategy: str
    reason: str
    required_capabilities: tuple[str, ...]
    selected: bool
    estimated_cost: str = UNMEASURED
    expected_information_gain: str = UNMEASURED

    def as_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "strategy": self.strategy,
            "reason": self.reason,
            "required_capabilities": list(self.required_capabilities),
            "selected": self.selected,
            "estimated_cost": self.estimated_cost,
            "expected_information_gain": self.expected_information_gain,
        }


@dataclass
class StrategyPlan:
    """Considered, selected, and why — enough to reconstruct the decision."""

    question_id: str
    considered: list[StrategyRecord] = field(default_factory=list)
    obstructions: list[str] = field(default_factory=list)
    symbolic_budget: int = 0
    depth: str = "standard"
    # What the caller already knew, carried forward. It does not yet change
    # which strategies are selected — that is adaptive selection, and it is not
    # built. Carrying it is the part that can be checked now: evidence handed in
    # and then dropped on the floor is the failure mode, and a plan that cannot
    # show what it was given cannot be audited for it.
    prior_evidence: list[dict] = field(default_factory=list)

    @property
    def selected(self) -> list[StrategyRecord]:
        return [record for record in self.considered if record.selected]

    @property
    def plannable(self) -> bool:
        return bool(self.selected)

    def as_dict(self) -> dict:
        return {
            "question_id": self.question_id,
            "depth": self.depth,
            "depth_means": DEPTHS.get(self.depth, ""),
            "symbolic_budget": self.symbolic_budget,
            "considered": [record.as_dict() for record in self.considered],
            "selected": [record.strategy for record in self.selected],
            "obstructions": list(self.obstructions),
            "prior_evidence_considered": list(self.prior_evidence),
            "prior_evidence_note": (
                "carried from the question and recorded; it does not yet "
                "narrow selection, which is adaptive strategy and is not built"
            ),
            "information_gain": (
                "not modelled in this build; every record reports "
                f"{UNMEASURED!r} rather than a number nothing measured"
            ),
        }


def _budget_for(question: ResearchQuestion) -> int:
    """The narrowest of what the depth permits and what the caller allowed.

    Narrowest, never widest: a caller's budget is a ceiling, and a depth that
    wanted more does not get to raise it.
    """
    allowed = [DEPTH_SYMBOLIC_BUDGET.get(question.depth, 0)]
    if "symbolic_budget" in question.constraints:
        allowed.append(int(question.constraints["symbolic_budget"]))
    if "symbolic_sequences" in question.budget:
        allowed.append(int(question.budget["symbolic_sequences"]))
    return min(allowed)


def plan(question: ResearchQuestion, backends=None) -> StrategyPlan:
    """Which strategies this question selects, and what stands in the way."""
    plan = StrategyPlan(
        question_id=question.question_id,
        depth=question.depth,
        symbolic_budget=_budget_for(question),
        prior_evidence=[
            {
                "evidence_id": item.evidence_id,
                "polarity": item.polarity,
                "attributable": item.sufficiently_provenanced,
            }
            for item in question.prior_evidence
        ],
    )
    usable = availability(backends)

    if question.depth == "adaptive":
        plan.obstructions.append(
            "depth 'adaptive' is declared by the schema and not implemented in "
            "this build; planning it as 'standard' would answer an easier "
            "question than the one asked"
        )
        return plan

    permitted = set(DEPTH_CAPABILITIES.get(question.depth, ()))
    requested = set(question.required_capabilities)

    for capability in sorted(requested - permitted):
        plan.obstructions.append(
            f"capability '{capability}' was required but depth "
            f"'{question.depth}' does not permit it"
        )

    for name, (capability, summary) in sorted(STRATEGIES.items()):
        entry = usable.get(capability, {})
        if capability not in permitted:
            plan.considered.append(StrategyRecord(
                plan.question_id, name,
                f"depth '{question.depth}' does not permit {capability}",
                (capability,), False,
            ))
            continue
        if requested and capability not in requested:
            plan.considered.append(StrategyRecord(
                plan.question_id, name,
                f"{capability} was not among the required capabilities",
                (capability,), False,
            ))
            continue
        if not entry.get("available", False):
            reason = "; ".join(entry.get("unavailable", ())) or "no provider"
            plan.considered.append(StrategyRecord(
                plan.question_id, name,
                f"{capability} unavailable here: {reason}",
                (capability,), False,
            ))
            plan.obstructions.append(f"{name}: {capability} unavailable ({reason})")
            continue
        if name == "symbolic-sequence" and plan.symbolic_budget == 0:
            plan.considered.append(StrategyRecord(
                plan.question_id, name,
                "the symbolic budget for this question is 0",
                (capability,), False,
            ))
            continue
        plan.considered.append(StrategyRecord(
            plan.question_id, name, summary, (capability,), True,
        ))

    if not plan.selected and not plan.obstructions:
        plan.obstructions.append(
            "no strategy Crystal has matches this question at this depth"
        )
    return plan
