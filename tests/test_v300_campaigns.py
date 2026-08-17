"""Tests for the v3 campaign system, packs, and research engines."""

from crystal.campaigns import (
    CampaignDefinition,
    CampaignCandidate,
    CampaignRegistry,
    CampaignResult,
    CampaignScope,
    CampaignTransition,
    TransitionKind,
    discover_packs,
    run_campaign,
    run_campaigns,
)
from crystal.campaigns.scoring import ScoreComponents, score_candidate
from crystal.graphs.state import StateGraph, build_state_graph, classify_state
from crystal.research.boundary import BoundaryCase, BoundaryProposal, propose_boundaries
from crystal.research.order_sensitivity import detect_order_sensitivity
from crystal.invariants import discover_invariants
from crystal.parsers import solidity_regex
from crystal.sequences import generate_sequences
from crystal.symbolic import SymbolicEngine


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

TRANSFER = """
pragma solidity ^0.8.20;
contract Token {
    mapping(address => uint256) public balances;
    address public owner;
    uint256 public nonce;
    uint256 public expiry;

    modifier onlyOwner() { require(msg.sender == owner); _; }

    function transfer(address to, uint256 amount) external {
        balances[msg.sender] -= amount;
        balances[to] += amount;
        nonce += 1;
    }
    function setOwner(address x) external onlyOwner { owner = x; }
    function setExpiry(uint256 x) external onlyOwner { expiry = x; }
    function approve(address spender, uint256 amount) external {
        nonce += 1;
    }
}
"""

BOUNDARY_SOURCE = """
pragma solidity ^0.8.20;
contract Auction {
    uint256 public deadline;
    uint256 public premium;

    function bid() external {
        require(block.timestamp < deadline);
        premium = (deadline - block.timestamp) * 10;
    }
    function claim() external {
        require(block.timestamp >= deadline);
    }
}
"""


def parse(source, name="T.sol"):
    return solidity_regex.parse_text(source, name)


# ---------------------------------------------------------------------------
# TransitionKind
# ---------------------------------------------------------------------------

def test_transition_kind_values():
    assert TransitionKind.WRITE_READ.value == "write-read"
    assert TransitionKind.MIGRATION_ACTION.value == "migration-action"
    assert len(TransitionKind) == 12


# ---------------------------------------------------------------------------
# CampaignScope
# ---------------------------------------------------------------------------

def test_scope_accepts_contract_wildcard():
    scope = CampaignScope()
    assert scope.accepts_contract("Anything")

    scope = CampaignScope(allowed_contracts=["Token"])
    assert scope.accepts_contract("Token")
    assert not scope.accepts_contract("Vault")


def test_scope_accepts_contract_glob():
    scope = CampaignScope(allowed_contracts=["Token*"])
    assert scope.accepts_contract("TokenVault")
    assert not scope.accepts_contract("Vault")


def test_scope_accepts_category():
    scope = CampaignScope(allowed_categories=["ownership", "role"])
    assert scope.accepts_category("ownership")
    assert not scope.accepts_category("balance")


# ---------------------------------------------------------------------------
# CampaignDefinition
# ---------------------------------------------------------------------------

def test_campaign_matches_transition():
    campaign = CampaignDefinition(
        campaign_id="test-1",
        name="Test",
        description="test",
        transitions=[
            CampaignTransition(kind=TransitionKind.OWNERSHIP_ACTION,
                               description="test"),
        ],
    )
    assert campaign.matches_transition("ownership-action")
    assert not campaign.matches_transition("balance-transfer")


def test_campaign_no_transitions_matches_all():
    campaign = CampaignDefinition(
        campaign_id="test-2", name="Test", description="test",
    )
    assert campaign.matches_transition("anything")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_registry_register_and_retrieve():
    registry = CampaignRegistry()
    campaign = CampaignDefinition(
        campaign_id="test-reg-1", name="Test", description="d", pack="test",
    )
    registry.register(campaign)
    assert registry.get("test-reg-1") is campaign
    assert registry.get("nonexistent") is None
    assert "test" in registry.list_packs()
    assert len(registry.campaigns_for_pack("test")) == 1


def test_discover_packs_loads_builtins():
    registry = discover_packs()
    packs = registry.list_packs()
    assert "generic" in packs
    assert "defi" in packs
    assert "authorization" in packs
    assert "migration" in packs
    assert "economic" in packs
    assert "registry" in packs
    campaigns = registry.list_campaigns()
    assert len(campaigns) >= 15


def test_ens_pack_loads():
    registry = discover_packs()
    registry.load_pack("crystal.packs.ens")
    assert "ens" in registry.list_packs()
    ens_campaigns = registry.campaigns_for_pack("ens")
    assert len(ens_campaigns) == 7
    ids = {c.campaign_id for c in ens_campaigns}
    assert "ens-A1-migration-fuse-roles" in ids
    assert "ens-B4-expiry-premium" in ids


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def test_score_candidate_basic():
    components = score_candidate(
        sequence=("A.f", "A.g"),
        changed_state=["owner"],
        categories=("ownership",),
        edge_kinds=("ownership-action",),
        novelty=0.8,
        confidence=0.7,
        validation_status="SYMBOLIC_ONLY",
    )
    assert isinstance(components, ScoreComponents)
    assert 0.0 <= components.final <= 1.0
    assert components.final > 0.0


