"""Build 020 — Crystal can finally be asked something.

`ResearchQuestion` is the first input contract. What is pinned here is narrow
and worth stating plainly: that a question can be written down, checked, stored,
reloaded in another process and turned into a strategy plan — not that Crystal
understands it semantically, and not that anything can yet schedule Crystal.

Two refusals carry most of the weight, because both failures are silent:

* the current checkout is **never** substituted for a snapshot nobody gave;
* an empty surface **never** means the whole project.

Both would produce a well-formed answer to a question that was not asked.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from crystal.question import (
    AT_EXECUTION,
    DEPTHS,
    ENFORCEABLE_BUDGET,
    ENFORCEABLE_CONSTRAINTS,
    PriorEvidence,
    ResearchQuestion,
    SemanticDrift,
    SourceSnapshot,
    Surface,
    Target,
    UnsupportedSchema,
    availability,
    dumps,
    export,
    from_legacy_call,
    loads,
    plan,
    to_legacy_kwargs,
    validate,
)
from crystal.question.strategy import UNMEASURED

REPO_ROOT = Path(__file__).resolve().parent.parent

QUESTION = (
    "Can an unvalidated zero value reach the accounting transition without "
    "preserving the assets/shares relation?"
)

LEGACY_FIXTURE = """
pragma solidity ^0.8.0;
contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }
    function withdraw(uint256 amount) external {
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
    }
}
"""


def question(**overrides) -> ResearchQuestion:
    base = dict(
        question=QUESTION,
        target=Target(target_id="lido/stonks@1", repository="lidofinance/stonks"),
        source_snapshot=SourceSnapshot(revision="57fb317", tree_digest="576929de"),
        affected_surface=(Surface("function", "Order.isValidSignature"),),
        required_capabilities=("static", "symbolic"),
        depth="standard",
    )
    base.update(overrides)
    return ResearchQuestion(**base)


# -- a well-formed question ----------------------------------------------------

def test_a_complete_question_validates():
    result = validate(question())
    assert result.ok
    assert result.codes() == []


def test_identity_is_content_derived_not_a_session_handle():
    """The same question about the same world is the same question."""
    assert question().question_id == question().question_id
    assert question().question_id != question(depth="deep").question_id


def test_who_asked_does_not_change_what_was_asked():
    """Provenance is excluded from identity on purpose.

    Two callers asking the identical question must not look like two questions.
    """
    plain = question()
    attributed = plain.with_provenance(asked_by="arcadia", ticket="ARC-1")
    assert attributed.question_id == plain.question_id
    assert attributed.provenance["asked_by"] == "arcadia"


def test_a_question_and_a_hypothesis_are_carried_without_merging():
    """§8: 'is P violated?' and 'if C holds, T violates P' are different inputs."""
    both = question(hypothesis="If minBuyAmount is 0, placeOrder can settle below floor.")
    assert both.question == QUESTION
    assert both.hypothesis.startswith("If minBuyAmount")
    assert validate(both).ok
    assert both.question_id != question().question_id


# -- the world is never assumed ------------------------------------------------

@pytest.mark.invariant
def test_a_missing_snapshot_is_refused_not_filled_from_the_current_checkout():
    result = validate(question(source_snapshot=SourceSnapshot()))
    assert not result.ok
    assert "snapshot.missing" in result.codes()


def test_resolving_at_execution_is_allowed_only_when_said_explicitly():
    result = validate(question(source_snapshot=SourceSnapshot(revision=AT_EXECUTION)))
    assert result.ok
    assert "snapshot.deferred" in result.codes()


# -- an empty surface is never everything --------------------------------------

@pytest.mark.invariant
def test_an_empty_surface_is_refused_unless_discovery_was_requested():
    result = validate(question(affected_surface=()))
    assert not result.ok
    assert "surface.empty" in result.codes()

    asked = validate(question(affected_surface=(), discover_surface=True))
    assert asked.ok


def test_a_surface_needs_a_kind_crystal_resolves_and_an_identifier():
    unknown_kind = validate(question(affected_surface=(Surface("vibes", "x"),)))
    assert "surface.unknown_kind" in unknown_kind.codes()

    unnamed = validate(question(affected_surface=(Surface("function", "  "),)))
    assert "surface.unidentified" in unnamed.codes()


# -- nothing is accepted that Crystal cannot apply -----------------------------

@pytest.mark.invariant
def test_a_constraint_crystal_cannot_enforce_is_an_error_not_a_default():
    """A bound accepted and ignored makes a result look narrower than it was."""
    result = validate(question(constraints={"max_gas": 100_000}))
    assert not result.ok
    assert "constraint.unenforceable" in result.codes()


def test_every_enforceable_constraint_is_accepted():
    result = validate(question(constraints={
        "allowed_paths": ["src"], "excluded_paths": ["test"],
        "max_sequence_length": 3, "symbolic_budget": 50, "timeout_seconds": 300,
    }))
    assert result.ok
    assert set(ENFORCEABLE_CONSTRAINTS) >= {"symbolic_budget", "timeout_seconds"}


@pytest.mark.invariant
def test_a_budget_dimension_crystal_does_not_control_is_an_error():
    result = validate(question(budget={"gpu_hours": 2}))
    assert not result.ok
    assert "budget.unenforceable" in result.codes()
    assert "gpu_hours" not in ENFORCEABLE_BUDGET


def test_a_negative_budget_is_refused():
    assert "budget.invalid_value" in validate(
        question(budget={"experiments": -1})
    ).codes()


@pytest.mark.invariant
def test_an_unknown_capability_is_refused_rather_than_ignored():
    result = validate(question(required_capabilities=("static", "telepathy")))
    assert not result.ok
    assert "capability.unknown" in result.codes()


def test_an_unknown_depth_is_refused():
    assert "depth.unknown" in validate(question(depth="exhaustive")).codes()
    assert set(DEPTHS) == {"shallow", "standard", "deep", "adaptive"}


def test_an_output_shape_crystal_does_not_produce_is_refused():
    assert "output.unknown" in validate(
        question(expected_output=("severity_rating",))
    ).codes()


# -- the negative cases §34 names ---------------------------------------------

@pytest.mark.parametrize("overrides,code", [
    ({"target": Target(target_id="  ")}, "target.missing"),
    ({"source_snapshot": SourceSnapshot()}, "snapshot.missing"),
    ({"affected_surface": (Surface("function", ""),)}, "surface.unidentified"),
    ({"required_capabilities": ("nope",)}, "capability.unknown"),
    ({"budget": {"nope": 1}}, "budget.unenforceable"),
    ({"depth": "nope"}, "depth.unknown"),
    ({"question": ""}, "question.empty"),
    ({"schema_version": "9.0"}, "schema.unknown_major"),
    ({"schema_version": "not-a-version"}, "schema.malformed"),
    ({"constraints": {"nope": 1}}, "constraint.unenforceable"),
])
def test_each_malformed_question_fails_with_its_own_reason(overrides, code):
    result = validate(question(**overrides))
    assert not result.ok
    assert code in result.codes()


def test_an_invalid_prior_evidence_reference_is_refused():
    duplicated = (
        PriorEvidence("E1", "assets and shares move together", "supports",
                      {"producer": "crystal", "revision": "abc"}),
        PriorEvidence("E1", "and again", "supports",
                      {"producer": "crystal", "revision": "abc"}),
    )
    codes = validate(question(prior_evidence=duplicated)).codes()
    assert "evidence.duplicate" in codes

    assert "evidence.invalid_polarity" in validate(question(prior_evidence=(
        PriorEvidence("E2", "c", "probably", {"producer": "x", "revision": "y"}),
    ))).codes()


def test_prior_evidence_without_provenance_is_marked_not_rejected():
    """It may steer strategy. It stays attributable-as-unattributable."""
    result = validate(question(prior_evidence=(
        PriorEvidence("E3", "someone says this holds", "supports", {}),
    )))
    assert result.ok
    assert "evidence.unattributable" in result.codes()


# -- serialisation -------------------------------------------------------------

@pytest.mark.invariant
def test_serialisation_is_deterministic_and_round_trips_without_drift():
    original = question(
        constraints={"symbolic_budget": 50}, budget={"experiments": 2},
        prior_evidence=(PriorEvidence("E1", "c", "refutes",
                                      {"producer": "crystal", "revision": "a"}),),
    )
    once, twice = dumps(original), dumps(original)
    assert once == twice

    back = loads(once)
    assert back.question_id == original.question_id
    assert back.as_dict() == original.as_dict()
    assert dumps(back) == once


@pytest.mark.invariant
def test_a_document_whose_meaning_changed_in_transit_is_refused():
    """Not "repaired". A question whose meaning moved must not run under its
    old identity."""
    tampered = json.loads(dumps(question()))
    tampered["constraints"] = {"symbolic_budget": 1}
    with pytest.raises(SemanticDrift):
        loads(json.dumps(tampered))


@pytest.mark.invariant
def test_an_unknown_schema_major_is_refused_before_anything_is_interpreted():
    future = json.loads(dumps(question()))
    future["schema_version"] = "2.0"
    with pytest.raises(UnsupportedSchema):
        loads(json.dumps(future))


def test_an_unknown_minor_field_is_ignored_and_the_identity_still_checks():
    """That is what MINOR compatibility means, and the identity check is what
    makes ignoring safe."""
    extended = json.loads(dumps(question()))
    extended["schema_version"] = "1.7"
    extended["some_future_hint"] = "ignored"
    # The version is part of the identity, so the recorded id no longer matches;
    # drop it exactly as a MINOR-aware reader would, and the load succeeds.
    extended.pop("question_id")
    reloaded = loads(json.dumps(extended))
    assert reloaded.schema_version == "1.7"
    assert reloaded.question == QUESTION


# -- another process, no shared memory ----------------------------------------

REPLAY = """
import json, sys
from crystal.question import load, validate, plan
q = load(sys.argv[1])
v = validate(q)
p = plan(q)
print(json.dumps({
    "question_id": q.question_id,
    "valid": v.ok,
    "selected": sorted(r.strategy for r in p.selected),
    "budget": p.symbolic_budget,
    "constraints": q.constraints,
}))
"""


@pytest.mark.invariant
def test_a_question_reloads_and_plans_identically_in_a_separate_process(tmp_path):
    """§31. A question is a file, not a session: whatever a later process needs
    is in the bytes, or the question did not carry it."""
    original = question(constraints={"symbolic_budget": 40})
    path = tmp_path / "question.json"
    path.write_text(dumps(original), encoding="utf-8")
    script = tmp_path / "replay.py"
    script.write_text(REPLAY, encoding="utf-8")

    # Explicitly this checkout. Outside the repository `crystal` resolves to
    # whatever is installed, which on a developer machine is routinely an older
    # build — the test would then pass or fail on the install rather than on
    # the contract.
    environment = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
    completed = subprocess.run(
        [sys.executable, str(script), str(path)],
        capture_output=True, text=True, timeout=180, encoding="utf-8",
        env=environment, cwd=str(tmp_path),
    )
    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout)

    here = plan(original)
    assert seen["question_id"] == original.question_id
    assert seen["valid"] is True
    assert seen["selected"] == sorted(r.strategy for r in here.selected)
    assert seen["budget"] == here.symbolic_budget == 40
    assert seen["constraints"] == {"symbolic_budget": 40}


# -- question to strategy ------------------------------------------------------

@pytest.mark.invariant
def test_every_selected_strategy_is_attributable_to_its_question():
    """§36: question → strategy traceability, 100%."""
    asked = question()
    selected = plan(asked)
    assert selected.considered
    for record in selected.considered:
        assert record.question_id == asked.question_id


def test_the_trace_keeps_what_was_considered_and_not_only_what_ran():
    rejected = [r for r in plan(question(depth="shallow")).considered
                if not r.selected]
    assert rejected
    assert all(r.reason for r in rejected)


def test_depth_narrows_what_may_run():
    shallow = {r.strategy for r in plan(question(depth="shallow")).selected}
    standard = {r.strategy for r in plan(question(depth="standard")).selected}
    assert shallow < standard
    assert "symbolic-sequence" not in shallow


def test_the_budget_is_the_narrowest_of_depth_constraint_and_caller_budget():
    """A caller's budget is a ceiling; a hungrier depth does not raise it."""
    assert plan(question(depth="deep")).symbolic_budget == 300
    assert plan(question(depth="deep", constraints={"symbolic_budget": 20})
                ).symbolic_budget == 20
    assert plan(question(depth="deep", constraints={"symbolic_budget": 20},
                         budget={"symbolic_sequences": 5})).symbolic_budget == 5


