"""Build 010: campaign packs on the analysis path, and composition by call.

Five defects found in live use, each of which made a working mechanism look
like a clean result:

* an operator's campaign pack could not be loaded by `scan` at all, so output
  was byte-identical with and without it;
* `rglob("*.sol")` matched Foundry's `broadcast/X.s.sol/` directory in six more
  places than the one already fixed, crashing any whole-repository scan;
* `crystal campaign list` raised `AttributeError` before printing anything;
* the causal graph only linked functions sharing storage, so a protocol split
  across contracts that compose by *calling* produced an empty graph;
* `asymmetric-side-effect` could not report the minimal asymmetry — two sibling
  entry points, one guarded, one not.
"""

from crystal.campaigns import discover_packs
from crystal.campaigns.definition import CampaignDefinition, CampaignScope
from crystal.detectors.asymmetric_side_effect import detect as detect_asymmetry
from crystal.engine import research
from crystal.graphs.binding import build_bindings
from crystal.graphs.state import build_state_graph
from crystal.parsers import parse_project
from crystal.paths import glob_files, rglob_files

# A protocol that is nothing but composition: the handle is interface-typed and
# no storage is shared between the two contracts at any point.
COMPOSED = """
pragma solidity ^0.8.20;
interface ICollateralManagement {
    function slashPegOutCollateral(address who, uint256 amount) external;
}
contract CollateralManagement is ICollateralManagement {
    mapping(address => uint256) public collateral;
    uint256 public slashed;
    function slashPegOutCollateral(address who, uint256 amount) external {
        collateral[who] -= amount;
        slashed += amount;
    }
}
contract PegOutContract {
    ICollateralManagement _collateralManagement;
    uint256 public pegOutCount;
    function refundPegOut(address who, uint256 amount) external {
        pegOutCount += 1;
        _collateralManagement.slashPegOutCollateral(who, amount);
    }
}
"""

# Two sibling entry points reaching the same value operation, one behind a
# revocation check and one behind nothing.
GUARDED_SIBLINGS = """
pragma solidity ^0.8.20;
contract PegOutContract {
    mapping(bytes32 => uint256) public collateral;
    mapping(address => bool) public revoked;
    uint256 public totalSlashed;
    function refundUserPegOut(bytes32 id, address who, uint256 amount) external {
        require(!revoked[who], "revoked");
        _transfer(who, amount);
        collateral[id] -= amount;
    }
    function refundPegOut(bytes32 id, address who, uint256 amount) external {
        _transfer(who, amount);
        collateral[id] -= amount;
    }
    function _transfer(address who, uint256 amount) internal {
        totalSlashed += amount;
    }
}
"""

# The same composition, but reached through an internal helper: the external
# call lives in `_transfer`, which no caller outside the contract can invoke.
COMPOSED_VIA_HELPER = """
pragma solidity ^0.8.20;
interface ICollateralManagement {
    function slashPegOutCollateral(address who, uint256 amount) external;
}
contract CollateralManagement is ICollateralManagement {
    mapping(address => uint256) public collateral;
    uint256 public slashed;
    function slashPegOutCollateral(address who, uint256 amount) external {
        collateral[who] -= amount;
        slashed += amount;
    }
}
contract PegOutContract {
    ICollateralManagement _collateralManagement;
    uint256 public pegOutCount;
    function refundPegOut(address who, uint256 amount) external {
        pegOutCount += 1;
        _transfer(who, amount);
    }
    function _transfer(address who, uint256 amount) internal {
        _collateralManagement.slashPegOutCollateral(who, amount);
    }
}
"""

