"""Build 022 — Crystal can be asked from outside Python, and always answers once.

Two things a process on the other side of the boundary needs, and neither
existed: a way in that is not a Python import (`crystal ask`), and a single,
versioned document to read back (`crystal-question-result/1.0`), whatever
happened — including when the question could not be read at all.

The guarantees pinned here are about the boundary, not about analysis:

* one envelope per call, schema-valid, on stdout or in a file;
* an exit code that always equals the envelope's own `exit_code`;
* `analysis_ran` and `evidence` that tell "not run" from "found nothing";
* nothing a caller can send produces a traceback instead of an envelope.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from crystal.question import (
    ResearchQuestion,
    SourceSnapshot,
    Surface,
    Target,
    dumps,
)
from crystal.question import envelope as envelope_module
from crystal.question.envelope import (
    EXIT_CODES,
    REFUSED_UNREADABLE,
    RESULT_SCHEMA,
    ask,
)
from crystal.question.schemas import QUESTION, RESULT, SCHEMAS

jsonschema = pytest.importorskip(
    "jsonschema",
    reason="schema conformance needs the dev extra; the zero-dependency core "
           "job installs pytest alone, so these are skipped there by design",
)

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMA_FILES = {
    "question": REPO_ROOT / "schemas" / "crystal-research-question-1.json",
    "result": REPO_ROOT / "schemas" / "crystal-question-result-1.json",
}

PROTOCOL = """
pragma solidity ^0.8.0;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    function deposit(uint256 a) external { totalAssets += a; totalSupply += a; }
    function withdraw(uint256 a) external { totalAssets -= a; totalSupply -= a; }
    function donate(uint256 a) external { totalAssets += a; }
}
"""


@pytest.fixture
def project(tmp_path):
    # Not `target/`: that is Cargo's build directory and discovery skips it on
    # purpose. The first draft used it, and every run was — correctly — refused
    # with REFUSED_NO_SOURCES instead of reading as an empty result.
    root = tmp_path / "protocol"
    root.mkdir()
    (root / "Vault.sol").write_text(PROTOCOL, encoding="utf-8")
    return root


def question(**overrides) -> ResearchQuestion:
    base = dict(
        question="Does the assets/shares relation survive the donate path?",
        target=Target(target_id="probe@1"),
        source_snapshot=SourceSnapshot(revision="local"),
        affected_surface=(Surface("function", "Vault.donate"),),
        required_capabilities=("static", "symbolic"),
        depth="standard",
    )
    base.update(overrides)
    return ResearchQuestion(**base)


def conforms(envelope: dict) -> dict:
    jsonschema.validate(envelope, RESULT)
    return envelope


# -- one envelope per outcome, each schema-valid ------------------------------

OUTCOMES = {
    "EXECUTED": lambda p: (dumps(question()), p),
    "REFUSED_INVALID": lambda p: (dumps(question(depth="exhaustive")), p),
    "REFUSED_NO_SOURCES": lambda p: (dumps(question()), p.parent / "nowhere"),
    "REFUSED_UNRESOLVED_SURFACE": lambda p: (
        dumps(question(affected_surface=(Surface("function", "Vault.dnoate"),))), p),
    "REFUSED_UNPLANNABLE": lambda p: (dumps(question(depth="adaptive")), p),
    "REFUSED_UNREADABLE": lambda p: ("{not json", p),
}


@pytest.mark.invariant
@pytest.mark.parametrize("status", sorted(OUTCOMES))
def test_every_outcome_returns_one_schema_valid_envelope(status, project):
    document, where = OUTCOMES[status](project)
    envelope = conforms(ask(document, where, use_solc=False))
    assert envelope["status"] == status
    assert envelope["exit_code"] == EXIT_CODES[status]


def test_every_status_crystal_can_return_has_its_own_exit_code():
    codes = list(EXIT_CODES.values())
    assert len(codes) == len(set(codes))
    assert EXIT_CODES["EXECUTED"] == 0
    assert all(code >= 3 for status, code in EXIT_CODES.items() if status != "EXECUTED")


@pytest.mark.invariant
def test_evidence_is_present_if_and_only_if_the_analysis_ran(project):
    """A refusal and a clean result both contain no findings; only these two
    fields tell them apart, and neither is inferred from counts."""
    ran = ask(dumps(question()), project, use_solc=False)
    assert ran["analysis_ran"] is True
    assert ran["evidence"]["schema_version"] == "crystal-arcadia/2.0"

    refused = ask(dumps(question()), project.parent / "nowhere", use_solc=False)
    assert refused["analysis_ran"] is False
    assert refused["evidence"] is None


def test_the_evidence_is_the_existing_arcadia_payload_not_a_second_format(project):
    from crystal.report import arcadia, payload
    from crystal.question import execute

    envelope = ask(dumps(question()), project, use_solc=False)
    direct = execute(question(), project, use_solc=False).result
    expected = json.loads(json.dumps(arcadia(payload(direct)), default=list))
    assert set(envelope["evidence"]) == set(expected)
    assert envelope["evidence"]["producer"] == expected["producer"]


@pytest.mark.invariant
def test_the_producer_block_restates_what_crystal_does_not_decide(project):
    for status in OUTCOMES:
        document, where = OUTCOMES[status](project)
        producer = ask(document, where, use_solc=False)["producer"]
        assert producer["role"] == "evidence-only"
        assert producer["decides_severity"] is False
        assert producer["decides_submission"] is False
        assert producer["decides_research_state"] is False


def test_the_envelope_is_attributable_to_the_question_that_was_asked(project):
    asked = question()
    envelope = ask(dumps(asked), project, use_solc=False)
    assert envelope["question_id"] == asked.question_id
    assert envelope["question"]["question_id"] == asked.question_id
    assert envelope["plan"]["question_id"] == asked.question_id


def test_a_build_020_document_is_answered_and_keeps_its_old_identity(project):
    raw = (REPO_ROOT / "tests" / "fixtures" / "question_build020.json").read_text(
        encoding="utf-8")
    envelope = conforms(ask(raw, project, use_solc=False))
    assert envelope["question"]["provenance"]["legacy_question_id"] == \
        json.loads(raw)["question_id"]


# -- nothing a caller can send produces a traceback ---------------------------

@pytest.mark.parametrize("document,why", [
    ("{not json", "not valid JSON"),
    ("[1, 2, 3]", "a serialised question is a JSON object"),
    (json.dumps({**json.loads(dumps(question())), "schema_version": "2.0"}),
     "schema major 2"),
    (json.dumps({**json.loads(dumps(question())),
                 "constraints": {"symbolic_budget": 1}}), "changed in transit"),
    (json.dumps({"schema_version": "1.1", "question": "x",
                 "target": "not-an-object", "source_snapshot": {}}), "wrong shape"),
])
def test_an_unreadable_document_is_an_envelope_not_an_exception(document, why, project):
    envelope = conforms(ask(document, project, use_solc=False))
    assert envelope["status"] == REFUSED_UNREADABLE
    assert why in envelope["reason"]


def test_mutation_p_an_exception_escaping_ask_is_caught(project, monkeypatch):
    """The mutant reads the document without catching what a caller can send,
    so a malformed question raises instead of being answered."""
    from crystal.question import serialization
    from crystal.question.runner import execute

    def leaky(document, where, **overrides):
        asked = serialization.loads(document)
        return envelope_module.from_run(asked, execute(asked, where, **overrides))

    monkeypatch.setattr(envelope_module, "ask", leaky)
    with pytest.raises(json.JSONDecodeError):
        envelope_module.ask("{not json", project, use_solc=False)

    monkeypatch.undo()
    assert envelope_module.ask("{not json", project)["status"] == REFUSED_UNREADABLE


def test_mutation_q_evidence_attached_to_a_refusal_is_caught(project, monkeypatch):
    """The schema, not only the code, forbids evidence on a run that did not
    happen — so a consumer validating envelopes is protected too."""
    envelope = ask(dumps(question()), project.parent / "nowhere", use_solc=False)
    forged = dict(envelope, evidence={"schema_version": "crystal-arcadia/2.0",
                                      "producer": {}})
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(forged, RESULT)


def test_mutation_r_a_refusal_reported_with_exit_zero_is_caught(project):
    envelope = ask(dumps(question()), project.parent / "nowhere", use_solc=False)
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(dict(envelope, exit_code=0), RESULT)


# -- the question schema -------------------------------------------------------

@pytest.mark.parametrize("asked", [
    question(),
    question(affected_surface=(), discover_surface=True),
    question(constraints={"symbolic_budget": 5, "excluded_paths": ["test"]},
             budget={"experiments": 1}, expected_output=("evidence",)),
])
def test_every_document_crystal_writes_conforms_to_the_question_schema(asked):
    jsonschema.validate(json.loads(dumps(asked)), QUESTION)


def test_the_build_020_fixture_conforms_to_the_question_schema():
    raw = (REPO_ROOT / "tests" / "fixtures" / "question_build020.json").read_text(
        encoding="utf-8")
    jsonschema.validate(json.loads(raw), QUESTION)


@pytest.mark.parametrize("patch", [
    {"source_snapshot": {}},
    {"affected_surface": []},
    {"constraints": {"max_gas": 1}},
    {"depth": "exhaustive"},
    {"schema_version": "2.0"},
])
def test_the_question_schema_refuses_what_the_validator_refuses(patch):
    """A caller validating locally against the schema gets the same answer the
    validator would give, for the refusals a schema can express."""
    document = {**json.loads(dumps(question())), **patch}
    document.pop("question_id")
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(document, QUESTION)


def test_the_published_schema_files_match_the_code_byte_for_byte():
    """`schemas/` is what a non-Python consumer reads. It must not drift."""
    for key, path in SCHEMA_FILES.items():
        expected = json.dumps(SCHEMAS[key], indent=2, sort_keys=True) + "\n"
        assert path.read_text(encoding="utf-8") == expected, path.name


def test_both_schemas_are_valid_draft_2020_12():
    for schema in SCHEMAS.values():
        jsonschema.Draft202012Validator.check_schema(schema)


# -- across a real process boundary --------------------------------------------

def run_cli(*args, stdin=None, cwd=None):
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", "crystal.cli", "ask", *args],
        input=stdin, capture_output=True, text=True, encoding="utf-8",
        timeout=300, env=environment, cwd=cwd,
    )


@pytest.mark.invariant
def test_the_cli_exit_code_always_equals_the_envelope(project, tmp_path):
    """A caller may branch on either; they must never disagree."""
    document = tmp_path / "q.json"
    document.write_text(dumps(question()), encoding="utf-8")
    for where, expected in ((project, 0), (tmp_path / "nowhere", 4)):
        completed = run_cli(str(document), "--project", str(where), "--no-solc")
        envelope = json.loads(completed.stdout)
        assert completed.returncode == envelope["exit_code"] == expected
        jsonschema.validate(envelope, RESULT)


def test_stdout_carries_only_the_envelope_even_when_not_quiet(project, tmp_path):
    """Progress goes to stderr. A consumer parses stdout whole."""
    document = tmp_path / "q.json"
    document.write_text(dumps(question()), encoding="utf-8")
    completed = run_cli(str(document), "--project", str(project), "--no-solc")
    assert json.loads(completed.stdout)["schema_version"] == RESULT_SCHEMA
    assert "status EXECUTED" in completed.stderr


def test_a_question_can_arrive_on_stdin(project):
    completed = run_cli("-", "--project", str(project), "--no-solc", "--quiet",
                        stdin=dumps(question()))
    assert completed.returncode == 0
    assert json.loads(completed.stdout)["analysis_ran"] is True


def test_garbage_on_stdin_is_answered_not_crashed_on(project):
    completed = run_cli("-", "--project", str(project), "--quiet", stdin="{nope")
    envelope = json.loads(completed.stdout)
    assert completed.returncode == 10 == envelope["exit_code"]
    assert "Traceback" not in completed.stderr


def test_the_schemas_can_be_printed_for_a_consumer_that_has_no_checkout():
    for key in ("question", "result"):
        completed = run_cli("--print-schema", key)
        assert completed.returncode == 0
        assert json.loads(completed.stdout) == json.loads(
            json.dumps(SCHEMAS[key], sort_keys=True))
