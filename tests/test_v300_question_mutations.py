"""Build 020 — the mutation suite for the `ResearchQuestion` contract, §35 A–J.

Each test breaks the real code, confirms the damage actually occurs, and then
confirms the guard that is supposed to prevent it fires on the unmutated path.

That double check is the point. §32 warns against mutations structurally
incapable of causing the damage being tested: a mutation that cannot break
anything makes a suite look thorough while proving nothing. So every test here
asserts the mutant *misbehaves* before asserting the original does not.

One mutation in the brief's list, **D**, tested nothing when first written:
prior evidence was validated and then dropped, so "ignore prior evidence" was
already the behaviour. Rather than write a test that could not fail, the plan
was changed to carry it (`StrategyPlan.prior_evidence`) — which is what makes
the mutation meaningful. It still does not *steer* selection; that is adaptive
strategy, and it is not built.
"""

from __future__ import annotations

import json

import pytest

from crystal.question import (
    PriorEvidence,
    ResearchQuestion,
    SemanticDrift,
    SourceSnapshot,
    Surface,
    Target,
    UnsupportedSchema,
    dumps,
    loads,
    plan,
    to_legacy_kwargs,
    validate,
)
from crystal.question import capabilities as capabilities_module
from crystal.question import serialization as serialization_module
from crystal.question import strategy as strategy_module
from crystal.question import validation as validation_module


def question(**overrides) -> ResearchQuestion:
    base = dict(
        question="Does the assets/shares relation survive the donate path?",
        target=Target(target_id="lido/stonks@1"),
        source_snapshot=SourceSnapshot(revision="57fb317"),
        affected_surface=(Surface("function", "Vault.donate"),),
        required_capabilities=("static",),
        depth="standard",
    )
    base.update(overrides)
    return ResearchQuestion(**base)


# -- A · ignore source_snapshot ------------------------------------------------

def test_mutation_a_ignoring_the_snapshot_is_caught(monkeypatch):
    """A question about no particular world answers about whatever is on disk."""
    worldless = question(source_snapshot=SourceSnapshot())

    monkeypatch.setattr(
        SourceSnapshot, "identifies_a_world", property(lambda self: True)
    )
    assert validate(worldless).ok, "mutant must accept it, or the test is inert"

    monkeypatch.undo()
    result = validate(worldless)
    assert not result.ok
    assert "snapshot.missing" in result.codes()


# -- B · empty surface treated as the whole project ----------------------------

def test_mutation_b_an_empty_surface_meaning_everything_is_caught(monkeypatch):
    surfaceless = question(affected_surface=())

    original = validation_module.validate

    def permissive(q):
        return original(q.__class__(**{**q.__dict__, "discover_surface": True}))

    monkeypatch.setattr(validation_module, "validate", permissive)
    assert validation_module.validate(surfaceless).ok, "mutant must accept it"

    monkeypatch.undo()
    result = validation_module.validate(surfaceless)
    assert not result.ok
    assert "surface.empty" in result.codes()


# -- C · unknown capability accepted silently ----------------------------------

def test_mutation_c_swallowing_an_unknown_capability_is_caught(monkeypatch):
    asked = question(required_capabilities=("static", "telepathy"))

    monkeypatch.setattr(validation_module, "unknown_capabilities", lambda names: ())
    assert validate(asked).ok, "mutant must accept it"

    monkeypatch.undo()
    result = validate(asked)
    assert not result.ok
    assert "capability.unknown" in result.codes()
    assert capabilities_module.unknown(("telepathy",)) == ("telepathy",)


# -- D · prior evidence dropped ------------------------------------------------

def test_mutation_d_dropping_prior_evidence_is_caught(monkeypatch):
    """Handed in and then lost. Not steering yet — but demonstrably carried."""
    known = (
        PriorEvidence("E1", "the relation holds on deposit", "supports",
                      {"producer": "crystal", "revision": "57fb317"}),
    )
    asked = question(prior_evidence=known)

    monkeypatch.setattr(
        ResearchQuestion, "prior_evidence",
        property(lambda self: ()), raising=False,
    )
    assert plan(asked).prior_evidence == [], "mutant must drop it"

    monkeypatch.undo()
    carried = plan(asked).prior_evidence
    assert [item["evidence_id"] for item in carried] == ["E1"]
    assert carried[0]["attributable"] is True


def test_prior_evidence_without_provenance_is_carried_and_marked():
    asked = question(prior_evidence=(
        PriorEvidence("E9", "someone said so", "supports", {}),
    ))
    carried = plan(asked).prior_evidence
    assert carried[0]["attributable"] is False


# -- E · budget changed silently -----------------------------------------------

def test_mutation_e_widening_the_callers_budget_is_caught(monkeypatch):
    """A ceiling a planner may raise is not a ceiling."""
    bounded = question(depth="deep", budget={"symbolic_sequences": 5})

    monkeypatch.setattr(
        strategy_module, "_budget_for",
        lambda q: strategy_module.DEPTH_SYMBOLIC_BUDGET[q.depth],
    )
    assert plan(bounded).symbolic_budget == 300, "mutant must ignore the ceiling"

    monkeypatch.undo()
    assert plan(bounded).symbolic_budget == 5


def test_a_constraint_and_a_budget_both_narrow_and_never_widen():
    assert plan(question(depth="shallow", budget={"symbolic_sequences": 999})
                ).symbolic_budget == 0


# -- F · meaning changed in transit --------------------------------------------

