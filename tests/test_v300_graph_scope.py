"""Build 014: the state graph describes the same contract set as the rest.

Build 012 moved scaffolding and superseded code (`test-contracts/`, `legacy/`,
`mocks/`, ...) out of research by path segment, and every research output
respected it — except `result["state_graph"]`, which is built before that
filter runs and was never filtered. The graph kept carrying transitions, state
nodes and causal edges for excluded contracts, so the summary counted causal
edges over code the same report said it excluded, and everything that reads
the graph directly — order sensitivity, the mermaid rendering, campaign
edge-kind matching — saw a different contract set than everything else. On one
real target that was 52 of 69 contracts, a whole vendored Gnosis Safe tree
among them.

The fixtures are synthetic. A live protocol whose `PegOut.refund` reaches
`Collateral.slash` through an interface-typed handle; a superseded copy under
`legacy/` that makes the same call, so it is the *source* of leaked edges; and
a vendored implementation of the same interface under `test-contracts/`, which
the live handle binds to as well, so it is the *target* of a leaked edge whose
source is live. Both sides of the boundary are exercised.
"""

import pytest

from crystal.engine import research
from crystal.graphs.state import (
    CausalEdge,
    StateGraph,
    StateNode,
    StateTransition,
    build_state_graph,
)
from crystal.report import markdown, payload