VAULT = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    uint256 public balances;
    function deposit() external payable {
        totalAssets += msg.value;
        balances += msg.value;
    }
    function donate() external payable { totalAssets += msg.value; }
}
"""

PACK_SOURCE = "\n".join([
    "from crystal.campaigns import (",
    "    CampaignDefinition, CampaignScope, CampaignTransition, TransitionKind,",
    ")",
    "from crystal.campaigns.definition import CampaignInvariant",
    "",
    "CAMPAIGNS = [",
    "    CampaignDefinition(",
    "        campaign_id='flyover-%d' % n,",
    "        name='Flyover %d' % n,",
    "        description='operator campaign',",
    "        pack='flyover',",
    "        scope=CampaignScope(max_sequence_length=3, max_candidates=5),",
    "        transitions=[CampaignTransition(",
    "            TransitionKind.BALANCE_TRANSFER, 'value moves')],",
    "        invariants=[CampaignInvariant(",
    "            'value must be conserved', 'balance',",
    "            affected_state=['totalAssets', 'balances'])],",
    "        questions=['does path %d conserve value under reordering?' % n],",
    "    )",
    "    for n in range(1, 6)",
    "]",
    "",
])


def _write(directory, name, text):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _contracts(tmp_path, text):
    return parse_project([_write(tmp_path, "P.sol", text)]).contracts


def _project(tmp_path, text, name="Target.sol"):
    project = tmp_path / "project"
    project.mkdir(exist_ok=True)
    _write(project, name, text)
    return project


# -- #2 directories that end in a source extension --------------------------

def test_rglob_files_skips_directories_named_like_sources(tmp_path):
    """Foundry's `broadcast/Deploy.s.sol/` is a directory, not a file."""
    broadcast = tmp_path / "broadcast" / "Deploy.s.sol"
    broadcast.mkdir(parents=True)
    (broadcast / "run-latest.json").write_text("{}", encoding="utf-8")
    real = _write(tmp_path / "src", "Token.sol",
                  "pragma solidity ^0.8.20; contract T {}")

    found = list(rglob_files(tmp_path, "*.sol"))
    assert found == [real]
    # Every returned path must survive a read; that is the whole point.
    for path in found:
        path.read_text(encoding="utf-8")


def test_rglob_files_honours_the_exclude_set(tmp_path):
    for relative in ("src/A.sol", "node_modules/dep/B.sol", "out/C.sol"):
        _write(tmp_path / relative.rsplit("/", 1)[0],
               relative.rsplit("/", 1)[1], "contract X {}")
    kept = {p.name for p in rglob_files(
        tmp_path, "*.sol", frozenset({"node_modules", "out"})
    )}
    assert kept == {"A.sol"}


def test_glob_files_skips_directories(tmp_path):
    (tmp_path / "pack.py").write_text("CAMPAIGNS = []", encoding="utf-8")
    (tmp_path / "notapack.py").mkdir()
    assert [p.name for p in glob_files(tmp_path, "*.py")] == ["pack.py"]


def test_whole_repository_scan_survives_a_foundry_layout(tmp_path):
    """The end-to-end shape that crashed: a Foundry repo with broadcast output."""
    broadcast = tmp_path / "broadcast" / "Deploy.s.sol" / "31337"
    broadcast.mkdir(parents=True)
    (broadcast / "run-latest.json").write_text("{}", encoding="utf-8")
    (tmp_path / "broadcast" / "Other.s.sol").mkdir(parents=True, exist_ok=True)
    _write(tmp_path, "Vault.sol", VAULT)

    result = research(str(tmp_path), use_solc=False, use_foundry=False)
    assert [c.name for c in result["contracts"]] == ["Vault"]


# -- #3 campaign list ------------------------------------------------------

def test_campaign_list_does_not_require_a_pack_argument():
    """`campaign list` read `args.pack`, which only `campaign run` declared."""
    from crystal.cli import _campaign, build_parser

    args = build_parser().parse_args(["campaign", "list"])
    assert getattr(args, "pack", None) is None
    assert _campaign(args) == 0


def test_campaign_list_accepts_a_pack():
    from crystal.cli import build_parser

    args = build_parser().parse_args(
        ["campaign", "list", "--pack", "crystal.packs.ens"]
    )
    assert args.pack == "crystal.packs.ens"


# -- #1 packs reaching the analysis path -----------------------------------

def test_a_pack_outside_crystal_can_be_loaded_from_a_file(tmp_path):
    """An operator's pack lives beside their engagement, not inside Crystal."""
    path = _write(tmp_path, "flyover.py", PACK_SOURCE)
    registry = discover_packs(packs=[str(path)])
    assert "flyover" in registry.list_packs()
    assert len(registry.campaigns_for_pack("flyover")) == 5
    assert registry.load_report[str(path)] == 5


def test_a_pack_that_loads_nothing_is_reported_not_ignored(tmp_path):
    path = _write(tmp_path, "empty.py", "CAMPAIGNS = []\n")
    registry = discover_packs(packs=[str(path)])
    assert registry.load_report[str(path)] == 0