def test_mutation_f_semantic_drift_on_reload_is_caught(monkeypatch):
    """A question whose meaning moved must not run under its old identity."""
    original = question(constraints={"symbolic_budget": 50})
    edited = json.loads(dumps(original))
    edited["constraints"] = {"symbolic_budget": 1}
    document = json.dumps(edited)

    # The mutant is a reader that trusts the recorded id instead of checking it.
    monkeypatch.setattr(
        ResearchQuestion, "question_id",
        property(lambda self: json.loads(document)["question_id"]),
    )
    accepted = serialization_module.loads(document)
    assert accepted.constraints == {"symbolic_budget": 1}, "mutant must accept it"

    monkeypatch.undo()
    with pytest.raises(SemanticDrift):
        loads(document)
    assert loads(dumps(original)).question_id == original.question_id


# -- G · unknown schema MAJOR accepted -----------------------------------------

def test_mutation_g_accepting_an_unknown_major_is_caught(monkeypatch):
    future = json.loads(dumps(question()))
    future["schema_version"] = "3.0"
    future.pop("question_id")
    document = json.dumps(future)

    monkeypatch.setattr(serialization_module, "parse_guard", lambda version: None)
    accepted = serialization_module.loads(document)
    assert accepted.schema_version == "3.0", "mutant must accept it"

    monkeypatch.undo()
    with pytest.raises(UnsupportedSchema):
        loads(document)


def test_a_minor_this_build_has_not_seen_is_not_refused():
    """That is what MINOR means, and the identity check makes it safe."""
    forward = json.loads(dumps(question()))
    forward["schema_version"] = "1.99"
    forward.pop("question_id")
    assert loads(json.dumps(forward)).schema_version == "1.99"


# -- H · global authority injected ---------------------------------------------

@pytest.mark.invariant
def test_mutation_h_global_authority_cannot_ride_in_on_a_question():
    """Severity, priority and submission are MIRA's. A question is not a side
    door into Crystal for them."""
    smuggled = json.loads(dumps(question()))
    smuggled.update({
        "severity": "critical", "payout": 50000, "submission": "immediate",
        "global_priority": 1, "coverage": "complete",
    })
    # The recorded identity was computed without them, so an injected field
    # either changes nothing or fails the identity check. Either is a refusal.
    reloaded = loads(json.dumps(smuggled))

    written = dumps(reloaded)
    for field_name in ("severity", "payout", "submission",
                       "global_priority", "coverage"):
        assert not hasattr(reloaded, field_name)
        assert field_name not in written
    assert reloaded.question_id == question().question_id

    planned = plan(reloaded).as_dict()
    assert "severity" not in json.dumps(planned)
    assert "payout" not in json.dumps(planned)


@pytest.mark.invariant
def test_the_validator_is_not_a_severity_engine():
    """§28. It checks form and feasibility; it does not grade a question."""
    trivial = question(question="Is the number 1 equal to 1 in this contract?")
    assert validate(trivial).ok, "form is the only thing being judged"

    assert "severity" not in json.dumps(validate(trivial).as_dict())


# -- I · identity moves during serialisation -----------------------------------

def test_mutation_i_an_identity_that_moves_in_transit_is_caught(monkeypatch):
    original = question()
    document = dumps(original)

    # The mutant computes identity over something other than the semantics, so
    # a faithfully written document no longer reconstructs to its own id.
    monkeypatch.setattr(
        ResearchQuestion, "semantic_content", lambda self: {"anything": "else"},
    )
    with pytest.raises(SemanticDrift):
        loads(document)

    monkeypatch.undo()
    assert loads(document).question_id == original.question_id
    assert dumps(loads(document)) == document


def test_identity_is_stable_across_repeated_serialisation():
    asked = question()
    once = loads(dumps(asked))
    twice = loads(dumps(once))
    assert once.question_id == twice.question_id == asked.question_id


# -- J · the legacy conversion claims a fidelity it does not have --------------

def test_mutation_j_a_lossy_legacy_conversion_that_claims_fidelity_is_caught(
        monkeypatch):
    rich = question(
        prior_evidence=(PriorEvidence("E1", "c", "supports",
                                      {"producer": "crystal", "revision": "a"}),),
        budget={"experiments": 2},
        expected_output=("proof",),
    )

    from crystal.question import legacy as legacy_module
    monkeypatch.setattr(
        legacy_module, "to_legacy_kwargs",
        lambda q: {"kwargs": {}, "project": "", "lost": []},
    )
    assert legacy_module.to_legacy_kwargs(rich)["lost"] == [], "mutant claims fidelity"

    monkeypatch.undo()
    mapped = to_legacy_kwargs(rich)
    assert set(mapped["lost"]) >= {
        "affected_surface", "prior_evidence", "budget", "expected_output",
    }


# -- the acceptance criteria of §36, as assertions ------------------------------

@pytest.mark.invariant
def test_every_strategy_that_would_run_is_attributable_to_a_question():
    """question → strategy traceability, 100%."""
    asked = question()
    for record in plan(asked).considered:
        assert record.question_id == asked.question_id
        assert record.reason


def test_no_silent_loss_across_the_whole_contract():
    """Constraints, snapshot, capabilities and budget all survive a round trip."""
    asked = question(
        constraints={"symbolic_budget": 42, "excluded_paths": ["legacy"]},
        budget={"experiments": 3},
        required_capabilities=("static", "symbolic"),
        source_snapshot=SourceSnapshot(revision="r1", tree_digest="d1",
                                       configuration_digest="c1"),
    )
    back = loads(dumps(asked))
    assert back.constraints == asked.constraints
    assert back.budget == asked.budget
    assert back.required_capabilities == asked.required_capabilities
    assert back.source_snapshot == asked.source_snapshot
    assert back.question_id == asked.question_id
