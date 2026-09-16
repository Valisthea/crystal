"""Build 019 — one canonical proposal, carrying what its producers already knew.

`ResearchCandidate` is the record a parent system reads, and until this build it
was the thinnest record in the pipeline. Detector signals carry a
`FALSIFICATION` tuple, `EvidenceRecord` carries `limitations` and `provenance`,
and `models.Hypothesis` carries a `rationale` — the candidate carried none of
them, and the rationale was computed and then dropped by the constructor.

Two rules are pinned here, and they are the same rule twice: nothing is
invented, and falsification is cited rather than composed. A generic sentence in
the falsification slot would be worse than the gap, because a reader would stop
looking for the real one.

The round-trip test spawns a **separate process** on purpose. Asserting that a
dataclass survives `asdict` proves serialisation, not persistence; the roadmap
asks whether a later process can read Crystal's evidence and learn what would
falsify it, and only a second interpreter answers that.
"""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import asdict

import pytest

from crystal.engine import research
from crystal.provenance import (
    configuration_digest,
    merge_missing,
    producer_provenance,
    source_digest,
)
from crystal.quality.triage import EXPECTED, ResearchCandidate, build_candidates

VAULT = """
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


@pytest.fixture
def scanned(tmp_path):
    (tmp_path / "Vault.sol").write_text(VAULT, encoding="utf-8")
    return research(tmp_path, use_solc=False, use_foundry=False)


# -- nothing is invented -------------------------------------------------------

@pytest.mark.invariant
def test_a_producer_that_supplies_no_falsification_leaves_it_empty(scanned):
    """A generic sentence here would stop a reader looking for the real one."""
    for candidate in scanned["research_candidates"]:
        if candidate.falsification:
            continue
        assert "falsification" in candidate.missing


@pytest.mark.invariant
def test_missing_names_exactly_the_fields_that_are_absent(scanned):
    for candidate in scanned["research_candidates"]:
        present = {
            "rationale": bool(candidate.rationale),
            "falsification": bool(candidate.falsification),
            "assumptions": bool(candidate.assumptions),
            "limitations": bool(candidate.limitations),
        }
        assert candidate.missing == [f for f in EXPECTED if not present[f]]


def test_falsification_is_cited_from_a_detector_never_composed_here(scanned):
    """Composing one at this layer would be protocol interpretation."""
    cited = [c for c in scanned["research_candidates"] if c.falsification]
    assert cited, "the fixture should trip at least one detector"
    for candidate in cited:
        for question in candidate.falsification:
            assert question.startswith("["), question
            assert " on " in question.split("]")[0]
        assert any("inherits the premise of" in a for a in candidate.assumptions)


def test_the_rationale_producers_compute_is_no_longer_discarded(scanned):
    from_core = [c for c in scanned["research_candidates"]
                 if c.source == "core-hypothesis"]
    assert from_core
    assert all(c.rationale for c in from_core)


# -- identity is unchanged by any of this --------------------------------------

@pytest.mark.invariant
def test_the_new_fields_do_not_change_a_candidate_identity():
    """Adding a field to the identity material would silently reissue every id."""
    plain = {
        "hypotheses": [], "sequence_hypotheses": [], "protocol_invariants": [],
        "detectors": (),
    }

    class Inv:
        category, expression, confidence, evidence = "acc", "x <-> y", 0.5, ["e"]

    bare = build_candidates(dict(plain, protocol_invariants=[Inv()]))
    enriched = build_candidates(dict(
        plain, protocol_invariants=[Inv()],
        producer_provenance={"producer": "crystal", "source_digest": "abc"},
    ))
    assert [c.id for c in bare] == [c.id for c in enriched]
    assert enriched[0].provenance and not bare[0].provenance


def test_a_candidate_built_without_the_new_fields_is_still_valid():
    """Backward compatibility: an older caller's positional construction."""
    candidate = ResearchCandidate(
        "id0", "CAT", "title", 0.5, True, ["e"], ["A.f"], "legacy",
    )
    assert candidate.falsification == []
    assert candidate.falsifiable is False


# -- survives a process restart ------------------------------------------------

READER = """
import json, sys
payload = json.load(open(sys.argv[1], encoding="utf-8"))
candidates = payload["research_candidates"]
carrying = [c for c in candidates if c["falsification"]]
print(json.dumps({
    "total": len(candidates),
    "with_falsification": len(carrying),
    "sample": carrying[0] if carrying else None,
    "provenance": payload["producer_provenance"],
}))
"""


@pytest.mark.invariant
def test_falsification_and_provenance_survive_a_process_restart(scanned, tmp_path):
    """A later process must be able to read what would falsify a proposal.

    Spawned rather than round-tripped in memory: `asdict` proving reversible
    says nothing about whether the fields reached the file a parent system
    actually reads.
    """
    from crystal.report import payload

    written = tmp_path / "scan.json"
    written.write_text(json.dumps(payload(scanned), default=list), encoding="utf-8")
    reader = tmp_path / "reader.py"
    reader.write_text(READER, encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, str(reader), str(written)],
        capture_output=True, text=True, timeout=120, encoding="utf-8",
    )
    assert completed.returncode == 0, completed.stderr
    seen = json.loads(completed.stdout)

    assert seen["total"] > 0
    assert seen["with_falsification"] > 0
    assert seen["sample"]["falsification"]
    assert seen["sample"]["assumptions"]
    assert seen["sample"]["rationale"]
    assert seen["provenance"]["source_digest"]