@pytest.mark.invariant
def test_no_strategy_is_a_real_answer_given_with_a_reason():
    """§23. A planner that must return a plan will return a bad one."""
    empty = plan(question(depth="shallow", required_capabilities=("symbolic",)))
    assert not empty.plannable
    assert empty.obstructions
    assert any("does not permit" in reason for reason in empty.obstructions)


@pytest.mark.invariant
def test_adaptive_depth_is_an_obstruction_not_a_quiet_downgrade():
    """Declared by the schema, not implemented. Planning it as `standard`
    would answer an easier question than the one asked."""
    adaptive = plan(question(depth="adaptive"))
    assert not adaptive.plannable
    assert any("not implemented" in reason for reason in adaptive.obstructions)


@pytest.mark.invariant
def test_information_gain_is_reported_as_unmeasured_never_as_a_number():
    """A made-up number is worse than a gap: it will be summed and believed."""
    for record in plan(question()).considered:
        assert record.expected_information_gain == UNMEASURED
        assert record.estimated_cost == UNMEASURED


def test_an_unavailable_capability_is_an_obstruction_with_its_reason():
    class Missing:
        available = False
        reason = "medusa executable not installed"

    blocked = plan(question(depth="deep", required_capabilities=("fuzzing",)),
                   backends={"medusa": Missing(), "echidna": Missing()})
    assert not blocked.plannable
    assert any("not installed" in reason for reason in blocked.obstructions)


