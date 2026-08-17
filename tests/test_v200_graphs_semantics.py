from crystal.graphs.callgraph import build_call_graph, summarize
from crystal.graphs.cfg import build_cfg
from crystal.graphs.storage import build_storage_graph
from crystal.parsers import solidity_regex
from crystal.semantics.dataflow import build_dataflow
from crystal.semantics.inheritance import (
    build_inheritance_graph,
    linearize,
    link_inheritance,
    resolve,
)
from crystal.semantics.modifiers import build_modifier_graph, modifier_definitions
from crystal.semantics.proxy import detect_proxy_signals, report

SOURCE = """
pragma solidity ^0.8.20;
contract Base {
    uint256 public seed;
    function bump() public { seed += 1; }
}
contract Mid is Base {
    uint256 public counter;
}
contract Top is Mid {
    address public implementation;
    bool public paused;

    modifier onlyAdmin() { require(msg.sender == implementation); _; }
    modifier freeform() { _; }

    function upgradeTo(address x) external onlyAdmin { implementation = x; }
    function bump() public { counter += 1; seed += 2; }
    function branchy(uint256 x) external {
        if (x > 10) { counter += 1; } else { counter += 2; }
        for (uint256 i = 0; i < 3; i++) { seed += 1; }
        if (paused) { revert("paused"); }
        counter += x;
    }
    function reach() external { bump(); helper(); }
    function helper() internal { seed += 1; }
    function initialize(address x) external { implementation = x; }
}
"""

PROXY = """
pragma solidity ^0.8.20;
contract P {
    function fwd(address impl, bytes calldata data) external {
        (bool ok, ) = impl.delegatecall(data);
        require(ok);
    }
}
"""


def parsed():
    contracts = solidity_regex.parse_text(SOURCE, "T.sol")
    link_inheritance(contracts)
    return contracts


# -- CFG -------------------------------------------------------------------

def test_cfg_has_real_basic_blocks():
    top = next(c for c in parsed() if c.name == "Top")
    cfg = build_cfg(next(f for f in top.functions if f.name == "branchy"))
    assert cfg.model == "ir-basic-blocks"
    assert cfg.nodes[0].kind == "entry"
    assert cfg.nodes[-1].kind == "exit"
    assert cfg.branch_count >= 2
    labels = {label for _, _, label in cfg.edges}
    assert {"true", "false"} <= labels
    assert "loop-back" in labels
    assert cfg.cyclomatic_complexity > 1


def test_cfg_marks_terminators_and_calls():
    top = next(c for c in parsed() if c.name == "Top")
    cfg = build_cfg(next(f for f in top.functions if f.name == "branchy"))
    assert any(node.terminator == "revert" for node in cfg.nodes)


def test_cfg_falls_back_without_ir():
    top = next(c for c in parsed() if c.name == "Top")
    function = next(f for f in top.functions if f.name == "bump")
    function.ir = None
    cfg = build_cfg(function)
    assert cfg.model == "text-approximation"
    assert cfg.nodes


# -- call graph ------------------------------------------------------------

def test_call_graph_resolves_internal_calls():
    edges = build_call_graph(parsed())
    pairs = {(e.source, e.target) for e in edges}
    assert ("Top.reach", "Top.helper") in pairs
    assert ("Top.reach", "Top.bump") in pairs
    edge = next(e for e in edges if e.target == "Top.helper")
    assert edge.resolved and edge.line > 0


def test_call_graph_keeps_unresolved_external_targets():
    contracts = solidity_regex.parse_text(PROXY, "P.sol")
    summary = summarize(contracts)
    assert summary.unresolved
    assert any("delegatecall" in e.target for e in summary.unresolved)
    assert "P.fwd" in summary.external_entry_points


# -- storage ---------------------------------------------------------------

def test_storage_graph_has_layout_and_located_edges():
    graph = build_storage_graph(parsed())
    assert graph
    assert len(graph) == len(graph.edges)
    slots = {(s.contract, s.name): s for s in graph.layout}
    assert slots[("Top", "implementation")].bits == 160
    assert slots[("Top", "paused")].bits == 8
    # address (160) + bool (8) share one slot.
    assert slots[("Top", "implementation")].slot == slots[("Top", "paused")].slot
    assert slots[("Top", "paused")].offset == 160
    assert all(edge.line > 0 for edge in graph.edges if edge.kind == "write")


# -- dataflow --------------------------------------------------------------

def test_dataflow_marks_attacker_controlled_writes():
    edges = build_dataflow(parsed())
    tainted = [e for e in edges if e.kind == "tainted-write"]
    assert tainted
    assert any(e.variable.endswith("counter") and e.tainted for e in tainted)
    assert any(e.source.startswith("ARG:") for e in tainted)


# -- inheritance -----------------------------------------------------------

def test_inheritance_graph_includes_transitive_edges():
    edges = build_inheritance_graph(parsed())
    pairs = {(e.child, e.parent, e.kind) for e in edges}
    assert ("Top", "Mid", "inherits") in pairs
    assert ("Top", "Base", "inherits-transitively") in pairs


def test_linearization_order():
    assert linearize("Top", parsed()) == ["Top", "Mid", "Base"]


def test_link_inheritance_attributes_base_state():
    contracts = solidity_regex.parse_text(SOURCE, "T.sol")
    top = next(c for c in contracts if c.name == "Top")
    assert "seed" not in {v.name for v in top.state_vars}

    link_inheritance(contracts)
    top = next(c for c in contracts if c.name == "Top")
    assert {"seed", "counter"} <= {v.name for v in top.state_vars}
    bump = next(f for f in top.functions if f.name == "bump")
    assert "seed" in bump.writes


def test_resolution_reports_overrides():
    resolution = next(r for r in resolve(parsed()) if r.contract == "Top")
    assert ("Top.bump", "Base.bump") in resolution.overrides
    assert not resolution.unresolved_bases


# -- modifiers -------------------------------------------------------------

def test_modifier_graph_classifies_guards():
    edges = {(e.function, e.modifier): e for e in build_modifier_graph(parsed())}
    upgrade = edges[("Top.upgradeTo", "onlyAdmin")]
    assert upgrade.guard_kind == "authority"
    assert upgrade.definition == "Top.onlyAdmin"


def test_modifier_definitions_are_listed():
    names = {d.name for d in modifier_definitions(parsed())}
    assert {"onlyAdmin", "freeform"} <= names


# -- proxy -----------------------------------------------------------------

def test_proxy_signals_include_naming_and_initializer():
    kinds = {s.kind for s in detect_proxy_signals(parsed())}
    assert "naming" in kinds
    assert "initializer" in kinds


def test_proxy_report_finds_delegatecall():
    result = report(solidity_regex.parse_text(PROXY, "P.sol"))
    assert result.delegatecall_sites
    assert any(s.kind == "delegatecall" for s in result.signals)
