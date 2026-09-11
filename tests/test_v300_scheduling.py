"""Build 018 — how the symbolic budget is spent, and what is said about the rest.

Measured on the Lido stonks protocol, 2026-09-11. `derive_state_deltas`
executes a bounded number of sequence hypotheses; there were 198 and the budget
is 150. The 48 that did not run were dropped silently, and **175 of the 198
carried the same score, 0.720** — so the budget boundary fell deep inside a tie
and the decision was made by the tiebreak: sequence length, then lexicographic
order.

`Stonks.constructor -> Order.initialize -> Order.isValidSignature` was discarded
because `S` sorts late. 22 of the 48 discarded sequences already carried a
detector signal on their path.

Scores are not touched by any of this. The tests below pin that: the ranking
still decides, and demand only decides what the ranking could not.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from crystal.engine import research
from crystal.scheduling import Demand, demand_for, schedule_sequences
from crystal.scheduling.demand import _signalled_functions, _writes_by_function


@dataclass(frozen=True)
class Hypothesis:
    """Stands in for a sequence hypothesis; the scheduler reads these two."""

    sequence: tuple[str, ...]
    score: float


@dataclass(frozen=True)
class Signal:
    contract: str
    function: str


def hypotheses(*specs):
    return [Hypothesis(tuple(seq), score) for seq, score in specs]


# -- the score still ranks -----------------------------------------------------

def test_demand_never_promotes_a_sequence_past_a_better_scored_one():
    """Re-ranking on demand would be scoring the ranking twice.

    The low-scored sequence here has every demand signal and the high-scored
    one has none; the ranking must still put the high-scored one first.
    """
    wanted = Hypothesis(("A.f", "B.g"), 0.10)      # cross-contract, signalled
    plain = Hypothesis(("A.h",), 0.90)             # nothing at all

    allocation = schedule_sequences(
        [wanted, plain], budget=1,
        detectors=[Signal("A", "f")],
        contracts=(),
    )
    assert allocation.executed == [plain]


def test_ordering_is_stable_when_scores_separate_cleanly():
    ordered = schedule_sequences(
        hypotheses((("A.a",), 0.9), (("B.b",), 0.5), (("C.c",), 0.1)),
        budget=3,
    ).executed
    assert [h.score for h in ordered] == [0.9, 0.5, 0.1]


# -- and demand decides what the score could not -------------------------------

def test_inside_a_tie_a_sequence_carrying_evidence_wins():
    with_signal = Hypothesis(("A.f", "A.g"), 0.72)
    without = Hypothesis(("A.h", "A.i"), 0.72)

    allocation = schedule_sequences(
        [without, with_signal], budget=1,
        detectors=[Signal("A", "f")],
        contracts=(),
    )
    assert allocation.executed == [with_signal]


def test_inside_a_tie_a_cross_contract_chain_wins():
    spanning = Hypothesis(("A.f", "B.g"), 0.72)
    single = Hypothesis(("A.h", "A.i"), 0.72)

    allocation = schedule_sequences([single, spanning], budget=1)
    assert allocation.executed == [spanning]


def test_a_tie_the_demand_cannot_separate_stays_deterministic():
    """Two sequences alike in every signal must not reorder between runs."""
    pair = hypotheses((("A.b", "A.c"), 0.72), (("A.a", "A.d"), 0.72))
    first = schedule_sequences(pair, budget=2).executed
    second = schedule_sequences(list(reversed(pair)), budget=2).executed
    assert [h.sequence for h in first] == [h.sequence for h in second]


# -- the relational guarantee --------------------------------------------------

STRUCTURE = """
pragma solidity ^0.8.0;
contract {alpha} {{
    uint256 public total;
    uint256 public shares;
    address public keeper;
    function {open}(uint256 a) external {{ total += a; shares += a; }}
    function {skew}(uint256 a) external {{ total += a; }}
    function {peek}() external view returns (uint256) {{ return total; }}
}}
contract {beta} {{
    {alpha} public inner;
    uint256 public mirror;
    function {sync}(uint256 a) external {{ mirror += a; inner.{open}(a); }}
}}
"""

NAMED = STRUCTURE.format(alpha="Vault", beta="Router", open="deposit",
                         skew="donate", peek="totalAssets", sync="route")
RENAMED = STRUCTURE.format(alpha="Zzz", beta="Aaa", open="qqq",
                           skew="rrr", peek="sss", sync="ttt")

RENAMING = {"Vault": "Zzz", "Router": "Aaa", "deposit": "qqq",
            "donate": "rrr", "totalAssets": "sss", "route": "ttt"}


def _translate(name: str) -> str:
    for original, renamed in RENAMING.items():
        name = name.replace(original, renamed)
    return name


@pytest.mark.invariant
def test_the_allocation_does_not_change_when_everything_is_renamed(tmp_path):
    """The discriminant is relational, so spelling must not move the boundary.

    This is the constraint Build 012 set and Build 016 applied to the protocol
    layer. The sequence budget was the last place still deciding by spelling:
    with the names reversed alphabetically, the old lexicographic tiebreak
    would hand out a different 150.
    """
    named_dir, renamed_dir = tmp_path / "named", tmp_path / "renamed"
    named_dir.mkdir()
    renamed_dir.mkdir()
    (named_dir / "T.sol").write_text(NAMED, encoding="utf-8")
    (renamed_dir / "T.sol").write_text(RENAMED, encoding="utf-8")

    a = research(named_dir, use_solc=False, use_foundry=False)
    b = research(renamed_dir, use_solc=False, use_foundry=False)

    translated = {
        tuple(_translate(step) for step in delta.sequence)
        for delta in a["state_deltas"]
    }
    actual = {tuple(delta.sequence) for delta in b["state_deltas"]}
    assert translated == actual


# -- nothing is discarded in silence -------------------------------------------

@pytest.mark.invariant
def test_what_the_budget_did_not_reach_is_reported_with_a_reason():
    """48 hypotheses used to vanish without a line of output."""
    allocation = schedule_sequences(
        hypotheses(*[((f"A.f{i}",), 0.5) for i in range(10)]), budget=4,
    )
    report = allocation.report()
    assert report["executed"] == 4
    assert report["deferred"] == 6
    assert len(report["deferred_detail"]) == 6
    for item in report["deferred_detail"]:
        assert item["sequence"] and item["reasons"]


def test_the_report_says_how_wide_the_tie_at_the_boundary_was():
    """A wide tie means the score is not ranking and the tiebreak is.

    A reader deciding whether to trust the cut needs that number: on stonks it
    is 175 of 198.
    """
    allocation = schedule_sequences(
        hypotheses(*[((f"A.f{i}",), 0.72) for i in range(20)]), budget=5,
    )
    assert allocation.boundary_score == pytest.approx(0.72)
    assert allocation.tied_at_boundary == 20


def test_the_report_does_not_offer_a_bigger_budget_as_the_fix():
    """Throughput is the axis Crystal deliberately does not compete on."""
    note = schedule_sequences(hypotheses((("A.f",), 0.5)), budget=0).report()["note"]
    assert "Raising the budget is not the intended fix" in note


def test_the_budget_is_honoured_exactly():
    allocation = schedule_sequences(
        hypotheses(*[((f"A.f{i}",), 0.5) for i in range(300)]), budget=150,
    )
    assert len(allocation.executed) == 150
    assert len(allocation.deferred) == 150


# -- the demand signals themselves ---------------------------------------------

def test_demand_reads_only_structure():
    writes = {"A.f": ("total",), "A.g": ()}
    signalled = {"A.f"}

    mutating_and_signalled = demand_for(("A.f",), signalled=signalled, writes=writes)
    inert = demand_for(("A.g",), signalled=set(), writes=writes)

    assert mutating_and_signalled.signalled == 1
    assert mutating_and_signalled.mutating == 1
    assert mutating_and_signalled.score > inert.score
    assert inert.score == 0.0


def test_a_sequence_that_writes_nothing_cannot_produce_a_delta():
    """A chain of pure getters is a provably empty slot."""
    inert = demand_for(("A.view1", "A.view2"), signalled=set(), writes={})
    assert inert.mutating == 0
    assert "writes no state" in " ".join(inert.reasons())


def test_repeated_signals_on_one_path_are_not_double_counted():
    """Two signals on the same chain are the same chain, not twice the evidence."""
    one = Demand(signalled=1, cross_contract=True, mutating=1)
    many = Demand(signalled=5, cross_contract=True, mutating=3)
    assert one.score == many.score == 1.0


def test_signal_helpers_tolerate_an_empty_pipeline():
    assert _signalled_functions(()) == set()
    assert _writes_by_function(()) == {}
    assert schedule_sequences([], budget=10).executed == []


# -- end to end ----------------------------------------------------------------

def test_a_scan_reports_its_sequence_budget(tmp_path):
    (tmp_path / "T.sol").write_text(NAMED, encoding="utf-8")
    result = research(tmp_path, use_solc=False, use_foundry=False)
    budget = result["sequence_budget"]
    assert budget["budget"] == 150
    assert budget["executed"] == len(result["state_deltas"])
    assert budget["considered"] >= budget["executed"]