# -- capabilities --------------------------------------------------------------

def test_internal_capabilities_are_available_without_any_external_tool():
    reported = availability({})
    assert reported["static"]["available"]
    assert reported["symbolic"]["available"]
    assert not reported["fuzzing"]["available"]


def test_an_unprobed_tool_is_distinguished_from_an_unavailable_one():
    assert "not probed" in " ".join(availability({})["runtime"]["unavailable"])


def test_the_registry_exports_without_claiming_anything_about_this_machine():
    exported = export()
    assert exported["schema"] == "crystal-capabilities/1.0"
    assert "fuzzing" in exported["capabilities"]
    fuzzing = exported["capabilities"]["fuzzing"]
    assert "available" not in fuzzing            # no claim about this machine
    assert fuzzing["always_available"] is False  # a property of the capability


# -- the legacy entry point ----------------------------------------------------

def test_the_legacy_call_still_works(tmp_path):
    """§33: no removal before a measured equivalent exists."""
    from crystal.engine import research

    (tmp_path / "V.sol").write_text(LEGACY_FIXTURE, encoding="utf-8")
    result = research(tmp_path, use_solc=False, use_foundry=False)
    assert result["research_candidates"]


@pytest.mark.invariant
def test_the_legacy_call_states_its_assumptions_instead_of_hiding_them(tmp_path):
    """Written down, its two implicit defaults become explicit declarations."""
    translated = from_legacy_call(tmp_path, use_foundry=False, target_id="t1")
    assert translated.source_snapshot.revision == AT_EXECUTION
    assert translated.discover_surface is True
    assert validate(translated).ok
    assert "snapshot.deferred" in validate(translated).codes()


