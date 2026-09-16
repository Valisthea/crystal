"""Running a question through the machinery Crystal already has.

Thin on purpose. This is not a new engine and not the adaptive planner: it
validates, plans, maps the plan onto the existing pipeline, and hands back the
result with the plan attached so every output is attributable to the question
that asked for it.

What it refuses is more interesting than what it runs:

* an **invalid** question does not execute. A validator whose verdict can be
  ignored is documentation.
* an **unplannable** question does not execute either, and the obstructions come
  back instead of a result. §26 — "blocked" and "found nothing" must never be
  the same answer, because only one of them means the analysis happened.

The mapping is deliberately partial and says so. `affected_surface` does not yet
narrow the analysis: Crystal discovers its own surface, and pretending a
question scoped it would be the silent substitution this contract exists to
prevent. `narrowed` names exactly what was applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import ResearchQuestion
from .strategy import StrategyPlan, plan as plan_for
from .validation import ValidationResult, validate

# Statuses. Local to a run, never a global research state: there is no
# COMPLETE and no CONFIRMED here, and there will not be.
EXECUTED = "EXECUTED"
REFUSED_INVALID = "REFUSED_INVALID"
REFUSED_UNPLANNABLE = "REFUSED_UNPLANNABLE"


@dataclass
class QuestionRun:
    """What happened when this question met Crystal."""

    question_id: str
    status: str
    validation: ValidationResult
    plan: StrategyPlan | None = None
    result: dict | None = None
    # What the question actually changed about the run, and what it did not.
    narrowed: dict = field(default_factory=dict)
    unapplied: list[str] = field(default_factory=list)

    @property
    def executed(self) -> bool:
        return self.status == EXECUTED

    def report(self) -> dict:
        return {
            "question_id": self.question_id,
            "status": self.status,
            "validation": self.validation.as_dict(),
            "plan": self.plan.as_dict() if self.plan else None,
            "narrowed": dict(self.narrowed),
            "unapplied": list(self.unapplied),
        }


def execute(question: ResearchQuestion, project, *, backends=None, **overrides):
    """Validate, plan, and run — or refuse, with the reason.

    `project` is passed separately because the question's `target` is an
    identity, not a path Crystal is entitled to resolve on its own. A caller
    knows where the target is checked out; the question knows which target it is.
    """
    from ..engine import research

    verdict = validate(question)
    if not verdict.ok:
        return QuestionRun(question.question_id, REFUSED_INVALID, verdict)

    planned = plan_for(question, backends=backends)
    if not planned.plannable:
        return QuestionRun(question.question_id, REFUSED_UNPLANNABLE,
                           verdict, plan=planned)

    selected = {record.strategy for record in planned.selected}
    narrowed = {"symbolic_budget": planned.symbolic_budget}
    unapplied = []

    excluded = tuple(question.constraints.get("excluded_paths") or ())
    if excluded:
        narrowed["excluded_paths"] = list(excluded)
    if question.constraints.get("allowed_paths"):
        # Discovery has no allowlist today. Naming it beats applying half of it.
        unapplied.append(
            "constraint 'allowed_paths': discovery filters exclusions only"
        )
    if question.affected_surface:
        unapplied.append(
            "affected_surface: Crystal discovers its own surface; the question "
            "did not narrow it, and a result that claimed otherwise would be "
            "answering a narrower question than it ran"
        )
    if "timeout_seconds" in question.constraints:
        unapplied.append(
            "constraint 'timeout_seconds': applies to backends, and none were "
            "launched on this path"
        )

    result = research(
        project,
        use_foundry="runtime-harness" in selected or "property-fuzz" in selected,
        # The one knob a question moves today. Passed in, not set on the result
        # afterwards: a limit applied after the run is a limit nothing read.
        sequence_budget=planned.symbolic_budget,
        excluded_paths=excluded,
        **overrides,
    )

    run = QuestionRun(
        question.question_id, EXECUTED, verdict, plan=planned, result=result,
        narrowed=narrowed, unapplied=unapplied,
    )
    result["research_question"] = run.report()
    return run
