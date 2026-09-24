"""Build 021 — a question's surface and prior evidence decide where the budget goes.

Measured on two real protocols before the change (Build 020): a question *about*
`Order.isValidSignature`, given 30 symbolic slots, executed **zero** sequences
touching it. On the Flyover bridge a question about `isCollateralSufficient`
got zero as well. The surface was carried and ignored, and the one item that
was covered took every slot — `Order.initialize`, 13 of 13.

Also found while measuring: `execute()` on a path with no sources answered
`EXECUTED` with every count at zero, which reads exactly like a clean target.

Each guarantee below is paired with a mutation that breaks it; each mutation
test first confirms the mutant misbehaves, so none of them can pass inertly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crystal.campaigns.definition import CampaignDefinition, CampaignScope
from crystal.campaigns.runner import run_campaign
from crystal.parsers import parse_project
from crystal.question import (
    REFUSED_NO_SOURCES,
    REFUSED_UNRESOLVED_SURFACE,
    PriorEvidence,
    ResearchQuestion,
    SourceSnapshot,
    Surface,
    Target,
    dumps,
    execute,
    loads,
    resolve,
    steer,
)
from crystal.question import runner as runner_module
from crystal.scheduling import schedule_sequences
from crystal.scheduling import demand as demand_module

FIXTURES = Path(__file__).parent / "fixtures"

PROTOCOL = """
pragma solidity ^0.8.0;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    address public owner;
    function deposit(uint256 a) external { totalAssets += a; totalSupply += a; }
    function withdraw(uint256 a) external { totalAssets -= a; totalSupply -= a; }
    function donate(uint256 a) external { totalAssets += a; }
    function setOwner(address o) external { owner = o; }
    function peek() external view returns (uint256) { return totalAssets; }
}
contract Router {
    Vault public vault;
    uint256 public routed;
    function route(uint256 a) external { routed += a; vault.deposit(a); }
}
"""

MOCK = """
pragma solidity ^0.8.0;
contract VaultMock { uint256 public x; function onlyInMock() external { x += 1; } }
"""


@pytest.fixture
def project(tmp_path):
    (tmp_path / "Vault.sol").write_text(PROTOCOL, encoding="utf-8")
    (tmp_path / "mocks").mkdir()
    (tmp_path / "mocks" / "VaultMock.sol").write_text(MOCK, encoding="utf-8")
    return tmp_path


@pytest.fixture
def contracts(project):
    from crystal.engine import select_sources
    return parse_project(select_sources(project)).contracts


def ask(**overrides) -> ResearchQuestion:
    base = dict(
        question="Does the assets/shares relation survive every path into Vault?",
        target=Target(target_id="probe@1"),
        source_snapshot=SourceSnapshot(revision="local"),
        affected_surface=(Surface("function", "Vault.donate"),),
        required_capabilities=("static", "symbolic"),
        depth="standard",
    )
    base.update(overrides)
    return ResearchQuestion(**base)


def evidence(evidence_id, polarity, about, attributable=True):
    provenance = {"producer": "crystal", "revision": "abc"} if attributable else {}
    return PriorEvidence(evidence_id, "a prior claim", polarity, provenance,
                         about=tuple(Surface("function", name) for name in about))


class H:
    """A sequence hypothesis: the scheduler reads these two fields."""

    def __init__(self, sequence, score=0.72):
        self.sequence = tuple(sequence)
        self.score = score


# -- resolving a surface against the code -------------------------------------

def test_each_resolvable_kind_maps_onto_entry_points(contracts):
    resolution = resolve((
        Surface("function", "Vault.donate"),
        Surface("contract", "Router"),
        Surface("state_variable", "Vault::totalSupply"),
        Surface("call_path", "Router.route -> Vault.deposit"),
    ), contracts)
    found = {f"{item.kind}:{item.identifier}": set(item.functions)
             for item in resolution.resolved}
    assert found["function:Vault.donate"] == {"Vault.donate"}
    assert found["contract:Router"] >= {"Router.route"}
    assert found["state_variable:Vault::totalSupply"] >= {"Vault.deposit", "Vault.withdraw"}
    assert "Vault.donate" not in found["state_variable:Vault::totalSupply"]
    assert found["call_path:Router.route -> Vault.deposit"] == {"Router.route", "Vault.deposit"}


def test_a_name_that_does_not_exist_is_unresolved_not_approximated(contracts):
    resolution = resolve((Surface("function", "Vault.dnoate"),), contracts)
    assert not resolution.resolved
    assert resolution.unresolved[0]["identifier"] == "Vault.dnoate"


def test_a_kind_crystal_cannot_map_is_reported_unsupported(contracts):
    resolution = resolve((Surface("asset_flow", "stETH->LDO"),), contracts)
    assert not resolution.resolved
    assert resolution.unsupported[0]["kind"] == "asset_flow"


def test_a_surface_that_only_exists_in_a_mock_is_not_the_target(contracts):
    """Resolution runs on the contract set research runs on."""
    resolution = resolve((Surface("function", "VaultMock.onlyInMock"),), contracts)
    assert not resolution.resolved


# -- the budget follows the question ------------------------------------------

def test_without_a_focus_the_ordering_is_exactly_build_018():
    """The legacy path must not move: no question, no change."""
    hypotheses = [H(("A.a",), 0.9), H(("B.b",), 0.72), H(("C.c",), 0.72)]
    plain = schedule_sequences(hypotheses, budget=3)
    empty = schedule_sequences(hypotheses, budget=3, focus=[])
    assert [h.sequence for h in plain.executed] == [h.sequence for h in empty.executed]
    assert plain.report()["focus"] == []


@pytest.mark.invariant
def test_sequences_touching_the_surface_take_the_budget_first():
    hypotheses = [H(("X.a",), 0.95), H(("X.b",), 0.9), H(("S.f",), 0.5)]
    allocation = schedule_sequences(
        hypotheses, budget=1, focus=[("function:S.f", frozenset({"S.f"}))],
    )
    assert [h.sequence for h in allocation.executed] == [("S.f",)]


@pytest.mark.invariant
def test_surface_items_share_the_budget_instead_of_one_taking_it_all():
    """On stonks, `Order.initialize` took 13 of 13 surface slots and
    `Order.isValidSignature` none. Round-robin across items is the fix."""
    hypotheses = [H(("Big.f", f"X.{i}"), 0.9) for i in range(20)] + [
        H(("Small.g", "Y.1"), 0.5), H(("Small.g", "Y.2"), 0.5),
    ]
    allocation = schedule_sequences(hypotheses, budget=4, focus=[
        ("function:Big.f", frozenset({"Big.f"})),
        ("function:Small.g", frozenset({"Small.g"})),
    ])
    coverage = {item["item"]: item["executed"] for item in allocation.focus}
    assert coverage == {"function:Big.f": 2, "function:Small.g": 2}


def test_a_surface_item_no_hypothesis_touches_is_reported_uncovered():
    allocation = schedule_sequences([H(("A.a",))], budget=5, focus=[
        ("function:A.a", frozenset({"A.a"})),
        ("function:Ghost.g", frozenset({"Ghost.g"})),
    ])
    assert allocation.report()["focus_uncovered"] == ["function:Ghost.g"]


def test_mutation_k_a_scheduler_that_ignores_the_surface_is_caught(monkeypatch):
    hypotheses = [H(("X.a",), 0.95), H(("S.f",), 0.5)]
    focus = [("function:S.f", frozenset({"S.f"}))]

    monkeypatch.setattr(demand_module, "_focus_order", lambda scored, focus: scored)
    mutant = schedule_sequences(hypotheses, budget=1, focus=focus)
    assert mutant.focus[0]["executed"] == 0, "mutant must starve the surface"

    monkeypatch.undo()
    assert schedule_sequences(hypotheses, budget=1, focus=focus).focus[0]["executed"] == 1


# -- the runner refuses what it cannot honestly run ----------------------------

@pytest.mark.invariant
def test_a_path_with_nothing_to_analyse_is_refused_not_reported_clean(tmp_path):
    """Build 020 answered EXECUTED with every count at zero."""
    run = execute(ask(), tmp_path / "nothing-here", use_solc=False)
    assert run.status == REFUSED_NO_SOURCES
    assert run.result is None


@pytest.mark.invariant
def test_a_question_about_code_that_does_not_exist_is_refused_before_analysis(
        project, monkeypatch):
    """Refused before the expensive run, not after it."""
    called = []
    monkeypatch.setattr(runner_module, "research",
                        lambda *a, **k: called.append(1) or {})
    run = execute(ask(affected_surface=(Surface("function", "Vault.dnoate"),)),
                  project, use_solc=False)
    assert run.status == REFUSED_UNRESOLVED_SURFACE
    assert called == []
    assert run.surface.unresolved[0]["identifier"] == "Vault.dnoate"


def test_mutation_m_running_an_unresolved_surface_anyway_is_caught(
        project, monkeypatch):
    """The mutant treats 'nothing resolved' as 'go ahead'."""
    question = ask(affected_surface=(Surface("function", "Vault.dnoate"),))

    monkeypatch.setattr(runner_module.SurfaceResolution, "any_resolved",
                        property(lambda self: True))
    assert execute(question, project, use_solc=False).executed,         "mutant must run a question about nothing"

    monkeypatch.undo()
    assert execute(question, project, use_solc=False).status == REFUSED_UNRESOLVED_SURFACE


def test_mutation_n_an_empty_path_reported_as_executed_is_caught(tmp_path, monkeypatch):
    """The mutant pretends there was something to read, and the run then looks
    exactly like a clean target: executed, every count zero."""
    import crystal.engine as engine_module
    import crystal.parsers as parsers_module

    empty = tmp_path / "nothing-here"
    monkeypatch.setattr(engine_module, "select_sources", lambda *a, **k: ["phantom.sol"])
    monkeypatch.setattr(parsers_module, "parse_project",
                        lambda sources: type("P", (), {"contracts": []})())
    monkeypatch.setattr(runner_module, "research", lambda *a, **k: {})
    mutant = execute(ask(discover_surface=True, affected_surface=()), empty,
                     use_solc=False)
    assert mutant.executed, "mutant must report the empty path as executed"

    monkeypatch.undo()
    assert execute(ask(), empty, use_solc=False).status == REFUSED_NO_SOURCES


def test_a_partly_resolved_surface_runs_and_names_the_missing_part(project):
    run = execute(ask(affected_surface=(
        Surface("function", "Vault.donate"), Surface("function", "Vault.dnoate"),
    )), project, use_solc=False)
    assert run.executed
    assert run.narrowed["symbolic_focus"] == ["function:Vault.donate"]
    assert any("Vault.dnoate" in item for item in run.unapplied)


def test_outputs_are_partitioned_by_surface_never_filtered(project):
    """A finding one call away from the surface is still a finding."""
    run = execute(ask(), project, use_solc=False)
    split = run.on_surface["research_candidates"]
    assert split["on_surface"] + split["elsewhere"] == len(run.result["research_candidates"])


# -- prior evidence reorders, never skips -------------------------------------

def _groups(contracts, names):
    return resolve(tuple(Surface("function", n) for n in names), contracts)


def test_uncertainty_is_reached_before_what_is_already_supported(contracts):
    resolution = _groups(contracts, ("Vault.deposit", "Vault.donate", "Vault.withdraw"))
    question = ask(affected_surface=tuple(
        Surface("function", n) for n in ("Vault.deposit", "Vault.donate", "Vault.withdraw")),
        prior_evidence=(
            evidence("E1", "supports", ["Vault.deposit"]),
            evidence("E2", "refutes", ["Vault.withdraw"]),
        ))
    order = [label for label, _ in steer(question, resolution, contracts).order]
    assert order == ["function:Vault.withdraw", "function:Vault.donate",
                     "function:Vault.deposit"]


@pytest.mark.invariant
def test_a_contradiction_is_reported_and_reached_first_not_resolved(contracts):
    resolution = _groups(contracts, ("Vault.deposit", "Vault.donate"))
    question = ask(affected_surface=(Surface("function", "Vault.deposit"),
                                     Surface("function", "Vault.donate")),
                   prior_evidence=(evidence("E1", "supports", ["Vault.donate"]),
                                   evidence("E2", "refutes", ["Vault.donate"])))
    steering = steer(question, resolution, contracts)
    assert steering.order[0][0] == "function:Vault.donate"
    assert steering.contradictions[0]["supports"] == ["E1"]
    assert steering.contradictions[0]["refutes"] == ["E2"]
    assert "does not pick a side" in steering.contradictions[0]["resolution"]


@pytest.mark.invariant
def test_supported_evidence_never_removes_an_item_from_the_budget(contracts):
    """Evidence reorders. Skipping would be trusting a claim Crystal did not
    establish."""
    resolution = _groups(contracts, ("Vault.deposit", "Vault.donate"))
    question = ask(affected_surface=(Surface("function", "Vault.deposit"),
                                     Surface("function", "Vault.donate")),
                   prior_evidence=(evidence("E1", "supports", ["Vault.deposit"]),))
    steering = steer(question, resolution, contracts)
    assert {label for label, _ in steering.order} == {
        "function:Vault.deposit", "function:Vault.donate"}

    hypotheses = [H(("Vault.deposit",)), H(("Vault.donate",))]
    allocation = schedule_sequences(hypotheses, budget=2, focus=steering.order)
    assert {item["item"]: item["executed"] for item in allocation.focus} == {
        "function:Vault.donate": 1, "function:Vault.deposit": 1}


def test_mutation_l_evidence_that_skips_a_supported_item_is_caught(contracts, monkeypatch):
    from crystal.question import steering as steering_module

    resolution = _groups(contracts, ("Vault.deposit", "Vault.donate"))
    question = ask(affected_surface=(Surface("function", "Vault.deposit"),
                                     Surface("function", "Vault.donate")),
                   prior_evidence=(evidence("E1", "supports", ["Vault.deposit"]),))
    real = steering_module.steer

    def skipping(q, r, c):
        result = real(q, r, c)
        result.order = [g for g in result.order
                        if g[0] != "function:Vault.deposit"]
        return result

    monkeypatch.setattr(steering_module, "steer", skipping)
    assert len(steering_module.steer(question, resolution, contracts).order) == 1

    monkeypatch.undo()
    assert len(steering_module.steer(question, resolution, contracts).order) == 2


def test_unattributable_or_unplaced_evidence_does_not_steer(contracts):
    resolution = _groups(contracts, ("Vault.deposit", "Vault.donate"))
    question = ask(affected_surface=(Surface("function", "Vault.deposit"),
                                     Surface("function", "Vault.donate")),
                   prior_evidence=(
                       evidence("E1", "refutes", ["Vault.donate"], attributable=False),
                       PriorEvidence("E2", "free text only", "refutes",
                                     {"producer": "x", "revision": "y"}),
                   ))
    steering = steer(question, resolution, contracts)
    assert [label for label, _ in steering.order] == [
        "function:Vault.deposit", "function:Vault.donate"]
    reasons = {item["evidence_id"]: item["reason"] for item in steering.not_steering}
    assert "cannot be attributed" in reasons["E1"]
    assert "does not say which part" in reasons["E2"]


# -- schema 1.1 and identity ---------------------------------------------------

@pytest.mark.invariant
def test_a_document_written_by_build_020_still_loads_and_keeps_its_old_id():
    """§42: existing questions must stay readable across the MINOR bump."""
    raw = (FIXTURES / "question_build020.json").read_text(encoding="utf-8")
    recorded = json.loads(raw)["question_id"]
    loaded = loads(raw)
    assert loaded.schema_version == "1.0"
    assert loaded.provenance["legacy_question_id"] == recorded
    assert loads(dumps(loaded)).question_id == loaded.question_id


@pytest.mark.invariant
def test_a_minor_version_does_not_change_what_a_question_is():
    """Build 020 hashed the full version, so every MINOR bump would have
    reissued every question's identity."""
    assert ask(schema_version="1.0").question_id == ask(schema_version="1.1").question_id
    assert ask(schema_version="1.0").question_id != ask(schema_version="2.0").question_id


