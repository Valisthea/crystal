"""Output-contract validation.

v1 shipped the rule list without checking it. v2 evaluates every rule against
the actual result and reports violations, so a regression in the evidence-only
contract fails loudly instead of silently shipping.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QualityRule:
    name: str
    description: str


@dataclass(frozen=True)
class QualityReport:
    passed: bool
    checks: dict[str, bool] = field(default_factory=dict)
    violations: tuple[str, ...] = ()
    rules: tuple[str, ...] = ()


RULES = [
    QualityRule("no-confirmed-findings", "Heuristics remain research candidates until concrete validation."),
    QualityRule("deterministic-id", "Equivalent candidates receive the same stable identifier."),
    QualityRule("deduplicate", "Repeated semantic signals are collapsed before triage."),
    QualityRule("provenance", "Every candidate records the subsystem that produced it."),
    QualityRule("confidence-bounded", "Confidence is normalized to [0,1]."),
    QualityRule("detectors-are-evidence", "Detector signals are research status, never findings."),
    QualityRule("evidence-schema", "Evidence records carry a schema version and provenance."),
    QualityRule("unsupported-over-guess", "Unmodelled behaviour is reported, not approximated."),
]


def rules():
    return RULES


def validate(result) -> QualityReport:
    candidates = result.get("research_candidates", []) or []
    detectors = result.get("detectors", []) or []
    evidence = result.get("evidence_records", []) or []
    gate = result.get("finding_gate", {}) or {}
    decisions = gate.get("decisions", []) or []

    checks: dict[str, bool] = {
        "no-confirmed-findings": all(
            (decision.get("status") if isinstance(decision, dict) else
             getattr(decision, "status", "")) != "CONFIRMED"
            for decision in decisions
        ),
        "deterministic-id": all(getattr(x, "id", "") for x in candidates),
        "deduplicate": len({getattr(x, "id", "") for x in candidates}) == len(candidates),
        "provenance": all(getattr(x, "source", "") for x in candidates),
        "confidence-bounded": all(
            0.0 <= getattr(x, "confidence", 0.0) <= 1.0 for x in candidates
        ),
        "detectors-are-evidence": all(
            getattr(x, "status", "") == "RESEARCH" for x in detectors
        ),
        "evidence-schema": all(
            getattr(x, "schema_version", "") and getattr(x, "provenance", None)
            is not None for x in evidence
        ),
        "unsupported-over-guess": all(
            isinstance(getattr(x, "unsupported", ()), (tuple, list))
            for x in result.get("state_deltas", []) or []
        ),
    }

    violations = tuple(
        f"{name}: {rule.description}"
        for rule in RULES
        for name in [rule.name]
        if checks.get(name) is False
    )
    return QualityReport(
        passed=not violations,
        checks=checks,
        violations=violations,
        rules=tuple(rule.name for rule in RULES),
    )
