"""Build 012: ranking hygiene on the Flyover/Rootstock settlement surface.

Four defects that made a five-contract protocol look like a hundred-candidate
haystack, each captured here by its *shape* on a synthetic fixture rather than
by the external target:

* a one-shot `initialize` (the OpenZeppelin `initializer` modifier, or a
  hand-rolled self-guarding flag) headed attack sequences it can never take part
  in, outranking the real defect chain;
* the same chain was rendered once per campaign that selected it, instead of
  once with the campaigns aggregated;
* `legacy/` and `test-contracts/` trees — superseded copies and scaffolding —
  were scanned as if they were live code;
* call-flow edges through interface-typed handles multiplied composition
  candidates past the point of triage.
"""

from crystal.discovery import excluded_dir_reason
from crystal.engine import research
from crystal.graphs.state import (
    StateGraph,
    StateTransition,
    attacker_reachable_functions,
    one_shot_functions,
)
from crystal.report import payload
from crystal.research.composition import (
    MAX_PER_HEAD,
    CompositionCandidate,
    _rank_and_cap,
)

# A protocol that is nothing but cross-contract composition: `PegOut.refund`
# reaches `Collateral.slash` through an interface-typed handle. `initialize`
# carries the one-shot modifier and shares `_collateral` with `refund`, so
# without the guard it would head an `initialize -> refund -> slash` chain.
FLYOVER = """
pragma solidity ^0.8.20;
interface ICollateral { function slash(address who, uint256 amount) external; }
contract Collateral is ICollateral {
    mapping(address => uint256) public collateral;
    uint256 public slashed;
    function slash(address who, uint256 amount) external {
        collateral[who] -= amount;
        slashed += amount;
    }
}
contract PegOut {
    ICollateral private _collateral;
    mapping(bytes32 => uint256) public registry;
    uint256 public count;
    bool private _initialized;
    modifier initializer() { require(!_initialized, "init"); _initialized = true; _; }
    function initialize(address c) external initializer { _collateral = ICollateral(c); }
    function deposit(bytes32 id, uint256 amount) external {
        registry[id] += amount;
        count += 1;
    }
    function refund(bytes32 id, address who, uint256 amount) external {
        registry[id] -= amount;
        _collateral.slash(who, amount);
    }
}
"""

REAL = ("pragma solidity ^0.8.20; contract RealPegOut "
        "{ uint256 public x; function poke() external { x += 1; } }")
LEGACY = ("pragma solidity ^0.8.20; contract OldVault "
          "{ uint256 public y; function poke() external { y += 1; } }")
SCAFFOLD = ("pragma solidity ^0.8.20; contract Widget "
            "{ uint256 public z; function poke() external { z += 1; } }")

# Two operator campaigns with identical, wide scope: every chain one selects,
# the other selects too, so every candidate is a cross-campaign duplicate.
DUP_PACK = "\n".join([
    "from crystal.campaigns.definition import CampaignDefinition, CampaignScope",
    "CAMPAIGNS = [",
    "    CampaignDefinition(campaign_id='opdup-a', name='A', description='op',",
    "        pack='opdup',",
    "        scope=CampaignScope(max_sequence_length=3, max_candidates=10)),",
    "    CampaignDefinition(campaign_id='opdup-b', name='B', description='op',",
    "        pack='opdup',",
    "        scope=CampaignScope(max_sequence_length=3, max_candidates=10)),",
    "]",
    "",
])


def _write(directory, name, text):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _flyover_project(tmp_path):
    src = tmp_path / "lbc" / "src"
    _write(src, "Flyover.sol", FLYOVER)
    return tmp_path / "lbc"


def _transition(function, **kwargs):
    return StateTransition(function=function, **kwargs)


def _graph(*transitions):
    graph = StateGraph()
    graph.transitions = list(transitions)
    return graph


# -- Task 1: one-shot initializers never head a live sequence ----------------

def test_one_shot_functions_recognises_modifier_and_self_guarding_flag():
    """The modifier is the fast path; a self-set init flag is the structural one."""
    graph = _graph(
        _transition("C.initialize", writes={"C::owner"},
                    visibility="external", modifiers=("initializer",)),
        _transition("C.reinit", writes={"C::x"},
                    visibility="external", modifiers=("reinitializer(2)",)),
        _transition("C.configure", reads={"C::initialized"},
                    writes={"C::initialized", "C::cfg"}, visibility="external"),
        _transition("C.deposit", reads={"C::bal"}, writes={"C::bal"},
                    visibility="external"),
    )
    assert one_shot_functions(graph) == {"C.initialize", "C.reinit", "C.configure"}