def test_limitations_survive_serialisation_when_a_producer_supplies_them():
    candidate = ResearchCandidate(
        "id0", "SEQUENCE", "t", 0.5, True, ["e"], ["A.f"], "sequence-engine",
        limitations=["depth beyond the default was justified"],
    )
    assert asdict(candidate)["limitations"] == [
        "depth beyond the default was justified"
    ]
    assert "limitations" not in candidate.missing


# -- provenance ----------------------------------------------------------------

@pytest.mark.invariant
def test_provenance_carries_no_clock(tmp_path):
    """A timestamp would make every run differ and determinism checks noise."""
    (tmp_path / "V.sol").write_text(VAULT, encoding="utf-8")
    first = research(tmp_path, use_solc=False, use_foundry=False)
    second = research(tmp_path, use_solc=False, use_foundry=False)
    assert first["producer_provenance"] == second["producer_provenance"]

    rendered = json.dumps(first["producer_provenance"])
    for forbidden in ("timestamp", "generated_at", "run_at", "date"):
        assert forbidden not in rendered


def test_the_source_digest_is_content_not_modification_time(tmp_path):
    source = tmp_path / "V.sol"
    source.write_text(VAULT, encoding="utf-8")
    before, count = source_digest([source], tmp_path)
    assert count == 1

    source.touch()
    after, _ = source_digest([source], tmp_path)
    assert after == before, "touching a file must not change its digest"

    source.write_text(VAULT + "\n// changed\n", encoding="utf-8")
    changed, _ = source_digest([source], tmp_path)
    assert changed != before


def test_the_source_digest_does_not_leak_the_operators_directory_layout(tmp_path):
    """Digested relative to the project root: an absolute path is a fact about
    a laptop, not about the code."""
    one, two = tmp_path / "a", tmp_path / "b"
    one.mkdir()
    two.mkdir()
    (one / "V.sol").write_text(VAULT, encoding="utf-8")
    (two / "V.sol").write_text(VAULT, encoding="utf-8")
    assert source_digest([one / "V.sol"], one) == source_digest([two / "V.sol"], two)


def test_configuration_digest_separates_runs_that_were_configured_differently():
    a = configuration_digest({"use_solc": True, "include_tests": False})
    b = configuration_digest({"use_solc": False, "include_tests": False})
    assert a != b
    assert a == configuration_digest({"include_tests": False, "use_solc": True})


def test_an_unprobed_tool_is_absent_rather_than_recorded_unavailable(tmp_path):
    """Those are different claims, and only the first is true."""
    stamp = producer_provenance(
        project=tmp_path, sources=(), configuration={}, tools={"forge": "1.6.0"},
    )
    assert stamp["tool_versions"] == {"forge": "1.6.0"}
    assert "medusa" not in stamp["tool_versions"]


@pytest.mark.invariant
def test_stamping_a_legacy_record_never_overwrites_what_it_already_had():
    """Replacing a historical value with today's is the failure this guards."""
    historical = {"producer_build": "007", "source_digest": "old"}
    stamp = {"producer_build": "019", "source_digest": "new", "producer": "crystal"}

    merged = merge_missing(historical, stamp)
    assert merged["producer_build"] == "007"
    assert merged["source_digest"] == "old"
    assert merged["producer"] == "crystal"
    assert merged["filled"] == ["producer"]
    assert merged["provenance_origin"] == "partially-stamped-at-read"


def test_a_record_with_no_provenance_is_marked_as_stamped_at_read():
    merged = merge_missing({}, {"producer": "crystal", "producer_build": "019"})
    assert merged["provenance_origin"] == "stamped-at-read"
    assert merged["filled"] == ["producer", "producer_build"]


def test_a_complete_record_is_returned_untouched():
    record = {"producer": "crystal", "producer_build": "007"}
    assert merge_missing(record, {"producer": "x", "producer_build": "y"}) == record


# -- the ranker that ranked nothing --------------------------------------------

def test_the_order_of_result_hypotheses_is_not_an_output_contract(scanned):
    """`crystal/ranking.py` sorted these and `build_candidates` re-sorted them.

    Removing it in Build 019 was safe because its only consumer discarded the
    ordering; this pins that it stays safe.
    """
    shuffled = dict(scanned, hypotheses=list(reversed(scanned["hypotheses"])))
    assert [c.id for c in build_candidates(shuffled)] == [
        c.id for c in scanned["research_candidates"]
    ]


def test_crystal_no_longer_ships_a_ranking_module():
    with pytest.raises(ImportError):
        __import__("crystal.ranking")