def test_score_penalizes_low_confidence():
    high = score_candidate(
        sequence=("A.f",), changed_state=["x"],
        categories=(), edge_kinds=(), novelty=0.5,
        confidence=0.9, validation_status="SYMBOLIC_ONLY",
    )
    low = score_candidate(
        sequence=("A.f",), changed_state=["x"],
        categories=(), edge_kinds=(), novelty=0.5,
        confidence=0.2, validation_status="SYMBOLIC_ONLY",
    )
    assert high.final > low.final


# ---------------------------------------------------------------------------
# CampaignResult
# ---------------------------------------------------------------------------

def test_campaign_result_top_candidates():
    candidates = [
        CampaignCandidate(
            candidate_id=f"c{i}", campaign_id="t", category="storage",
            target_contract="X", target_functions=("f",),
            hypothesis="h", state_sequence=("f",), score=float(i),
        )
        for i in range(15)
    ]
    result = CampaignResult(
        campaign_id="t", campaign_name="Test", candidates=candidates,
    )
    top = result.top_candidates
    assert len(top) == 10
    assert top[0].score >= top[-1].score


# ---------------------------------------------------------------------------
# run_campaign against parsed source
# ---------------------------------------------------------------------------

def test_run_campaign_produces_candidates():
    from crystal.graphs.state import build_state_graph, candidate_sequences
    from crystal.research.statedelta import derive_state_deltas

    contracts = parse(TRANSFER)
    engine = SymbolicEngine(contracts)
    state_graph = build_state_graph(contracts)
    invariants = discover_invariants(contracts)
    sequences = generate_sequences(candidate_sequences(state_graph), invariants,
                                    state_graph=state_graph)
    deltas = derive_state_deltas(contracts, sequences, engine)

    campaign = CampaignDefinition(
        campaign_id="test-run",
        name="Test Run",
        description="test",
        scope=CampaignScope(max_sequence_length=4, max_candidates=5),
    )

    result_dict = {
        "contracts": contracts,
        "state_deltas": deltas,
        "differential_candidates": [],
        "novel_behaviors": [],
        "state_graph": state_graph,
        "detectors": [],
    }

    cr = run_campaign(campaign, result_dict)
    assert cr.campaign_id == "test-run"
    assert cr.total_sequences_explored > 0


# ---------------------------------------------------------------------------
# State graph classification
# ---------------------------------------------------------------------------

def test_classify_state_categories():
    assert classify_state("owner") == "ownership"
    assert classify_state("nonce") == "nonce"
    assert classify_state("expiry") == "temporal"
    assert classify_state("balances") == "balance"
    assert classify_state("resolver") == "registry"
    assert classify_state("randomVar") == "storage"


# ---------------------------------------------------------------------------
# Boundary engine
# ---------------------------------------------------------------------------

def test_propose_boundaries():
    contracts = parse(BOUNDARY_SOURCE)
    proposals = propose_boundaries(contracts)
    assert isinstance(proposals, list)
    for p in proposals:
        assert isinstance(p, BoundaryProposal)
        for case in p.cases:
            assert isinstance(case, BoundaryCase)


# ---------------------------------------------------------------------------
# Order-sensitivity engine
# ---------------------------------------------------------------------------

def test_order_sensitivity_runs():
    contracts = parse(TRANSFER)
    engine = SymbolicEngine(contracts)
    state_graph = build_state_graph(contracts)
    results = detect_order_sensitivity(contracts, state_graph, engine)
    assert isinstance(results, list)


# ---------------------------------------------------------------------------
# CLI campaign list (argument parser)
# ---------------------------------------------------------------------------

def test_cli_campaign_list_parser():
    from crystal.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["campaign", "list"])
    assert args.command == "campaign"
    assert args.campaign_command == "list"


def test_cli_campaign_run_parser():
    from crystal.cli import build_parser
    parser = build_parser()
    args = parser.parse_args(["campaign", "run", "test-id", "./target"])
    assert args.command == "campaign"
    assert args.campaign_command == "run"
    assert args.campaign_id == "test-id"
    assert args.project == "./target"


# ---------------------------------------------------------------------------
# Report payload includes campaigns
# ---------------------------------------------------------------------------

def test_payload_includes_campaign_results():
    from crystal.report import _campaign_payload
    cr = CampaignResult(campaign_id="t", campaign_name="Test")
    result = {"campaign_results": [cr]}
    payload = _campaign_payload(result)
    assert len(payload) == 1
    assert payload[0]["campaign_id"] == "t"


# ---------------------------------------------------------------------------
# Capabilities list
# ---------------------------------------------------------------------------

def test_capabilities_include_v3():
    from crystal.cli import CAPABILITIES
    assert "campaign-system" in CAPABILITIES
    assert "order-sensitivity-engine" in CAPABILITIES
    assert "boundary-engine" in CAPABILITIES
    assert "asymmetric-side-effect-detector" in CAPABILITIES
    assert "ens-preset" in CAPABILITIES