def test_attacker_reachable_excludes_one_shot_privileged_and_internal():
    graph = _graph(
        _transition("C.deposit", visibility="external", modifiers=("nonReentrant",)),
        _transition("C.initialize", visibility="external", modifiers=("initializer",)),
        _transition("C.setFee", visibility="external", modifiers=("onlyOwner",)),
        _transition("C.grant", visibility="public", modifiers=("onlyRole",)),
        _transition("C.helper", visibility="internal"),
    )
    reachable = attacker_reachable_functions(graph)
    assert "C.deposit" in reachable
    assert reachable.isdisjoint({"C.initialize", "C.setFee", "C.grant", "C.helper"})


def test_initializer_never_heads_or_joins_a_composition_chain(tmp_path):
    """Acceptance shape: the real chain outranks every chain with `initialize`.

    Here that means no composition candidate contains `initialize` at all, while
    the real `deposit -> refund -> slash` chain survives — even though the graph
    proves `initialize` would otherwise have headed a chain.
    """
    result = research(str(_flyover_project(tmp_path)),
                      use_solc=False, use_foundry=False)
    graph = result["state_graph"]

    assert "PegOut.initialize" in one_shot_functions(graph)
    # The guard is doing work: an edge out of `initialize` exists in the graph.
    assert any(e.source == "PegOut.initialize" for e in graph.causal_edges)

    chains = [tuple(c.chain) for c in result["composition_candidates"]]
    assert all(not any("initialize" in step for step in chain) for chain in chains)
    assert any(
        "PegOut.refund" in chain and "Collateral.slash" in chain
        for chain in chains
    ), "the real cross-contract chain must survive"


def test_initializer_excluded_from_campaign_sequences(tmp_path):
    result = research(str(_flyover_project(tmp_path)),
                      use_solc=False, use_foundry=False)
    for campaign in result["campaign_results"]:
        for candidate in campaign.candidates:
            assert all("initialize" not in step
                       for step in candidate.state_sequence)


# -- Task 2: deduplicate across campaigns, aggregate the campaigns -----------

def test_a_chain_is_rendered_once_with_campaigns_aggregated(tmp_path):
    project = tmp_path / "proj"
    _write(project / "src", "Pool.sol", (
        "pragma solidity ^0.8.20; contract Pool {"
        " uint256 public totalAssets; uint256 public balances;"
        " function deposit() external payable"
        " { totalAssets += msg.value; balances += msg.value; }"
        " function sync() external { balances = totalAssets; } }"
    ))
    pack = _write(tmp_path, "opdup.py", DUP_PACK)

    result = research(str(project), use_solc=False, use_foundry=False,
                      packs=[str(pack)])
    candidates = [c for cr in result["campaign_results"] for c in cr.candidates]
    assert candidates, "fixture produced no campaign candidate"

    chains = [c.state_sequence for c in candidates]
    assert len(chains) == len(set(chains)), \
        "a chain was rendered under more than one campaign"

    aggregated = [
        c for c in candidates
        if "opdup-a" in c.selected_by_campaigns
        and "opdup-b" in c.selected_by_campaigns
    ]
    assert aggregated, "campaigns that selected the same chain were not aggregated"

    # The JSON payload the report is built from carries no duplicate either.
    data = payload(result)
    payload_chains = [
        tuple(c["state_sequence"])
        for cr in data["campaign_results"] for c in cr["candidates"]
    ]
    assert len(payload_chains) == len(set(payload_chains))


def test_operator_pack_survives_dedup_against_built_ins(tmp_path):
    """A generic built-in must not steal an operator campaign's candidate."""
    project = tmp_path / "proj"
    _write(project / "src", "Pool.sol", (
        "pragma solidity ^0.8.20; contract Pool {"
        " uint256 public totalAssets; uint256 public balances;"
        " function deposit() external payable"
        " { totalAssets += msg.value; balances += msg.value; }"
        " function sync() external { balances = totalAssets; } }"
    ))
    pack = _write(tmp_path, "opdup.py", DUP_PACK)

    result = research(str(project), use_solc=False, use_foundry=False,
                      packs=[str(pack)])
    operator_hosted = [
        c for cr in result["campaign_results"]
        for c in cr.candidates
        if cr.campaign_id.startswith("opdup-")
    ]
    assert operator_hosted, "the operator pack lost every candidate to a built-in"


