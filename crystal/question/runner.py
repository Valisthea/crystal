"""Running a question through the machinery Crystal already has.

Thin on purpose. This is not a new engine: it validates, checks the question
against the code it is about, plans, lets the question steer the one budget
Crystal rations, and hands back the result with every decision attached.

What it refuses is more interesting than what it runs. Each refusal separates
two outcomes a caller must never confuse — §14, "analysis not run" versus
"analysis found nothing":

* **REFUSED_INVALID** — the question is malformed. A validator whose verdict
  can be ignored is documentation.
* **REFUSED_NO_SOURCES** — there is nothing at the path to analyse. Build 020
  reported this as `EXECUTED` with every count at zero, which reads exactly
  like a clean target.
* **REFUSED_UNRESOLVED_SURFACE** — nothing the question names exists in this
  code. Running anyway answers a question about the rest of the target and
  files it under this one.
* **REFUSED_UNPLANNABLE** — no strategy can answer it here; the obstructions
  come back instead of a result.

Statuses are local to a run. There is no COMPLETE and no CONFIRMED here.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .model import ResearchQuestion
from .steering import Steering, steer
from .strategy import StrategyPlan, plan as plan_for
from .surface import SurfaceResolution, research_contracts, resolve
from .validation import ValidationResult, validate

EXECUTED = "EXECUTED"
REFUSED_INVALID = "REFUSED_INVALID"
REFUSED_NO_SOURCES = "REFUSED_NO_SOURCES"
REFUSED_UNRESOLVED_SURFACE = "REFUSED_UNRESOLVED_SURFACE"
REFUSED_UNPLANNABLE = "REFUSED_UNPLANNABLE"


@dataclass
class QuestionRun:
    """What happened when this question met Crystal."""

    question_id: str
    status: str
    validation: ValidationResult
    plan: StrategyPlan | None = None
    result: dict | None = None
    surface: SurfaceResolution | None = None
    steering: Steering | None = None
    # What the question actually changed about the run, and what it did not.
    narrowed: dict = field(default_factory=dict)
    unapplied: list[str] = field(default_factory=list)
    # Outputs that touch the question's surface, counted separately from the
    # rest. Reported, never filtered: a finding one call away from the surface
    # is still a finding.
    on_surface: dict = field(default_factory=dict)
    reason: str = ""

    @property
    def executed(self) -> bool:
        return self.status == EXECUTED

    def report(self) -> dict:
        return {
            "question_id": self.question_id,
            "status": self.status,
            "reason": self.reason,
            "validation": self.validation.as_dict(),
            "plan": self.plan.as_dict() if self.plan else None,
            "surface": self.surface.as_dict() if self.surface else None,
            "steering": self.steering.as_dict() if self.steering else None,
            "narrowed": dict(self.narrowed),
            "unapplied": list(self.unapplied),
            "on_surface": dict(self.on_surface),
        }


def _partition(result, functions) -> dict:
    """How much of what came out touches the surface, and how much does not."""
    if not functions:
        return {}
    candidates = result.get("research_candidates", [])
    detectors = result.get("detectors", [])
    anomalies = result.get("delta_anomalies", [])
    on_candidates = sum(1 for c in candidates if functions & set(c.path))
    on_signals = sum(
        1 for s in detectors if f"{s.contract}.{s.function}" in functions
    )
    on_anomalies = sum(1 for a in anomalies if functions & set(a.sequence))
    return {
        "research_candidates": {"on_surface": on_candidates,
                                "elsewhere": len(candidates) - on_candidates},
        "detector_signals": {"on_surface": on_signals,
                             "elsewhere": len(detectors) - on_signals},
        "delta_anomalies": {"on_surface": on_anomalies,
                            "elsewhere": len(anomalies) - on_anomalies},
    }


def execute(question: ResearchQuestion, project, *, backends=None, **overrides):
    """Validate, check against the code, plan, and run — or refuse, with the reason.

    `project` is passed separately because the question's `target` is an
    identity, not a path Crystal is entitled to resolve on its own. A caller
    knows where the target is checked out; the question knows which target it is.
    """
    from ..engine import select_sources
    from ..parsers import parse_project

    verdict = validate(question)
    if not verdict.ok:
        return QuestionRun(question.question_id, REFUSED_INVALID, verdict,
                           reason="the question is malformed")

    excluded = tuple(question.constraints.get("excluded_paths") or ())
    sources = select_sources(project, overrides.get("languages"), excluded)
    if not sources:
        return QuestionRun(
            question.question_id, REFUSED_NO_SOURCES, verdict,
            reason="no analysable source at the given path; reporting zero "
                   "findings here would read like a clean target",
        )

    # Parsed once here, cheaply (0.2–0.3 s on the two reference protocols), so
    # a question about code that does not exist is refused before the
    # expensive analysis rather than after it.
    contracts = research_contracts(parse_project(sources).contracts)
    resolution = resolve(question.affected_surface, contracts)
    if question.affected_surface and not resolution.any_resolved:
        return QuestionRun(
            question.question_id, REFUSED_UNRESOLVED_SURFACE, verdict,
            surface=resolution,
            reason="nothing the question names exists in the analysed code; "
                   "running anyway would answer a question about the rest of "
                   "the target",
        )

    planned = plan_for(question, backends=backends)
    if not planned.plannable:
        return QuestionRun(question.question_id, REFUSED_UNPLANNABLE, verdict,
                           plan=planned, surface=resolution,
                           reason="no strategy can answer this question here")

    steering = steer(question, resolution, contracts)
    selected = {record.strategy for record in planned.selected}
    narrowed = {"symbolic_budget": planned.symbolic_budget}
    unapplied = []

    if excluded:
        narrowed["excluded_paths"] = list(excluded)
    if steering.order:
        narrowed["symbolic_focus"] = [label for label, _ in steering.order]
    for item in resolution.unresolved:
        unapplied.append(
            f"surface {item['kind']}:{item['identifier']} does not exist in the "
            f"analysed code"
        )
    for item in resolution.unsupported:
        unapplied.append(
            f"surface {item['kind']}:{item['identifier']}: {item['reason']}"
        )
    if question.constraints.get("allowed_paths"):
        # Discovery has no allowlist today. Naming it beats applying half of it.
        unapplied.append(
            "constraint 'allowed_paths': discovery filters exclusions only"
        )
    if "timeout_seconds" in question.constraints:
        unapplied.append(
            "constraint 'timeout_seconds': applies to backends, and none were "
            "launched on this path"
        )

    result = research(
        project,
        use_foundry="runtime-harness" in selected or "property-fuzz" in selected,
        # Passed in, never set on the result afterwards: a limit applied after
        # the run is a limit nothing read.
        sequence_budget=planned.symbolic_budget,
        excluded_paths=excluded,
        focus=steering.order or None,
        **overrides,
    )

    run = QuestionRun(
        question.question_id, EXECUTED, verdict, plan=planned, result=result,
        surface=resolution, steering=steering, narrowed=narrowed,
        unapplied=unapplied, on_surface=_partition(result, resolution.functions),
    )
    result["research_question"] = run.report()
    return run


def research(*args, **kwargs):
    """Indirection so a test can observe or refuse the call into the engine."""
    from ..engine import research as engine_research
    return engine_research(*args, **kwargs)
