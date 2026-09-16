"""Is this question well formed, and can Crystal act on it as written?

The validator answers exactly that and nothing adjacent. It does **not** decide
whether a question is interesting, severe, worth resources or likely to find
anything — §27 and §28. A validator that started grading questions would be
making the global research judgement Crystal is forbidden to hold, in the one
place a caller would least expect to find it.

Two failures matter more than the rest, because both are silent by nature:

* a **constraint or budget dimension Crystal cannot enforce**. Accepting one
  produces a result that appears to have honoured a bound it never applied;
* a **missing snapshot**. Filling it from the current checkout answers a
  question about a world the asker never described.

Both are errors here, never defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .capabilities import unknown as unknown_capabilities
from .model import (
    DEPTHS,
    ENFORCEABLE_BUDGET,
    ENFORCEABLE_CONSTRAINTS,
    EXPECTED_OUTPUTS,
    POLARITIES,
    SCHEMA_MAJOR,
    SURFACE_KINDS,
    ResearchQuestion,
)

# Errors refuse the question. Warnings record something a caller should know
# about a question Crystal will still act on.
ERROR = "error"
WARNING = "warning"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    message: str

    def as_dict(self) -> dict:
        return {"severity": self.severity, "code": self.code,
                "message": self.message}


@dataclass
class ValidationResult:
    findings: list[Finding] = field(default_factory=list)

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.findings if f.severity == WARNING]

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> list[str]:
        return [f.code for f in self.findings]

    def as_dict(self) -> dict:
        return {
            "valid": self.ok,
            "findings": [f.as_dict() for f in self.findings],
        }


class UnsupportedSchema(Exception):
    """A MAJOR this build does not understand. Never downgraded to a warning.

    A MINOR it has not seen is fine — that is what MINOR means. A MAJOR it has
    not seen changes what a field means, and reading it under the old meaning
    is how a question quietly becomes a different question.
    """


def validate(question: ResearchQuestion) -> ValidationResult:
    result = ValidationResult()

    def error(code, message):
        result.findings.append(Finding(ERROR, code, message))

    def warn(code, message):
        result.findings.append(Finding(WARNING, code, message))

    # -- schema ---------------------------------------------------------------
    major = question.schema_major
    if major < 0:
        error("schema.malformed",
              f"schema_version {question.schema_version!r} is not MAJOR.MINOR")
    elif major != SCHEMA_MAJOR:
        error("schema.unknown_major",
              f"schema major {major} is not understood by this build "
              f"(supports {SCHEMA_MAJOR}.x); refusing rather than guessing "
              f"what its fields mean")

    # -- the question itself --------------------------------------------------
    if not question.question or not question.question.strip():
        error("question.empty", "a question is required")
    elif len(question.question.strip()) < 12:
        warn("question.terse",
             "the question is very short; a question Crystal can act on names "
             "what must be determined, not an action to perform")

    # -- target ---------------------------------------------------------------
    if not question.target or not question.target.target_id.strip():
        error("target.missing",
              "target_id is required; Crystal does not derive an identity from "
              "a path basename")

    # -- the world ------------------------------------------------------------
    snapshot = question.source_snapshot
    if snapshot is None or not snapshot.identifies_a_world:
        error("snapshot.missing",
              "source_snapshot must identify a world (revision or tree_digest), "
              "or state AT_EXECUTION explicitly; the current checkout is never "
              "substituted for a snapshot that was not given")
    elif snapshot.resolve_at_execution:
        warn("snapshot.deferred",
             "the world will be resolved when the question runs; the result is "
             "reproducible only against the snapshot recorded in its answer")

    # -- surface --------------------------------------------------------------
    for surface in question.affected_surface:
        if surface.kind not in SURFACE_KINDS:
            error("surface.unknown_kind",
                  f"surface kind {surface.kind!r} is not one Crystal resolves "
                  f"({', '.join(sorted(SURFACE_KINDS))})")
        if not surface.identifier or not surface.identifier.strip():
            error("surface.unidentified",
                  f"a {surface.kind} surface needs a resolvable identifier")
    if not question.affected_surface and not question.discover_surface:
        error("surface.empty",
              "no surface was given and discovery was not requested; an empty "
              "surface never means the whole project")

    # -- constraints ----------------------------------------------------------
    for name in sorted(question.constraints):
        if name not in ENFORCEABLE_CONSTRAINTS:
            error("constraint.unenforceable",
                  f"constraint {name!r} is not one Crystal can apply; a "
                  f"constraint accepted and ignored would make the result look "
                  f"bounded when it was not "
                  f"(enforceable: {', '.join(sorted(ENFORCEABLE_CONSTRAINTS))})")
    for name in ("max_sequence_length", "symbolic_budget", "timeout_seconds"):
        value = question.constraints.get(name)
        if value is not None and (not isinstance(value, int) or value < 0):
            error("constraint.invalid_value",
                  f"constraint {name} must be a non-negative integer, got {value!r}")

    # -- budget ---------------------------------------------------------------
    for name, value in sorted(question.budget.items()):
        if name not in ENFORCEABLE_BUDGET:
            error("budget.unenforceable",
                  f"budget dimension {name!r} is not one Crystal controls "
                  f"(controls: {', '.join(sorted(ENFORCEABLE_BUDGET))})")
        elif not isinstance(value, int) or value < 0:
            error("budget.invalid_value",
                  f"budget {name} must be a non-negative integer, got {value!r}")

    # -- capabilities ---------------------------------------------------------
    missing = unknown_capabilities(question.required_capabilities)
    if missing:
        error("capability.unknown",
              f"no provider for {', '.join(missing)}; Crystal will not accept a "
              f"capability it cannot exercise, because the answer would address "
              f"a different question from the one asked")

    # -- depth ----------------------------------------------------------------
    if question.depth not in DEPTHS:
        error("depth.unknown",
              f"depth {question.depth!r} is not one of "
              f"{', '.join(sorted(DEPTHS))}")

    # -- expected output ------------------------------------------------------
    for wanted in question.expected_output:
        if wanted not in EXPECTED_OUTPUTS:
            error("output.unknown",
                  f"expected_output {wanted!r} is not a shape Crystal produces "
                  f"({', '.join(sorted(EXPECTED_OUTPUTS))})")

    # -- prior evidence -------------------------------------------------------
    seen = set()
    for item in question.prior_evidence:
        if not item.evidence_id:
            error("evidence.unidentified",
                  "prior evidence needs an evidence_id to be referenced")
        elif item.evidence_id in seen:
            error("evidence.duplicate",
                  f"prior evidence {item.evidence_id!r} appears twice")
        seen.add(item.evidence_id)
        if item.polarity not in POLARITIES:
            error("evidence.invalid_polarity",
                  f"polarity {item.polarity!r} is not one of "
                  f"{', '.join(sorted(POLARITIES))}")
        if not item.sufficiently_provenanced:
            warn("evidence.unattributable",
                 f"prior evidence {item.evidence_id!r} carries no producer or "
                 f"source; it may steer strategy but stays marked, because a "
                 f"step skipped on an unattributable claim is a gap nobody can "
                 f"audit later")

    return result


def parse_guard(schema_version: str) -> None:
    """Refuse a MAJOR this build cannot read, before anything is interpreted."""
    try:
        major = int(str(schema_version).split(".", 1)[0])
    except (TypeError, ValueError) as exc:
        raise UnsupportedSchema(
            f"schema_version {schema_version!r} is not MAJOR.MINOR"
        ) from exc
    if major != SCHEMA_MAJOR:
        raise UnsupportedSchema(
            f"schema major {major} is not understood by this build "
            f"(supports {SCHEMA_MAJOR}.x)"
        )
