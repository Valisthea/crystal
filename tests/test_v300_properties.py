"""Campaign invariants compile into executable properties, or refuse precisely.

`validate --backend halmos` on the flyover target returned 27 UNSUPPORTED and 1
EXECUTED with "no property could be derived without fabricating protocol
assumptions". That was correct while nothing carried a property. Since Build
010 a pack carries `CampaignInvariant`s; this layer turns them into Foundry,
Medusa and Halmos harness source, and turns what it cannot express exactly into
an UNSUPPORTED that names the fragment it could not bind.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from crystal.campaigns.definition import CampaignDefinition, CampaignInvariant, CampaignScope
from crystal.parsers import parse_project
from crystal.properties import (
    BACKENDS,
    Actor,
    Fixture,
    Instance,
    Recipe,
    Resolver,
    SetupCall,
    compile_properties,
    parse_invariant,
    property_name,
    split_report,
    write_compiled,
)
from crystal.properties.harness import ledger_name
from crystal.properties.ir import Exclusion, Independence, Relation, Replay, Unsupported

PROTOCOL = """
pragma solidity ^0.8.20;

interface ILedger {
    error Short(uint256 have);
    function credit(address who) external view returns (uint256);
    function debit(address who, uint256 amount) external;
}

contract Ledger is ILedger {
    enum Stage { NONE, OPEN, DONE }
    mapping(address => uint256) private _credit;
    uint256 private _fees;
    mapping(bytes32 => Stage) private _stages;

    function credit(address who) external view returns (uint256) { return _credit[who]; }
    function fees() external view returns (uint256) { return _fees; }
    function stage(bytes32 id) external view returns (Stage) { return _stages[id]; }
    function fund(address who) external payable { _credit[who] += msg.value; }
    function debit(address who, uint256 amount) external { _credit[who] -= amount; _fees += amount; }
    function open(bytes32 id) external { require(_stages[id] == Stage.NONE, "open"); _stages[id] = Stage.OPEN; }
    function close(bytes32 id) external { require(_stages[id] == Stage.OPEN, "close"); _stages[id] = Stage.DONE; }
}

contract Settlement {
    ILedger private _ledger;
    mapping(bytes32 => bool) private _done;
    constructor(address ledger) { _ledger = ILedger(ledger); }
    function settle(bytes32 id, address who, uint256 fee) external {
        uint256 have = _ledger.credit(who);
        if (have < fee) revert ILedger.Short(have);
        _done[id] = true;
        _ledger.debit(who, fee);
    }
    function cancel(bytes32 id, address who) external { _done[id] = true; _ledger.debit(who, 0); }
    function done(bytes32 id) external view returns (bool) { return _done[id]; }
}

contract Computed {
    mapping(address => uint256) private _bal;
    function bal(address a) external view returns (uint256) { return _bal[a]; }
    function skim() external { _bal[address(uint160(uint256(keccak256(abi.encode(msg.sender)))))] = 1; }
}