def test_a_pack_reaches_the_scan_path_and_changes_the_output(tmp_path):
    """The headline defect: scan output was identical with and without a pack."""
    project = _project(tmp_path, VAULT)
    pack = _write(tmp_path, "flyover.py", PACK_SOURCE)

    without = research(str(project), use_solc=False, use_foundry=False)
    with_pack = research(str(project), use_solc=False, use_foundry=False,
                         packs=[str(pack)])

    ran_without = {c.campaign_id for c in without["campaign_results"]}
    ran_with = {c.campaign_id for c in with_pack["campaign_results"]}
    assert not any(x.startswith("flyover-") for x in ran_without)
    assert {f"flyover-{n}" for n in range(1, 6)} <= ran_with
    assert with_pack["campaign_packs"][str(pack)] == 5


def test_campaign_questions_and_invariants_reach_the_evidence(tmp_path):
    project = _project(tmp_path, VAULT)
    pack = _write(tmp_path, "flyover.py", PACK_SOURCE)
    result = research(str(project), use_solc=False, use_foundry=False,
                      packs=[str(pack)])

    candidates = [
        c for cr in result["campaign_results"] for c in cr.candidates
        if cr.campaign_id.startswith("flyover-")
    ]
    assert candidates, "the operator pack produced no candidate"
    evidence = " | ".join(e for c in candidates for e in c.evidence)
    assert "invariant-candidate: value must be conserved" in evidence
    assert "campaign-question:" in evidence
    assert any(c.questions for c in candidates)


def test_scope_rejections_are_attributed(tmp_path):
    """A campaign that reported nothing must say which rule rejected what."""
    result = research(str(_project(tmp_path, VAULT)),
                      use_solc=False, use_foundry=False)
    quiet = [c for c in result["campaign_results"]
             if not c.candidates and c.total_sequences_pruned]
    assert quiet, "expected at least one campaign to prune something"
    assert all(c.pruned_by for c in quiet)


def test_allowed_functions_scope_is_enforced(tmp_path):
    """`accepts_function` was defined and never called."""
    from crystal.campaigns.runner import run_campaign

    result = research(str(_project(tmp_path, VAULT)),
                      use_solc=False, use_foundry=False)
    assert result["state_deltas"], "fixture produced no sequence to scope"

    outcome = run_campaign(CampaignDefinition(
        campaign_id="scoped", name="Scoped", description="",
        scope=CampaignScope(allowed_functions=["Vault.nothingMatches"]),
    ), result)
    assert outcome.candidates == []
    assert outcome.pruned_by.get("allowed_functions")


def test_allowed_categories_scope_is_enforced(tmp_path):
    """`accepts_category` was defined and never called."""
    from crystal.campaigns.runner import run_campaign

    result = research(str(_project(tmp_path, VAULT)),
                      use_solc=False, use_foundry=False)
    outcome = run_campaign(CampaignDefinition(
        campaign_id="scoped", name="Scoped", description="",
        scope=CampaignScope(allowed_categories=["temporal"]),
    ), result)
    assert outcome.candidates == []
    assert outcome.pruned_by.get("allowed_categories")


def test_campaigns_are_rendered_in_markdown(tmp_path):
    from crystal.report import markdown, payload

    result = research(str(_project(tmp_path, VAULT)),
                      use_solc=False, use_foundry=False)
    text = markdown(payload(result))
    assert "## Campaigns" in text
    assert "Why pruned" in text


# -- #4 composition through a declared type --------------------------------

def test_an_interface_handle_binds_to_its_implementation(tmp_path):
    bindings = build_bindings(_contracts(tmp_path, COMPOSED))
    assert bindings.implementations("ICollateralManagement") == (
        "CollateralManagement",
    )
    assert bindings.resolve(
        "PegOutContract", "_collateralManagement", "slashPegOutCollateral"
    ) == [("CollateralManagement.slashPegOutCollateral", 0.88)]


def test_a_bare_name_match_never_binds(tmp_path):
    """Binding is by declared type; two `settle` are not the same `settle`."""
    bindings = build_bindings(_contracts(tmp_path, COMPOSED))
    assert bindings.resolve("PegOutContract", "_notAHandle", "slash") == []
    assert bindings.resolve("PegOutContract", "pegOutCount", "anything") == []