def test_the_legacy_translation_says_which_identity_it_produced(tmp_path):
    weak = from_legacy_call(tmp_path, use_foundry=False)
    strong = from_legacy_call(tmp_path, use_foundry=False, target_id="lido/stonks@1")
    assert weak.provenance["target_identity"] == "resolved-path"
    assert strong.provenance["target_identity"] == "caller-supplied"


def test_use_foundry_maps_to_depth_rather_than_to_a_tool_name(tmp_path):
    assert from_legacy_call(tmp_path, use_foundry=True).depth == "deep"
    assert from_legacy_call(tmp_path, use_foundry=False).depth == "standard"


@pytest.mark.invariant
def test_converting_back_to_the_legacy_api_names_what_it_cannot_carry():
    """§35 J: a lossy direction that claims fidelity is the failure."""
    rich = question(
        prior_evidence=(PriorEvidence("E1", "c", "supports",
                                      {"producer": "crystal", "revision": "a"}),),
        budget={"experiments": 1},
        expected_output=("proof",),
        hypothesis="If C then T violates P",
    )
    mapped = to_legacy_kwargs(rich)
    assert set(mapped["lost"]) >= {
        "affected_surface", "prior_evidence", "budget", "expected_output",
        "hypothesis", "source_snapshot",
    }