def test_mutation_o_hashing_the_minor_again_is_caught(monkeypatch):
    original = ResearchQuestion.semantic_content

    def with_minor(self):
        content = original(self)
        content["schema_version"] = self.schema_version
        return content

    monkeypatch.setattr(ResearchQuestion, "semantic_content", with_minor)
    assert ask(schema_version="1.0").question_id != ask(schema_version="1.1").question_id

    monkeypatch.undo()
    assert ask(schema_version="1.0").question_id == ask(schema_version="1.1").question_id


def test_evidence_about_round_trips_and_is_part_of_identity():
    placed = ask(prior_evidence=(evidence("E1", "refutes", ["Vault.donate"]),))
    loose = ask(prior_evidence=(PriorEvidence(
        "E1", "a prior claim", "refutes", {"producer": "crystal", "revision": "abc"}),))
    assert placed.question_id != loose.question_id
    back = loads(dumps(placed))
    assert back.prior_evidence[0].about == (Surface("function", "Vault.donate"),)


# -- campaigns: nothing to find versus not reached -----------------------------

def _campaign(**scope):
    return CampaignDefinition("probe", "probe", "probe", scope=CampaignScope(**scope))


def test_a_campaign_whose_premise_is_not_in_the_target_is_absent(contracts):
    result = run_campaign(_campaign(allowed_categories=["nonce"]),
                          {"contracts": contracts, "state_deltas": []})
    assert result.surface == "absent"
    assert "nonce" in result.surface_reason


def test_a_campaign_whose_premise_exists_but_was_not_reached_says_so(contracts):
    result = run_campaign(_campaign(allowed_categories=["ownership"]),
                          {"contracts": contracts, "state_deltas": []})
    assert result.surface == "unreached"
    assert "no state delta executed" in result.surface_reason


def test_a_campaign_scoped_to_a_missing_contract_is_absent(contracts):
    result = run_campaign(_campaign(allowed_contracts=["Nope"]),
                          {"contracts": contracts, "state_deltas": []})
    assert result.surface == "absent"
