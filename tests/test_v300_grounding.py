"""Build 016 — a protocol claim rests on a relation, not on a name.

Measured on the Lido stonks protocol, 2026-09-07: the previous generators
produced 52 protocol invariants, 27 of them carrying `heuristic:function-name`
as their only evidence. All 21 "fee" invariants matched the substring `fee`
inside the word `Feed` — `getFeed`, `setTokenFeed`, `isFeedInSync`. Not one was
about a fee.

Every test here pins a reason rather than a count, and each runs on both parser
front-ends where the front-ends differ, because the first version of the
quantity predicate keyed off a tree-sitter expression kind and let every
counter through on the regex path.
"""

from __future__ import annotations

import pytest

from crystal.backends.base import derive_properties
from crystal.engine import research
from crystal.parsers import solidity_regex
from crystal.protocol.grounding import (
    co_movements,
    configurable_state,
    monotonic_variables,
    price_reads,
    scaled_amounts,
    state_writes,
)
from crystal.protocol.invariants import derive_protocol_invariants
from crystal.protocol.model import build_protocol_model

FEED = """
interface IFeed {
    function latestRoundData() external view returns (uint80,int256,uint256,uint256,uint80);
}
"""

ORACLE = f"""
pragma solidity ^0.8.20;
{FEED}
contract Router {{
    address feed;
    uint256 public price;
    uint256 public maxAge;

    // Reads its own storage. Named like a price source and not one.
    function getPrice() external view returns (uint256) {{
        return price;
    }}

    // Sets a threshold. Reads no feed at all.
    function setPriceTolerance(uint256 bps) external {{
        maxAge = bps;
    }}

    function unguarded() external view returns (uint256) {{
        (, int256 answer,,,) = IFeed(feed).latestRoundData();
        return uint256(answer);
    }}

    function guarded() external view returns (uint256) {{
        (, int256 answer,, uint256 updatedAt,) = IFeed(feed).latestRoundData();
        if (block.timestamp - updatedAt > maxAge) revert();
        return uint256(answer);
    }}
}}
"""

