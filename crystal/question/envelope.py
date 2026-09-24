"""The one document a caller gets back when it asks Crystal a question.

`crystal-question-result/1.0`. Whatever happened — the analysis ran, the
question was refused, the document could not even be read — the caller receives
exactly one envelope of this shape, on stdout or in a file, and an exit code
that says the same thing. A process on the other side of the boundary never has
to parse a traceback or guess from an empty list.

Two fields carry most of the contract:

* **`analysis_ran`** — true only for `EXECUTED`. A refusal and a clean result
  both contain no findings; this is the field that tells them apart, and it is
  never inferred from counts.
* **`evidence`** — the existing `crystal-arcadia/2.0` payload, nested whole,
  when and only when the analysis ran. Not a second evidence format: one
  contract for what Crystal found, and this envelope for what was asked and
  what happened to the asking.

The producer block repeats, in every envelope, what Crystal does not decide.
A consumer composing Crystal with other engines should not have to know that
from the documentation.
"""

from __future__ import annotations

import json

from .. import __build__, __version__
from .runner import (
    EXECUTED,
    REFUSED_INVALID,
    REFUSED_NO_SOURCES,
    REFUSED_UNPLANNABLE,
    REFUSED_UNRESOLVED_SURFACE,
    QuestionRun,
)

RESULT_SCHEMA = "crystal-question-result/1.0"

# The question document could not be read at all: not JSON, an unknown schema
# MAJOR, or a document whose meaning moved in transit. There is no question to
# attribute anything to, so the envelope carries none.
REFUSED_UNREADABLE = "REFUSED_UNREADABLE"

# One code per outcome, so a caller can branch without parsing. 1 and 2 are
# left to the interpreter and to argparse (usage errors), which is why the
# refusals start at 3.
EXIT_CODES = {
    EXECUTED: 0,
    REFUSED_INVALID: 3,
    REFUSED_NO_SOURCES: 4,
    REFUSED_UNRESOLVED_SURFACE: 5,
    REFUSED_UNPLANNABLE: 6,
    REFUSED_UNREADABLE: 10,
}

STATUSES = tuple(EXIT_CODES)


def producer() -> dict:
    return {
        "tool": "crystal",
        "version": __version__,
        "build": __build__,
        "role": "evidence-only",
        "decides_severity": False,
        "decides_submission": False,
        "decides_research_state": False,
    }


def _evidence(result) -> dict | None:
    from ..report import arcadia, payload

    if result is None:
        return None
    # Through JSON and back: the nested payload must be exactly what
    # `--format arcadia` writes, sets and dataclasses flattened the same way.
    return json.loads(json.dumps(arcadia(payload(result)), default=list))


def from_run(question, run: QuestionRun) -> dict:
    """The envelope for a question Crystal could read."""
    ran = run.status == EXECUTED
    report = run.report()
    return {
        "schema_version": RESULT_SCHEMA,
        "producer": producer(),
        "question_id": question.question_id,
        "question": question.as_dict(),
        "status": run.status,
        "exit_code": EXIT_CODES[run.status],
        "analysis_ran": ran,
        "reason": run.reason,
        "validation": report["validation"],
        "plan": report["plan"],
        "surface": report["surface"],
        "steering": report["steering"],
        "narrowed": report["narrowed"],
        "unapplied": report["unapplied"],
        "on_surface": report["on_surface"],
        "evidence": _evidence(run.result) if ran else None,
    }


def unreadable(reason: str) -> dict:
    """The envelope when there was no question to answer."""
    return {
        "schema_version": RESULT_SCHEMA,
        "producer": producer(),
        "question_id": None,
        "question": None,
        "status": REFUSED_UNREADABLE,
        "exit_code": EXIT_CODES[REFUSED_UNREADABLE],
        "analysis_ran": False,
        "reason": reason,
        "validation": None,
        "plan": None,
        "surface": None,
        "steering": None,
        "narrowed": {},
        "unapplied": [],
        "on_surface": {},
        "evidence": None,
    }


def ask(document: str, project, **overrides) -> dict:
    """Read a question document, answer it, and return the envelope.

    Never raises for anything the caller could have sent: malformed JSON, an
    unknown MAJOR and a document whose meaning drifted all come back as
    `REFUSED_UNREADABLE` envelopes, so a caller across a process boundary gets a
    document to branch on instead of a stack trace.
    """
    from .runner import execute
    from .serialization import SemanticDrift, loads
    from .validation import UnsupportedSchema

    try:
        question = loads(document)
    except json.JSONDecodeError as exc:
        return unreadable(f"the question document is not valid JSON: {exc}")
    except UnsupportedSchema as exc:
        return unreadable(str(exc))
    except SemanticDrift as exc:
        return unreadable(str(exc))
    except (TypeError, AttributeError, ValueError) as exc:
        return unreadable(f"the question document has the wrong shape: {exc}")

    return from_run(question, execute(question, project, **overrides))