LIVE = """
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

# A superseded copy of the live PegOut: same state names, same call through
# the same interface, so it produces the same transitions, nodes and edges.
LEGACY = """
pragma solidity ^0.8.20;
contract OldPegOut {
    ICollateral private _collateral;
    mapping(bytes32 => uint256) public registry;
    uint256 public count;
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

# A vendored implementation of the live interface. Named so the parser's own
# fixture classifier (`*Mock`, `mock/`, `test/`) does not catch it: only the
# `test-contracts/` directory does, which is exactly the shape that leaked.
SCAFFOLD = """
pragma solidity ^0.8.20;
contract VendoredCollateral is ICollateral {
    uint256 public slashed;
    function slash(address who, uint256 amount) external { slashed += amount; }
}
"""

EXCLUDED = {"OldPegOut", "VendoredCollateral"}
LIVE_EDGE = ("PegOut.refund", "Collateral.slash")
DEFECT_CHAIN = ["PegOut.deposit", "PegOut.refund", "Collateral.slash"]


def _write(directory, name, text):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    src = tmp_path_factory.mktemp("graph-scope") / "lbc" / "src"
    _write(src, "Flyover.sol", LIVE)
    _write(src / "legacy", "OldPegOut.sol", LEGACY)
    _write(src / "test-contracts", "VendoredCollateral.sol", SCAFFOLD)
    return src.parent


@pytest.fixture(scope="module")
def result(project):
    return research(str(project), use_solc=False, use_foundry=False)


@pytest.fixture(scope="module")
def restored(project):
    return research(str(project), use_solc=False, use_foundry=False,
                    include_tests=True)


def _leak(graph, excluded):
    """The acceptance measure: graph elements belonging to excluded contracts."""
    return (
        sum(1 for t in graph.transitions if t.contract in excluded),
        sum(1 for e in graph.causal_edges
            if e.source_contract in excluded or e.target_contract in excluded),
        sum(1 for n in graph.state_nodes if n.contract in excluded),
    )


def _keys(graph):
    """Order-carrying identity of each list, for subsequence checks."""
    return (
        [t.function for t in graph.transitions],
        [(e.source, e.target, e.source_line) for e in graph.causal_edges],
        [n.name for n in graph.state_nodes],
    )


def _is_subsequence(part, whole):
    it = iter(whole)
    return all(any(item == candidate for candidate in it) for item in part)


# -- The fixture really leaks without the filter ------------------------------

def test_the_unfiltered_graph_carries_the_excluded_contracts(restored):
    """Otherwise every assertion below would pass vacuously."""
    graph = restored["state_graph"]
    transitions, edges, nodes = _leak(graph, EXCLUDED)
    assert transitions and edges and nodes
    # Both sides of a leaked edge: a superseded source, and a vendored target
    # reached from a *live* source through the shared interface.
    assert any(e.source_contract == "OldPegOut" for e in graph.causal_edges)
    assert any(e.source_contract == "PegOut"
               and e.target_contract == "VendoredCollateral"
               for e in graph.causal_edges)


# -- The graph agrees with the rest of the result ----------------------------

def test_graph_describes_the_same_contract_set_as_the_result(result):
    excluded = {c.name for c in result["test_contracts"]}
    live = {c.name for c in result["contracts"]}
    assert EXCLUDED <= excluded and live.isdisjoint(EXCLUDED)

    graph = result["state_graph"]
    assert _leak(graph, excluded) == (0, 0, 0)

    # Every element the graph still carries belongs to a researched contract,
    # and every function it refers to is a transition it still has.
    functions = {t.function for t in graph.transitions}
    assert {t.contract for t in graph.transitions} <= live
    assert {e.source_contract for e in graph.causal_edges} <= live
    assert {e.target_contract for e in graph.causal_edges} <= live
    assert {n.contract for n in graph.state_nodes} <= live
    assert {e.source for e in graph.causal_edges} <= functions
    assert {e.target for e in graph.causal_edges} <= functions
    for node in graph.state_nodes:
        assert set(node.writers) | set(node.readers) <= functions
        assert node.writers or node.readers


def test_the_summary_and_the_rendering_agree_with_the_graph(result):
    data = payload(result)
    assert data["summary"]["causal_edges"] == len(result["state_graph"].causal_edges)
    listed = " ".join(data["excluded_test_contracts"])
    assert "OldPegOut" in listed and "VendoredCollateral" in listed

    # The mermaid state graph draws `edge.source`/`edge.target` verbatim, so an
    # excluded *function* in that section means the graph still carries it.
    # (The excluded contracts themselves are still listed, by name and file —
    # that listing is the point.)
    text = markdown(data, result)
    heading = "## State causality graph"
    assert heading in text, "the state graph is no longer rendered"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    assert "OldPegOut.refund" not in section and "OldPegOut.deposit" not in section
    assert "VendoredCollateral.slash" not in section
    assert "PegOut.refund" in section and "Collateral.slash" in section


def test_the_call_graph_agrees_too(result):
    """Rendered one section above the state graph, built from the same
    unfiltered set: a `legacy/` caller drawn there is the same contradiction."""
    live = {c.name for c in result["contracts"]}
    edges = result["call_graph"]
    assert edges
    for edge in edges:
        assert edge.source.split(".", 1)[0] in live
        assert edge.target.split(".", 1)[0] not in EXCLUDED
    assert any((e.source, e.target) == LIVE_EDGE for e in edges)

    text = markdown(payload(result), result)
    heading = "## Call graph"
    assert heading in text, "the call graph is no longer rendered"
    section = text.split(heading, 1)[1].split("\n## ", 1)[0]
    assert "OldPegOut." not in section and "VendoredCollateral." not in section
    assert "PegOut.refund" in section


# -- The chain that matters survives -----------------------------------------

def test_the_live_call_flow_edge_and_the_defect_chain_survive(result):
    """Acceptance shape: `deposit -> refund -> slash` still composes, still
    ranks above every chain containing `initialize`, and the `call-flow` edge
    that carries it across the contract boundary still exists."""
    graph = result["state_graph"]
    call_flow = [
        e for e in graph.causal_edges
        if (e.source, e.target) == LIVE_EDGE and e.edge_kind == "call-flow"
    ]
    assert len(call_flow) == 1
    assert call_flow[0].target_contract == "Collateral"

    chains = [c.chain for c in result["composition_candidates"]]
    assert DEFECT_CHAIN in chains
    rank = chains.index(DEFECT_CHAIN)
    initialize_ranks = [
        i for i, chain in enumerate(chains)
        if any("initialize" in step for step in chain)
    ]
    assert all(rank < other for other in initialize_ranks)
    # Build 012's guarantee, restated: no candidate contains the one-shot.
    assert not initialize_ranks


# -- --include-tests restores the graph, exactly as everything else -----------

def test_include_tests_restores_the_graph(result, restored):
    graph = restored["state_graph"]
    assert all(count > 0 for count in _leak(graph, EXCLUDED))
    assert EXCLUDED <= {t.contract for t in graph.transitions}

    # Restored means untouched: identical to a fresh build over the restored
    # contract set, element for element and in the same order.
    assert _keys(graph) == _keys(build_state_graph(restored["contracts"]))

    # And the filtered graph is a stable subsequence of it — the filter never
    # re-sorts, so every position-budgeted consumer sees the same order.
    for part, whole in zip(_keys(result["state_graph"]), _keys(graph)):
        assert part and _is_subsequence(part, whole)

    # The call graph comes back the same way.
    calls = [(e.source, e.target, e.line) for e in restored["call_graph"]]
    assert any(source.startswith("OldPegOut.") for source, _, _ in calls)
    assert any(target.startswith("VendoredCollateral.") for _, target, _ in calls)
    assert _is_subsequence(
        [(e.source, e.target, e.line) for e in result["call_graph"]], calls
    )


# -- StateGraph.without_contracts, on hand-built graphs ----------------------

def _transition(function, contract="", **kwargs):
    return StateTransition(function=function, contract=contract, **kwargs)


def _edge(source, target, source_contract="", target_contract="", line=0):
    return CausalEdge(
        source=source, target=target, produced=(), consumed=(), score=0.6,
        source_contract=source_contract, target_contract=target_contract,
        source_line=line,
    )


def _hand_built():
    return StateGraph(
        transitions=[
            _transition("A.f", "A", visibility="external", writes={"A::x"}),
            _transition("B.g", "B", visibility="external", writes={"A::x", "B::y"}),
            _transition("A.h", "A", visibility="external", reads={"A::x"}),
        ],
        causal_edges=[
            _edge("A.f", "B.g", "A", "B", line=1),
            _edge("A.f", "A.h", "A", "A", line=2),
            _edge("B.g", "A.h", "B", "A", line=3),
        ],
        state_nodes=[
            StateNode("A::x", "A", "storage", writers=("A.f", "B.g"), readers=("A.h",)),
            StateNode("A::z", "A", "storage", writers=("B.g",)),
            StateNode("B::y", "B", "storage", writers=("B.g",)),
            StateNode("A::untouched", "A", "storage"),
        ],
    )


def test_without_contracts_drops_every_element_of_the_excluded_contract():
    graph = _hand_built()
    filtered = graph.without_contracts({"B"})

    assert [t.function for t in filtered.transitions] == ["A.f", "A.h"]
    assert [(e.source, e.target) for e in filtered.causal_edges] == [("A.f", "A.h")]
    assert [n.name for n in filtered.state_nodes] == ["A::x", "A::untouched"]

    # Writers/readers are pruned to surviving functions; a node only excluded
    # functions touched (`A::z`) goes with them, but a hand-built node with no
    # recorded touches is not mistaken for one.
    node = filtered.state_nodes[0]
    assert node.writers == ("A.f",) and node.readers == ("A.h",)

    # A transition's own reads/writes are its behaviour: left untouched.
    assert filtered.transitions[0].writes == {"A::x"}


def test_without_contracts_returns_a_new_graph_and_preserves_order():
    graph = _hand_built()
    before = _keys(graph)
    filtered = graph.without_contracts({"B"})
    assert filtered is not graph
    assert _keys(graph) == before
    for part, whole in zip(_keys(filtered), before):
        assert _is_subsequence(part, whole)

    same = graph.without_contracts(set())
    assert same is not graph and _keys(same) == before
    assert [n.writers for n in same.state_nodes] == [n.writers for n in graph.state_nodes]


def test_without_contracts_falls_back_to_the_function_prefix():
    """Elements that carry no contract field are still `Contract.function`."""
    graph = StateGraph(
        transitions=[_transition("A.f"), _transition("B.g")],
        causal_edges=[_edge("A.f", "B.g"), _edge("B.g", "A.f"), _edge("A.f", "A.f")],
        state_nodes=[
            StateNode("A::x", "", "storage", writers=("A.f", "B.g")),
            StateNode("B::y", "", "storage", writers=("B.g",)),
        ],
    )
    filtered = graph.without_contracts({"B"})
    assert [t.function for t in filtered.transitions] == ["A.f"]
    assert [(e.source, e.target) for e in filtered.causal_edges] == [("A.f", "A.f")]
    assert [(n.name, n.writers) for n in filtered.state_nodes] == [("A::x", ("A.f",))]


def test_without_contracts_excludes_by_name_like_every_other_filter():
    """Sequence steps and detector signals are excluded by contract *name*, and
    a graph function is `Contract.function`, so a live contract that shares its
    name with an excluded copy is excluded here too — consistently, not
    silently kept in one output and dropped from the rest."""
    graph = StateGraph(transitions=[
        _transition("Quotes.hash", "Quotes", path="src/legacy/Quotes.sol"),
        _transition("Quotes.hash", "Quotes", path="src/libraries/Quotes.sol"),
        _transition("PegOut.refund", "PegOut", path="src/PegOut.sol"),
    ])
    filtered = graph.without_contracts({"Quotes"})
    assert [t.function for t in filtered.transitions] == ["PegOut.refund"]
