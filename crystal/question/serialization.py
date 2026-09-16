"""Writing a question down, and reading it back as the same question.

"The same question" is the whole requirement. A round trip that preserves the
text but loses a constraint has produced a different question that happens to
read alike — and the loss is invisible, because what comes back is well formed.
So `loads` verifies the identity it reconstructs against the identity that was
written, and refuses a mismatch rather than returning something plausible.

A question is a file, not a session. Nothing here reaches for ambient state:
whatever a later process needs to know is in the bytes, or the question did not
carry it.
"""

from __future__ import annotations

import json
from pathlib import Path

from .model import (
    PriorEvidence,
    ResearchQuestion,
    SourceSnapshot,
    Surface,
    Target,
    canonical_json,
)
from .validation import UnsupportedSchema, parse_guard


class SemanticDrift(Exception):
    """What was read back is not what was written.

    Raised when a reconstructed question's content-derived identity does not
    match the one recorded in the document. Either the file was edited, or a
    field this build does not know about carried meaning — and a question whose
    meaning moved in transit must not be executed under its old id.
    """


def dumps(question: ResearchQuestion) -> str:
    """The canonical serialisation. Same question, same bytes, any machine."""
    return canonical_json(question.as_dict())


def dump(question: ResearchQuestion, path) -> Path:
    path = Path(path)
    path.write_text(dumps(question), encoding="utf-8")
    return path


def loads(text: str) -> ResearchQuestion:
    """Reconstruct a question, refusing anything that is not the same one."""
    raw = json.loads(text)
    if not isinstance(raw, dict):
        raise SemanticDrift("a serialised question is a JSON object")

    # Before any field is interpreted: a MAJOR this build does not know changes
    # what the fields mean, so reading them under the old meaning is the bug.
    parse_guard(raw.get("schema_version", ""))

    question = ResearchQuestion(
        question=raw.get("question", ""),
        hypothesis=raw.get("hypothesis", ""),
        schema_version=raw.get("schema_version", ""),
        target=Target(**_known(raw.get("target") or {}, Target)),
        source_snapshot=SourceSnapshot(
            **_known(raw.get("source_snapshot") or {}, SourceSnapshot)
        ),
        affected_surface=tuple(
            Surface(kind=item.get("kind", ""), identifier=item.get("identifier", ""))
            for item in raw.get("affected_surface") or ()
        ),
        discover_surface=bool(raw.get("discover_surface", False)),
        constraints=dict(raw.get("constraints") or {}),
        prior_evidence=tuple(
            PriorEvidence(
                evidence_id=item.get("evidence_id", ""),
                claim=item.get("claim", ""),
                polarity=item.get("polarity", "inconclusive"),
                provenance=dict(item.get("provenance") or {}),
            )
            for item in raw.get("prior_evidence") or ()
        ),
        required_capabilities=tuple(raw.get("required_capabilities") or ()),
        budget=dict(raw.get("budget") or {}),
        depth=raw.get("depth", "standard"),
        expected_output=tuple(raw.get("expected_output") or ()),
        provenance=dict(raw.get("provenance") or {}),
    )

    recorded = raw.get("question_id")
    if recorded and recorded != question.question_id:
        raise SemanticDrift(
            f"question_id {recorded} was written, {question.question_id} was "
            f"reconstructed: the document's meaning changed in transit and it "
            f"must not run under the old identity"
        )
    return question


def load(path) -> ResearchQuestion:
    return loads(Path(path).read_text(encoding="utf-8"))


def _known(raw: dict, cls) -> dict:
    """Only the fields this build's dataclass declares.

    A MINOR extension may add fields; ignoring them is what MINOR
    compatibility means. It is safe precisely because the identity check above
    runs afterwards — if an ignored field carried meaning, the reconstructed id
    will not match and the load fails loudly instead of running a question with
    a piece of it dropped.
    """
    fields = {f for f in cls.__dataclass_fields__}
    return {key: value for key, value in raw.items() if key in fields}


__all__ = ["dumps", "dump", "loads", "load", "SemanticDrift", "UnsupportedSchema"]