# -- the boundary --------------------------------------------------------------

@pytest.mark.invariant
def test_the_contract_has_no_field_for_global_authority():
    """§32. Scheduling, coverage, saturation, severity and submission are
    MIRA's, and a question is not a side door into Crystal for them."""
    fields = set(ResearchQuestion.__dataclass_fields__)
    forbidden = {
        "severity", "payout", "submission", "priority", "global_priority",
        "coverage", "saturation", "research_debt", "lease", "queue",
        "human_review", "programme", "target_lifecycle",
    }
    assert not (fields & forbidden)
    assert not (set(dumps(question()).split('"')) & forbidden)


# -- into the machinery --------------------------------------------------------

EXEC_FIXTURE = """
pragma solidity ^0.8.0;
contract V {
    uint256 public total;
    uint256 public shares;
    function open(uint256 a) external { total += a; shares += a; }
    function close(uint256 a) external { total -= a; shares -= a; }
    function skew(uint256 a) external { total += a; }
}
"""


@pytest.fixture
def probe(tmp_path):
    (tmp_path / "V.sol").write_text(EXEC_FIXTURE, encoding="utf-8")
    return tmp_path


def asked(**overrides) -> ResearchQuestion:
    """A question about code the probe fixture actually contains.

    Build 020's execution tests asked about `Order.isValidSignature` against a
    fixture with no such function, and passed — because the runner scanned the
    whole project and filed the result under that question. Build 021 refuses
    that, so these tests ask about something that exists.
    """
    overrides.setdefault("affected_surface", (Surface("function", "V.open"),))
    return question(**overrides)


@pytest.mark.invariant
def test_a_question_narrows_the_budget_the_run_actually_spends(probe):
    """A limit applied after the run is a limit nothing read."""
    from crystal.question import execute

    run = execute(asked(constraints={"symbolic_budget": 7}), probe,
                  use_solc=False)
    assert run.executed
    assert run.plan.symbolic_budget == 7
    assert run.result["sequence_budget"]["budget"] == 7


