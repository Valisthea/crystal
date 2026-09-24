"""JSON Schemas for the two documents that cross Crystal's boundary.

A caller in another process — possibly another language — builds a question
document and reads a result envelope. It should be able to do both from these
schemas alone, without reading Crystal's source. `crystal ask --print-schema`
prints them, and `schemas/` in the repository holds copies a test keeps
byte-identical to what is defined here.

The question schema describes what Crystal *reads*: unknown top-level fields
are allowed, because a MINOR extension may add some and Crystal ignores those
it does not know — safely, since the identity check fails loudly if an ignored
field carried meaning. The result schema is closed: an envelope carries exactly
these fields, and a consumer may rely on that.
"""

from __future__ import annotations

from .envelope import EXIT_CODES, RESULT_SCHEMA, STATUSES
from .model import (
    DEPTHS,
    ENFORCEABLE_BUDGET,
    ENFORCEABLE_CONSTRAINTS,
    EXPECTED_OUTPUTS,
    POLARITIES,
    SURFACE_KINDS,
)

DRAFT = "https://json-schema.org/draft/2020-12/schema"

_SURFACE = {
    "type": "object",
    "required": ["kind", "identifier"],
    "properties": {
        "kind": {"enum": sorted(SURFACE_KINDS)},
        "identifier": {"type": "string", "minLength": 1},
    },
    "additionalProperties": False,
}

QUESTION = {
    "$schema": DRAFT,
    "$id": "https://github.com/Valisthea/crystal/schemas/crystal-research-question-1.json",
    "title": "Crystal ResearchQuestion, schema 1.x",
    "description": (
        "A question put to Crystal. question_id is optional on input: Crystal "
        "derives it from the content, and if one is present it must match, or "
        "the document is refused as having drifted in transit."
    ),
    "type": "object",
    "required": ["schema_version", "question", "target", "source_snapshot"],
    "properties": {
        "schema_version": {"type": "string", "pattern": r"^1\.\d+$"},
        "question_id": {"type": "string"},
        "question": {"type": "string", "minLength": 1},
        "hypothesis": {"type": "string"},
        "target": {
            "type": "object",
            "required": ["target_id"],
            "properties": {
                "target_id": {"type": "string", "minLength": 1},
                "project": {"type": "string"},
                "repository": {"type": "string"},
                "path": {"type": "string"},
                "contract": {"type": "string"},
                "deployment": {"type": "string"},
                "chain": {"type": "string"},
            },
        },
        "source_snapshot": {
            "type": "object",
            "description": (
                "Must identify a world: revision or tree_digest. Use revision "
                "'resolve-at-execution' to ask explicitly for the checkout as "
                "found; it is never assumed."
            ),
            "anyOf": [{"required": ["revision"]}, {"required": ["tree_digest"]}],
            "properties": {
                "revision": {"type": "string", "minLength": 1},
                "tree_digest": {"type": "string", "minLength": 1},
                "configuration_digest": {"type": "string"},
                "branch": {"type": "string"},
                "chain": {"type": "string"},
                "deployment": {"type": "string"},
            },
        },
        "affected_surface": {"type": "array", "items": _SURFACE},
        "discover_surface": {"type": "boolean"},
        "constraints": {
            "type": "object",
            "propertyNames": {"enum": sorted(ENFORCEABLE_CONSTRAINTS)},
        },
        "prior_evidence": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["evidence_id", "claim"],
                "properties": {
                    "evidence_id": {"type": "string", "minLength": 1},
                    "claim": {"type": "string"},
                    "polarity": {"enum": sorted(POLARITIES)},
                    "provenance": {"type": "object"},
                    "about": {"type": "array", "items": _SURFACE},
                },
            },
        },
        "required_capabilities": {"type": "array", "items": {"type": "string"}},
        "budget": {
            "type": "object",
            "propertyNames": {"enum": sorted(ENFORCEABLE_BUDGET)},
            "additionalProperties": {"type": "integer", "minimum": 0},
        },
        "depth": {"enum": sorted(DEPTHS)},
        "expected_output": {
            "type": "array", "items": {"enum": sorted(EXPECTED_OUTPUTS)},
        },
        "provenance": {"type": "object"},
    },
    "allOf": [{
        "description": "An empty surface is valid only when discovery is asked for.",
        "anyOf": [
            {"required": ["affected_surface"],
             "properties": {"affected_surface": {"minItems": 1}}},
            {"required": ["discover_surface"],
             "properties": {"discover_surface": {"const": True}}},
        ],
    }],
}

_NULLABLE_OBJECT = {"type": ["object", "null"]}

RESULT = {
    "$schema": DRAFT,
    "$id": "https://github.com/Valisthea/crystal/schemas/crystal-question-result-1.json",
    "title": RESULT_SCHEMA,
    "description": (
        "What Crystal returns for one question. Exactly one envelope per call, "
        "whatever happened. analysis_ran separates 'not run' from 'found "
        "nothing'; evidence is the crystal-arcadia/2.0 payload, present if and "
        "only if the analysis ran."
    ),
    "type": "object",
    "required": [
        "schema_version", "producer", "question_id", "question", "status",
        "exit_code", "analysis_ran", "reason", "validation", "plan", "surface",
        "steering", "narrowed", "unapplied", "on_surface", "evidence",
    ],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"const": RESULT_SCHEMA},
        "producer": {
            "type": "object",
            "required": ["tool", "version", "build", "role", "decides_severity",
                         "decides_submission", "decides_research_state"],
            "properties": {
                "tool": {"const": "crystal"},
                "version": {"type": "string"},
                "build": {"type": "string"},
                "role": {"const": "evidence-only"},
                "decides_severity": {"const": False},
                "decides_submission": {"const": False},
                "decides_research_state": {"const": False},
            },
        },
        "question_id": {"type": ["string", "null"]},
        "question": _NULLABLE_OBJECT,
        "status": {"enum": list(STATUSES)},
        "exit_code": {"enum": sorted(set(EXIT_CODES.values()))},
        "analysis_ran": {"type": "boolean"},
        "reason": {"type": "string"},
        "validation": _NULLABLE_OBJECT,
        "plan": _NULLABLE_OBJECT,
        "surface": _NULLABLE_OBJECT,
        "steering": _NULLABLE_OBJECT,
        "narrowed": {"type": "object"},
        "unapplied": {"type": "array", "items": {"type": "string"}},
        "on_surface": {"type": "object"},
        "evidence": {
            "oneOf": [
                {"type": "null"},
                {"type": "object", "required": ["schema_version", "producer"],
                 "properties": {"schema_version": {"const": "crystal-arcadia/2.0"}}},
            ],
        },
    },
    "allOf": [
        {
            "if": {"properties": {"status": {"const": "EXECUTED"}}},
            "then": {"properties": {"analysis_ran": {"const": True},
                                    "evidence": {"type": "object"},
                                    "exit_code": {"const": 0}}},
            "else": {"properties": {"analysis_ran": {"const": False},
                                    "evidence": {"type": "null"}}},
        },
        *[
            {"if": {"properties": {"status": {"const": status}}},
             "then": {"properties": {"exit_code": {"const": code}}}}
            for status, code in EXIT_CODES.items()
        ],
        {
            "if": {"properties": {"status": {"const": "REFUSED_UNREADABLE"}}},
            "then": {"properties": {"question": {"type": "null"},
                                    "question_id": {"type": "null"}}},
            "else": {"properties": {"question": {"type": "object"},
                                    "question_id": {"type": "string"}}},
        },
    ],
}

SCHEMAS = {"question": QUESTION, "result": RESULT}