def test_a_cross_contract_call_produces_a_causal_edge(tmp_path):
    """Five contracts that call each other used to yield zero edges."""
    graph = build_state_graph(_contracts(tmp_path, COMPOSED))
    calls = [e for e in graph.causal_edges if e.edge_kind == "call-flow"]
    assert len(calls) == 1
    edge = calls[0]
    assert edge.source == "PegOutContract.refundPegOut"
    assert edge.target == "CollateralManagement.slashPegOutCollateral"
    assert edge.source_contract != edge.target_contract
    assert edge.key_relation == "declared-type"
    assert edge.condition == "_collateralManagement.slashPegOutCollateral"
    # The callee's writes are what the call sets in motion.
    assert set(edge.consumed) == {
        "CollateralManagement::collateral", "CollateralManagement::slashed",
    }


def test_composition_reports_a_protocol_that_only_composes(tmp_path):
    result = research(str(_project(tmp_path, COMPOSED, "Protocol.sol")),
                      use_solc=False, use_foundry=False)

    assert result["state_graph"].causal_edges, "no causal edge across contracts"
    candidates = result["composition_candidates"]
    assert candidates, "a protocol that is only composition reported nothing"
    chain = candidates[0]
    assert chain.chain == [
        "PegOutContract.refundPegOut",
        "CollateralManagement.slashPegOutCollateral",
    ]
    assert "cross-contract" in chain.mechanism


def test_a_call_through_an_internal_helper_anchors_on_the_entry_point(tmp_path):
    """The chain must start where a caller can actually enter.

    The external call lives in `_transfer`, which is internal. Anchoring the
    chain there reports something nobody can invoke, and loses the reachable
    chain through `refundPegOut` entirely.
    """
    graph = build_state_graph(_contracts(tmp_path, COMPOSED_VIA_HELPER))
    calls = [e for e in graph.causal_edges if e.edge_kind == "call-flow"]
    assert [e.source for e in calls] == ["PegOutContract.refundPegOut"]
    # The helper it went through is named, so the evidence stays honest about
    # where the call actually is.
    assert calls[0].condition == (
        "_transfer -> _collateralManagement.slashPegOutCollateral"
    )


def test_an_unreachable_internal_call_produces_no_chain(tmp_path):
    """An internal helper is not an entry point, however much it calls out."""
    graph = build_state_graph(_contracts(tmp_path, COMPOSED_VIA_HELPER))
    assert not [
        e for e in graph.causal_edges if e.source == "PegOutContract._transfer"
    ]


def test_composition_through_a_helper_is_reported(tmp_path):
    result = research(str(_project(tmp_path, COMPOSED_VIA_HELPER, "P.sol")),
                      use_solc=False, use_foundry=False)
    chains = [c.chain for c in result["composition_candidates"]]
    assert ["PegOutContract.refundPegOut",
            "CollateralManagement.slashPegOutCollateral"] in chains
    assert not any(c[0].endswith("._transfer") for c in chains)


# -- #5 the minimal asymmetry ----------------------------------------------

def test_a_guarded_and_an_unguarded_sibling_are_an_asymmetry(tmp_path):
    """Two entry points, same transition, one with a revocation check."""
    signals = detect_asymmetry(_contracts(tmp_path, GUARDED_SIBLINGS))
    assert signals, "the school case produced no signal"
    found = [s for s in signals if s.function == "refundPegOut"]
    assert found, "the unguarded sibling was not the one reported"
    evidence = " | ".join(found[0].evidence)
    assert "authorization guard" in found[0].title
    assert "1/2 sites" in evidence
    # The guard the other path has must be named, not merely counted.
    assert "revoked" in evidence


def test_the_guarded_sibling_is_not_itself_reported(tmp_path):
    signals = detect_asymmetry(_contracts(tmp_path, GUARDED_SIBLINGS))
    assert not [s for s in signals if s.function == "refundUserPegOut"]


def test_symmetric_guards_stay_quiet(tmp_path):
    """Both siblings guarded is the system working, and must not report."""
    symmetric = GUARDED_SIBLINGS.replace(
        "    function refundPegOut(bytes32 id, address who, uint256 amount)"
        " external {\n        _transfer(who, amount);",
        "    function refundPegOut(bytes32 id, address who, uint256 amount)"
        " external {\n        require(!revoked[who], \"revoked\");\n"
        "        _transfer(who, amount);",
    )
    assert symmetric != GUARDED_SIBLINGS, "fixture rewrite did not apply"
    signals = detect_asymmetry(_contracts(tmp_path, symmetric))
    assert not [s for s in signals if "authorization guard" in s.title]