contract Hidden {
    mapping(address => uint256) private _secret;
    uint256 private _total;
    function total() external view returns (uint256) { return _total; }
    function poke(address a) external { _secret[a] += 1; _total += 1; }
}
"""

LBC = Path(os.environ.get(
    "CRYSTAL_FLYOVER_LBC", "C:/Users/admin/Desktop/Vercel Sandbox/rootstock/lbc"
))
FLYOVER_PACK = LBC.parent / "flyover-pack.py"


@pytest.fixture(scope="module")
def protocol(tmp_path_factory):
    root = tmp_path_factory.mktemp("protocol")
    (root / "src").mkdir()
    path = root / "src" / "Protocol.sol"
    path.write_text(PROTOCOL, encoding="utf-8")
    contracts = parse_project([path]).contracts
    return root, contracts


def resolver(contracts, *names):
    return Resolver(contracts, scope=set(names) or None)


def invariant(statement, category="conservation", affected=(), policy="exact"):
    return CampaignInvariant(statement, category, list(affected), policy)


def fixture_for(root) -> Fixture:
    return Fixture(
        project_root=str(root),
        name="protocol",
        instances=[
            Instance("Ledger", "ledger", "src/Protocol.sol"),
            Instance("Settlement", "settlement", "src/Protocol.sol", ctor_args=("address(ledger)",)),
        ],
        actors=[Actor("alice", 0xA11CE), Actor("bob", 0xB0B)],
        setup=[SetupCall("_vm.deal(address(this), 1 ether);")],
    )


# ── statement grammar ────────────────────────────────────────────────────

def test_relation_binds_sums_scalars_and_the_balance(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("sum(_credit) + _fees equals the contract ether balance", affected=["_credit", "_fees"]),
        resolver(contracts, "Ledger"),
    )
    form = parsed.form
    assert isinstance(form, Relation), form.describe()
    assert form.op == "=="
    assert [t.kind for t in form.lhs] == ["sum", "scalar"]
    assert form.rhs[0].kind == "balance" and form.rhs[0].contract == "Ledger"
    assert form.lhs[0].state.qualified == "Ledger::_credit"
    assert form.lhs[0].state.getter == "credit"
    assert form.lhs[1].state.getter == "fees"
    assert any("enumerate the holders" in note for note in parsed.notes)


def test_relation_refuses_a_term_it_cannot_bind(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("contract balance is at least the sum of open positions plus sum(_credit)",
                  category="solvency", affected=["_credit"], policy="lower_bound"),
        resolver(contracts, "Ledger"),
    )
    assert isinstance(parsed.form, Unsupported)
    assert "open positions" in parsed.form.reason
    assert "will not infer" in parsed.form.reason


def test_sum_refuses_a_mapping_with_a_computed_key(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("sum(_bal) equals the contract ether balance", affected=["_bal"]),
        resolver(contracts, "Computed"),
    )
    assert isinstance(parsed.form, Unsupported)
    assert "computed" in parsed.form.reason
    assert "Computed.skim" in parsed.form.reason


def test_state_without_an_accessor_is_refused_not_read_from_storage(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("sum(_secret) equals _total", affected=["_secret", "_total"]),
        resolver(contracts, "Hidden"),
    )
    assert isinstance(parsed.form, Unsupported)
    assert "Hidden::_secret" in parsed.form.reason
    assert "no public getter" in parsed.form.reason


def test_exclusion_binds_functions_and_their_key(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("for one id, at most one of settle and cancel can succeed",
                  category="settlement", affected=["_done"]),
        resolver(contracts, "Settlement", "Ledger"),
    )
    form = parsed.form
    assert isinstance(form, Exclusion), form.describe()
    assert form.key == "id"
    assert [f.qualified for f in form.functions] == ["Settlement.settle", "Settlement.cancel"]


def test_exclusion_refuses_an_unknown_entry_point(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("for one id, at most one of settle and vanish can succeed",
                  category="settlement", affected=["_done"]),
        resolver(contracts, "Settlement", "Ledger"),
    )
    assert isinstance(parsed.form, Unsupported)
    assert "vanish" in parsed.form.reason


def test_replay_binds_the_enum_value_and_the_once_clause(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("no id reaches DONE twice and close cannot run twice for one id",
                  category="replay", affected=["_stages"]),
        resolver(contracts, "Ledger"),
    )
    form = parsed.form
    assert isinstance(form, Replay), form.describe()
    assert form.key == "id"
    assert form.reach is not None and form.reach.qualified == "Ledger::_stages"
    assert form.reach_value == "DONE"
    assert [f.name for f in form.reach_writers] == ["close"]
    assert [f.name for f in form.once] == ["close"]


def test_independence_binds_settlement_to_the_bound_accounting_state(protocol):
    _, contracts = protocol
    res = resolver(contracts, "Settlement", "Ledger")
    parsed = parse_invariant(
        invariant("settlement of a proven payment never depends on the outcome of penalty accounting",
                  category="settlement", affected=["_credit", "_done"]),
        res,
    )
    form = parsed.form
    # `revert ILedger.Short(have)` names its owner in the source, so the guard
    # binds under both front-ends, whether or not error declarations were parsed.
    assert isinstance(form, Independence), form.describe()
    assert form.state.qualified == "Ledger::_credit"
    assert sorted(f.name for f in form.functions) == ["cancel", "settle"]
    assert len(form.guards) == 1
    guard = form.guards[0]
    assert guard.function == "Settlement.settle"
    assert guard.error_owner == "ILedger" and guard.error_name == "Short"
    assert "have < fee" in guard.condition
    assert form.arithmetic_on_state  # Ledger.debit does `_credit[who] -= amount`
    assert any("of a proven payment" in note and "not bound" in note for note in parsed.notes)


def test_unrecognised_statement_is_refused_with_the_text(protocol):
    _, contracts = protocol
    parsed = parse_invariant(
        invariant("the protocol should generally be safe", category="safety"),
        resolver(contracts, "Ledger"),
    )
    assert isinstance(parsed.form, Unsupported)
    assert "the protocol should generally be safe" in parsed.form.reason


# ── compilation ──────────────────────────────────────────────────────────

def campaigns():
    scope = CampaignScope(allowed_contracts=["Ledger", "Settlement"])
    return [
        CampaignDefinition("proto-p1-conservation", "P1", "", "protocol", scope, invariants=[
            invariant("sum(_credit) + _fees equals the contract ether balance", affected=["_credit", "_fees"]),
        ]),
        CampaignDefinition("proto-p2-exclusion", "P2", "", "protocol", scope, invariants=[
            invariant("for one id, at most one of settle and cancel can succeed", "settlement", ["_done"]),
        ]),
        CampaignDefinition("proto-p3-solvency", "P3", "", "protocol", scope, invariants=[
            invariant("contract balance is at least the sum of open positions", "solvency", ["_credit"], "lower_bound"),
        ]),
        CampaignDefinition("proto-p4-replay", "P4", "", "protocol", scope, invariants=[
            invariant("no id reaches DONE twice and close cannot run twice for one id", "replay", ["_stages"]),
        ]),
    ]


def test_without_a_fixture_nothing_is_deployed_and_nothing_is_guessed(protocol):
    _, contracts = protocol
    compiled = compile_properties(campaigns(), contracts, None)
    assert len(compiled) == 4 * len(BACKENDS)
    assert all(not item.supported for item in compiled)
    p1 = [item for item in compiled if item.name == "P1"]
    assert all("fixture" in item.unsupported_reason for item in p1)
    p3 = [item for item in compiled if item.name == "P3"]
    assert all("open positions" in item.unsupported_reason for item in p3)


def test_compiles_every_expressible_invariant_for_all_three_backends(protocol):
    root, contracts = protocol
    compiled = compile_properties(campaigns(), contracts, fixture_for(root), pack="protocol")
    by_name = {}
    for item in compiled:
        by_name.setdefault(item.name, {})[item.backend] = item
    assert set(by_name) == {"P1", "P2", "P3", "P4"}
    for name in ("P1", "P2", "P4"):
        for backend in BACKENDS:
            item = by_name[name][backend]
            assert item.supported, item.unsupported_reason
            assert item.filename.startswith("test/crystal/")
            assert item.campaign_id.startswith("proto-")
            assert "Ledger.fund" in item.exercises and "Settlement.settle" in item.exercises
            assert "_crystal_call(" in item.source
    assert all(not by_name["P3"][b].supported for b in BACKENDS)

    foundry = by_name["P1"]["foundry"].source
    assert "contract CrystalFoundry_protocol_P1 is Test" in foundry
    assert "function invariant_P1() public view" in foundry
    assert "_crystal_lhs_P1() == _crystal_rhs_P1()" in foundry
    assert "ledger.credit(_holders[i])" in foundry
    assert "address(ledger).balance" in foundry
    assert "afterInvariant()" in foundry and "CRYSTAL_NONVACUOUS P1" in foundry
    assert "targetSelector(" in foundry
    assert "// Reads:" in foundry and "Ledger::_credit via credit(address)" in foundry
    assert "settlement = new Settlement(address(ledger));" in foundry

    medusa = by_name["P1"]["medusa"].source
    assert "constructor()" in medusa and "function setUp" not in medusa
    assert "function property_P1() public view returns (bool)" in medusa
    assert "forge-std" not in medusa
    assert "medusa.json" in by_name["P1"]["medusa"].extra_files
    assert '"useSlither": false' in by_name["P1"]["medusa"].extra_files["medusa.json"]

    halmos = by_name["P1"]["halmos"].source
    assert "function check_P1_fund_" in halmos
    assert "function act_" not in halmos
    assert "halmos.toml" in by_name["P1"]["halmos"].extra_files

    exclusion = by_name["P2"]["halmos"].source
    assert "check_P2_settle_then_cancel" in exclusion and "check_P2_cancel_then_settle" in exclusion
    assert "check_P2ctl_settle_reachable" in exclusion  # the control that must fail

    replay = by_name["P4"]["foundry"].source
    assert "ledger.stage(c.key) == Ledger.Stage.DONE" in replay
    assert "_crystal_runs_P4_" in replay


def test_synthesised_handlers_record_every_address_they_hand_over(protocol):
    root, contracts = protocol
    compiled = compile_properties(campaigns(), contracts, fixture_for(root), pack="protocol")
    source = next(item for item in compiled if item.name == "P1" and item.backend == "foundry").source
    # Settlement.settle(bytes32 id, address who, uint256 fee): `who` comes from
    # the actor set and is recorded as touched before the call is made.
    assert "address a2 = _crystal_actor(p1);" in source or "address a3 = _crystal_actor(p2);" in source
    assert "c.touched[0] = a" in source
    assert "c.caller = _crystal_actor(seed);" in source
    # The payable entry point gets a bounded value.
    assert "c.value = _crystal_bound(v, 0, 1000 ether);" in source


def test_a_recipe_replaces_synthesis_and_feeds_a_ledger(protocol):
    root, contracts = protocol
    fixture = fixture_for(root)
    fixture.recipes["Settlement.settle"] = Recipe(
        "Settlement.settle",
        "c.caller = alice;\nc.data = abi.encodeCall(settlement.settle, (bytes32(seed), bob, 0));\nc.key = bytes32(seed);",
        feeds="Settlement::_done",
        note="a settlement the fixture knows how to build",
    )
    compiled = compile_properties(campaigns(), contracts, fixture, pack="protocol")
    source = next(item for item in compiled if item.name == "P2" and item.backend == "foundry").source
    ledger = ledger_name("Settlement::_done")
    assert f"_keys_{ledger}" in source and f"_crystal_pickKey_{ledger}(" in source
    assert "a settlement the fixture knows how to build" in source
    # `cancel(bytes32 id, ...)` indexes `_done[id]`, so its synthesised handler
    # now picks ids from the ledger the recipe feeds instead of fuzzing them.
    assert "_crystal_pickKey_" in source.split("// Settlement.cancel")[1].split("function act_")[0]


def test_property_names_follow_the_campaign_id():
    assert property_name("flyover-p5-proven-payment-settlement", 9) == "P5"
    assert property_name("proto-p1-conservation", 1) == "P1"
    assert property_name("generic-reentrancy", 3) == "I3"


def test_write_and_report(protocol, tmp_path):
    root, contracts = protocol
    compiled = compile_properties(campaigns(), contracts, fixture_for(root), pack="protocol")
    written = write_compiled(compiled, tmp_path)
    assert (tmp_path / "test" / "crystal" / "CrystalFoundry_protocol_P1.t.sol").exists()
    assert (tmp_path / "test" / "crystal" / "CrystalMedusa_protocol_P1.sol").exists()
    assert (tmp_path / "test" / "crystal" / "CrystalHalmos_protocol_P1.t.sol").exists()
    assert len(written) >= 9
    report = split_report(compiled)
    assert "[COMPILED]    P1 foundry" in report
    assert "[UNSUPPORTED] P3" in report and "open positions" in report


# ── the flyover target ───────────────────────────────────────────────────

@pytest.mark.skipif(not (LBC / "src").is_dir() or not FLYOVER_PACK.is_file(),
                    reason="the Rootstock flyover target is not on this machine")
def test_flyover_pack_compiles_four_of_five_and_refuses_p3_precisely():
    import sys

    from crystal.campaigns import discover_packs
    from crystal.discovery import discover

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from flyover_fixture import flyover_fixture

    contracts = parse_project(discover(LBC)).contracts
    campaigns_ = discover_packs(packs=[str(FLYOVER_PACK)]).campaigns_for_pack("flyover")
    compiled = compile_properties(campaigns_, contracts, flyover_fixture(str(LBC)), pack="flyover")
    forms = {item.name: item.form for item in compiled}
    assert forms == {
        "P1": "relation", "P2": "exclusion", "P3": "unsupported",
        "P4": "replay", "P5": "independence",
    }
    assert sum(1 for item in compiled if item.supported) == 4 * len(BACKENDS)
    p3 = next(item for item in compiled if item.name == "P3")
    assert "unsettled quote totals" in p3.unsupported_reason
    assert "will not infer" in p3.unsupported_reason

    p5 = next(item for item in compiled if item.name == "P5" and item.backend == "foundry")
    assert "IPegOut.InsufficientCollateral.selector" in p5.source
    assert any(
        read.startswith("CollateralManagementContract::_pegOutCollateral via getPegOutCollateral(address)")
        for read in p5.reads
    )
    # The guard is named with its line; the two front-ends number statements
    # differently, so only the site and the error are pinned here.
    assert any(
        "PegOutContract.refundPegOut:" in note and "IPegOut.InsufficientCollateral" in note
        and "collateral < quote.penaltyFee" in note
        for note in p5.notes
    )
    assert any("refundUserPegOut: no revert is conditioned" in note for note in p5.notes)

    p1 = next(item for item in compiled if item.name == "P1" and item.backend == "foundry")
    assert "collateral.getPenalties()" in p1.source
    assert "collateral.getRewards(_holders[i])" in p1.source
    # PauseRegistry is deployed by the fixture but outside the pack's scope.
    assert not any(label.startswith("PauseRegistry") for label in p1.exercises)
    assert "PegOutContract.refundPegOut" in p1.exercises