# -- Task 3: legacy and scaffolding directories are out of scope -------------

def test_excluded_dir_reason_is_structural_by_segment():
    assert excluded_dir_reason("a/src/legacy/X.sol") == "superseded/legacy directory"
    assert excluded_dir_reason("a/src/deprecated/Z.sol") == "superseded/legacy directory"
    assert excluded_dir_reason("a/test-contracts/Y.sol") == "test-scaffolding directory"
    assert excluded_dir_reason("a/src/PegOut.sol") is None
    # By directory, never by contract name: a production file merely *named*
    # `LegacyResolver` in an ordinary directory stays in scope.
    assert excluded_dir_reason("a/src/LegacyResolver.sol") is None


def test_scaffolding_and_legacy_excluded_visibly_and_restorably(tmp_path):
    src = tmp_path / "lbc" / "src"
    _write(src, "RealPegOut.sol", REAL)
    _write(src / "legacy", "OldVault.sol", LEGACY)
    _write(src / "test-contracts", "Widget.sol", SCAFFOLD)
    project = tmp_path / "lbc"

    result = research(str(project), use_solc=False, use_foundry=False)
    researched = {c.name for c in result["contracts"]}
    assert "RealPegOut" in researched
    assert researched.isdisjoint({"OldVault", "Widget"})

    # Still parsed, still listed under the excluded bucket, with a reason.
    excluded = {c.name for c in result["test_contracts"]}
    assert {"OldVault", "Widget"} <= excluded
    reasons = {e["contract"]: e["reason"]
               for e in result.get("excluded_scaffolding", [])}
    assert reasons.get("OldVault") == "superseded/legacy directory"
    assert reasons.get("Widget") == "test-scaffolding directory"

    listed = " ".join(payload(result)["excluded_test_contracts"])
    assert "OldVault" in listed and "Widget" in listed

    # --include-tests restores them to research.
    restored = research(str(project), use_solc=False, use_foundry=False,
                        include_tests=True)
    assert {"RealPegOut", "OldVault", "Widget"} <= {
        c.name for c in restored["contracts"]
    }


# -- Task 4: composition candidates are ranked and capped, not removed -------

def test_rank_and_cap_dedups_prunes_unreachable_heads_and_caps_fan_out():
    graph = _graph(
        _transition("C.open", visibility="external"),
        _transition("C.admin", visibility="external", modifiers=("onlyOwner",)),
    )

    def candidate(chain, score):
        return CompositionCandidate(list(chain), ["x"], "e", score, True, [], [])

    result = _rank_and_cap([
        candidate(["C.open", "C.a"], 0.90),
        candidate(["C.open", "C.b"], 0.80),
        candidate(["C.open", "C.c"], 0.70),
        candidate(["C.open", "C.d"], 0.60),   # 4th tail — capped away
        candidate(["C.open", "C.a"], 0.50),   # duplicate chain — deduped
        candidate(["C.admin", "C.z"], 0.95),  # unreachable head — pruned
    ], graph)

    heads = [c.chain[0] for c in result]
    assert heads.count("C.open") == MAX_PER_HEAD
    assert all(c.chain[0] != "C.admin" for c in result)

    open_a = [c for c in result if c.chain == ["C.open", "C.a"]]
    assert len(open_a) == 1 and open_a[0].score == 0.90


def test_composition_is_bounded_and_free_of_one_shot_heads(tmp_path):
    result = research(str(_flyover_project(tmp_path)),
                      use_solc=False, use_foundry=False)
    candidates = result["composition_candidates"]
    assert candidates, "the composed protocol must still yield candidates"
    assert len(candidates) <= 20
    per_head: dict[str, int] = {}
    for candidate in candidates:
        head = candidate.chain[0]
        per_head[head] = per_head.get(head, 0) + 1
    assert all(count <= MAX_PER_HEAD for count in per_head.values())