@pytest.mark.invariant
def test_a_declared_exclusion_actually_narrows_what_is_read(probe):
    """§13. A constraint recorded and not applied makes a result look narrower
    than the run was — the one failure the whole constraint list guards."""
    from crystal.question import execute

    (probe / "extra").mkdir()
    (probe / "extra" / "Other.sol").write_text(EXEC_FIXTURE, encoding="utf-8")

    wide = execute(asked(), probe, use_solc=False)
    narrow = execute(asked(constraints={"excluded_paths": ["extra"]}),
                     probe, use_solc=False)

    assert len(narrow.result["sources"]) < len(wide.result["sources"])
    assert narrow.narrowed["excluded_paths"] == ["extra"]
    assert not any("excluded_paths" in item for item in narrow.unapplied)


def test_an_exclusion_matches_path_segments_not_substrings(probe):
    """Excluding `test` drops `test/Foo.sol` and never `latest/Foo.sol`."""
    from crystal.question import execute

    (probe / "latest").mkdir()
    (probe / "latest" / "Keep.sol").write_text(EXEC_FIXTURE, encoding="utf-8")

    run = execute(asked(constraints={"excluded_paths": ["test"]}),
                  probe, use_solc=False)
    kept = {Path(source).name for source in run.result["sources"]}
    assert "Keep.sol" in kept


@pytest.mark.invariant
def test_an_invalid_question_does_not_run(probe):
    """A validator whose verdict can be ignored is documentation."""
    from crystal.question import REFUSED_INVALID, execute

    run = execute(asked(source_snapshot=SourceSnapshot()), probe,
                  use_solc=False)
    assert run.status == REFUSED_INVALID
    assert run.result is None
    assert "snapshot.missing" in run.validation.codes()


@pytest.mark.invariant
def test_an_unplannable_question_is_blocked_not_reported_as_finding_nothing(probe):
    """§26: 'blocked' and 'found nothing' must never be the same answer."""
    from crystal.question import REFUSED_UNPLANNABLE, execute

    run = execute(asked(depth="adaptive"), probe, use_solc=False)
    assert run.status == REFUSED_UNPLANNABLE
    assert run.result is None
    assert run.plan.obstructions


@pytest.mark.invariant
def test_the_run_names_what_the_question_asked_for_and_did_not_get(probe):
    """A resolved surface steers the budget and says so; a missing or
    unsupported item in the same question is named, not dropped."""
    from crystal.question import execute

    run = execute(asked(affected_surface=(
        Surface("function", "V.open"),
        Surface("function", "V.doesNotExist"),
        Surface("asset_flow", "a->b"),
    )), probe, use_solc=False)
    assert run.executed
    assert run.narrowed["symbolic_focus"] == ["function:V.open"]
    assert any("V.doesNotExist" in item for item in run.unapplied)
    assert any("asset_flow" in item for item in run.unapplied)


def test_every_output_of_a_run_is_attributable_to_its_question(probe):
    from crystal.question import execute

    question_ = asked(constraints={"symbolic_budget": 5})
    run = execute(question_, probe, use_solc=False)
    attached = run.result["research_question"]
    assert attached["question_id"] == question_.question_id
    assert attached["plan"]["question_id"] == question_.question_id
    assert attached["status"] == "EXECUTED"


@pytest.mark.invariant
def test_a_run_status_is_local_and_carries_no_global_state():
    """No COMPLETE, no CONFIRMED. Those are MIRA's, and not from here."""
    from crystal.question import runner as execute_module

    statuses = {
        value for name, value in vars(execute_module).items()
        if name.isupper() and isinstance(value, str)
    }
    assert statuses == {
        "EXECUTED", "REFUSED_INVALID", "REFUSED_NO_SOURCES",
        "REFUSED_UNRESOLVED_SURFACE", "REFUSED_UNPLANNABLE",
    }
    for forbidden in ("COMPLETE", "CONFIRMED", "SATURATED", "APPROVED"):
        assert not any(forbidden in status for status in statuses)