LEDGER = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public nonce;
    mapping(address => uint256) public balances;

    function deposit(uint256 assets) external {
        totalAssets += assets;
        totalSupply += assets;
        balances[msg.sender] += assets;
        nonce += 1;
    }
    function donate(uint256 assets) external {
        totalAssets += assets;
    }
    function withdraw(uint256 assets) external {
        totalAssets -= assets;
        totalSupply -= assets;
        balances[msg.sender] -= assets;
    }
}
"""

SCALING = """
pragma solidity ^0.8.20;
library Math { function mulDiv(uint256 a, uint256 b, uint256 c) internal pure returns (uint256) { return a * b / c; } }
contract Desk {
    uint256 public constant MAX_BASIS_POINTS = 1e4;
    uint256 public immutable MARGIN_BPS;
    uint256 public collected;
    bool public paused;
    address public receiver;

    constructor(uint256 margin) { MARGIN_BPS = margin; }

    function quote(uint256 amount) external view returns (uint256) {
        uint256 expected = amount * 2;
        return (expected * MARGIN_BPS) / MAX_BASIS_POINTS;
    }

    function prorate(uint256 amount, uint256 basis) external view returns (uint256) {
        return Math.mulDiv(collected, amount, basis);
    }

    function accumulate(uint256 amount) external {
        collected = collected + amount;
    }
}
"""


def solidity(source, tmp_path, name="T.sol"):
    (tmp_path / name).write_text(source, encoding="utf-8")
    return research(tmp_path, use_solc=False, use_foundry=False)


def contract(parsed, name):
    return next(c for c in parsed["contracts"] if c.name == name)


# -- a name is not a price source ---------------------------------------------

def test_a_getter_over_local_storage_is_not_an_oracle_read(tmp_path):
    parsed = solidity(ORACLE, tmp_path)
    functions = {(x.contract, x.function) for x in parsed["protocol_model"].oracle_signals}
    assert ("Router", "getPrice") not in functions
    assert ("Router", "setPriceTolerance") not in functions
    assert ("Router", "unguarded") in functions


def test_an_interface_declaration_yields_nothing(tmp_path):
    parsed = solidity(ORACLE, tmp_path)
    model = build_protocol_model(parsed["contracts"])
    assert all(x.contract != "IFeed" for x in model.oracle_signals)
    assert all(x.contract != "IFeed" for x in model.fee_signals)
    assert not price_reads(contract(parsed, "IFeed"))


def test_an_observed_freshness_guard_suppresses_the_invariant(tmp_path):
    """The source already asserts it. Repeating it back is not evidence."""
    parsed = solidity(ORACLE, tmp_path)
    signals = {x.function: x for x in parsed["protocol_model"].oracle_signals}
    assert signals["guarded"].freshness_checked is True
    assert signals["unguarded"].freshness_checked is False
    # The signal survives — nothing is silenced — but no invariant is raised.
    stated = [i for i in parsed["protocol_invariants"] if i.category == "oracle"]
    assert [i.expression.split()[0] for i in stated] == ["Router.unguarded"]


def test_a_guarded_read_scores_below_an_unguarded_one(tmp_path):
    parsed = solidity(ORACLE, tmp_path)
    signals = {x.function: x for x in parsed["protocol_model"].oracle_signals}
    assert signals["guarded"].confidence < signals["unguarded"].confidence


# -- a ledger is co-movement, not a name family -------------------------------

def test_two_totals_that_move_together_are_a_relation(tmp_path):
    parsed = solidity(LEDGER, tmp_path)
    pairs = {(a, b) for a, b, _ in co_movements(contract(parsed, "Vault"))}
    assert ("totalAssets", "totalSupply") in pairs


def test_a_counter_stepped_by_a_literal_is_not_part_of_a_ledger(tmp_path):
    parsed = solidity(LEDGER, tmp_path)
    pairs = {(a, b) for a, b, _ in co_movements(contract(parsed, "Vault"))}
    assert not any("nonce" in pair for pair in pairs)


def test_the_quantity_test_survives_the_regex_front_end():
    """The first version keyed off a tree-sitter expression kind.

    `nonce += 1` is `number_literal` under tree-sitter and `expression` under
    the regex parser, so a kind check passed on one path and let every counter
    into a ledger relation on the other.
    """
    parsed = solidity_regex.parse_text(LEDGER, "Vault.sol")
    vault = next(c for c in parsed if c.name == "Vault")
    by_variable = {w.variable: w.by_quantity for w in state_writes(vault)}
    assert by_variable["nonce"] is False
    assert by_variable["totalAssets"] is True
    assert not any("nonce" in pair for pair in
                   {(a, b) for a, b, _ in co_movements(vault)})


def test_a_mapping_is_not_paired_with_a_scalar_total(tmp_path):
    """Sum-of-balances against a total needs an enumerable holder set.

    `derive_properties` already refuses that. Raising it here under a name
    suggesting two scalars would be the approximation the refusal exists for.
    """
    parsed = solidity(LEDGER, tmp_path)
    pairs = {(a, b) for a, b, _ in co_movements(contract(parsed, "Vault"))}
    assert not any("balances" in pair for pair in pairs)


def test_an_asymmetric_path_does_not_dissolve_the_relation(tmp_path):
    """`donate` moves assets without shares. That is the finding, not a reason
    to drop the pair — an earlier rule required identical writer sets and hid
    exactly the case the relation exists to expose."""
    parsed = solidity(LEDGER, tmp_path)
    pairs = {(a, b) for a, b, _ in co_movements(contract(parsed, "Vault"))}
    assert ("totalAssets", "totalSupply") in pairs
    assert any(x.kind == "asset-share-asymmetry" for x in parsed["delta_anomalies"])


# -- scaling is multiplicative, and the scalar is configurable ----------------

def test_an_accumulator_is_not_a_scaling(tmp_path):
    parsed = solidity(SCALING, tmp_path)
    functions = {x.function for x in scaled_amounts(contract(parsed, "Desk"))}
    assert "accumulate" not in functions


def test_a_margin_applied_to_a_local_is_found(tmp_path):
    """Lido applies its margin to a local holding an earlier call's result.

    Requiring the scaled side to be a parameter missed it — and it is the whole
    formula the protocol's own fuzz model reimplements by hand.
    """
    parsed = solidity(SCALING, tmp_path)
    found = {(x.function, x.scalar, x.amount)
             for x in scaled_amounts(contract(parsed, "Desk"))}
    assert ("quote", "MARGIN_BPS", "expected") in found


def test_the_constant_denominator_is_not_the_scalar(tmp_path):
    """`MAX_BASIS_POINTS` is 10000 in every deployment that will ever exist."""
    parsed = solidity(SCALING, tmp_path)
    assert "MAX_BASIS_POINTS" not in configurable_state(contract(parsed, "Desk"))
    assert "MARGIN_BPS" in configurable_state(contract(parsed, "Desk"))


def test_a_non_numeric_state_variable_scales_nothing(tmp_path):
    parsed = solidity(SCALING, tmp_path)
    configurable = configurable_state(contract(parsed, "Desk"))
    assert "paused" not in configurable
    assert "receiver" not in configurable


def test_a_muldiv_call_is_the_same_relation_written_as_a_call(tmp_path):
    parsed = solidity(SCALING, tmp_path)
    found = {(x.function, x.scalar) for x in scaled_amounts(contract(parsed, "Desk"))}
    assert ("prorate", "collected") in found


# -- monotonicity from the writes --------------------------------------------

def test_monotonicity_is_earned_from_the_operator(tmp_path):
    parsed = solidity(LEDGER, tmp_path)
    monotone = monotonic_variables(contract(parsed, "Vault"))
    assert "nonce" in monotone
    assert "totalAssets" not in monotone   # `withdraw` decrements it


def test_a_property_names_the_writes_that_earned_it(tmp_path):
    parsed = solidity(LEDGER, tmp_path)
    properties, _ = derive_properties(
        contract(parsed, "Vault"), parsed["protocol_invariants"],
    )
    monotonic = next(p for p in properties if p.kind == "monotonicity")
    assert monotonic.subject == "nonce"
    assert "deposit" in monotonic.comment


# -- no invariant may rest on a name -----------------------------------------

@pytest.mark.parametrize("source", [ORACLE, LEDGER, SCALING])
def test_no_invariant_cites_a_name_match_as_its_evidence(source, tmp_path):
    parsed = solidity(source, tmp_path)
    for invariant in parsed["protocol_invariants"]:
        for line in invariant.evidence:
            assert not line.startswith("heuristic:function-name")
            assert not line.startswith("state-name-family:")


def test_every_invariant_cites_a_line_or_a_signature(tmp_path):
    """Evidence a reader cannot go and look at is not evidence."""
    parsed = solidity(ORACLE + "\n", tmp_path)
    parsed = solidity(SCALING, tmp_path, name="S.sol")
    for invariant in parsed["protocol_invariants"]:
        joined = " ".join(invariant.evidence)
        assert "line " in joined or "signature:" in joined or "convention:" in joined


def test_derive_protocol_invariants_accepts_the_model_it_is_given(tmp_path):
    parsed = solidity(SCALING, tmp_path)
    model = build_protocol_model(parsed["contracts"])
    assert derive_protocol_invariants(model)
