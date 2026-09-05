"""Cross-contract state namespacing.

A state variable is unique only inside its contract. Before namespacing, every
stage keyed on the bare name, so two contracts that both declare `balances`
were one variable to the whole engine:

    deltas  = {'balances': 'ARG:a#1 - ARG:a#2'}
    edges   = [('Alpha.credit','Beta.debit',('balances',)), ...]
    nodes   = [('balances', 'Beta')]

That delta reads as a conservation law across a sequence where two unrelated
protocols each moved money, the edges were invented out of a spelling
coincidence, and Alpha's node was silently overwritten by Beta's. Every test
here pins one of those failure modes.
"""

from crystal.graphs.state import build_state_graph, classify_state
from crystal.naming import (
    StateNamespace,
    bare_name,
    contract_of,
    is_qualified,
    qualify,
    same_contract,
)
from crystal.parsers import solidity_regex
from crystal.research.delta_anomalies import detect_delta_anomalies
from crystal.research.statedelta import StateDelta
from crystal.symbolic import SymbolicEngine

COLLIDING = """
pragma solidity ^0.8.20;
contract Alpha {
    uint256 public balances;
    uint256 public totalAssets;
    function credit(uint256 a) external { balances += a; totalAssets += a; }
}
contract Beta {
    uint256 public balances;
    uint256 public totalSupply;
    function debit(uint256 a) external { balances -= a; totalSupply -= a; }
}
"""

SINGLE = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    function deposit(uint256 a) external { totalAssets += a; }
    function withdraw(uint256 a) external { totalAssets -= a; }
}
"""


def _contracts(source):
    return solidity_regex.parse_text(source, "X.sol")


def _delta(delta_map, sequence=("Alpha.credit", "Beta.debit")):
    return StateDelta(
        sequence=list(sequence),
        before={name: "0" for name in delta_map},
        after=dict(delta_map),
        delta=dict(delta_map),
        touched=sorted(delta_map),
        changed=sorted(k for k, v in delta_map.items() if v != "0"),
        confidence=0.80,
    )


# -- naming primitives -----------------------------------------------------

def test_qualify_and_bare_name_round_trip():
    assert qualify("Vault", "balances") == "Vault::balances"
    assert bare_name("Vault::balances") == "balances"
    assert contract_of("Vault::balances") == "Vault"


def test_qualification_survives_an_access_path():
    qualified = qualify("Vault", "balances[msg.sender]")
    assert qualified == "Vault::balances[msg.sender]"
    assert bare_name(qualified) == "balances[msg.sender]"


def test_qualify_is_idempotent_and_tolerates_missing_context():
    assert qualify("Vault", "Vault::balances") == "Vault::balances"
    assert qualify("", "balances") == "balances"
    assert bare_name("balances") == "balances"
    assert contract_of("balances") == ""
    assert not is_qualified("balances")


def test_bare_name_splits_on_the_first_separator_only():
    # Rust paths carry `::` of their own; qualification adds exactly one prefix.
    assert bare_name("runtime::TotalSupply::<T>") == "TotalSupply::<T>"
    assert contract_of("runtime::TotalSupply::<T>") == "runtime"


def test_same_contract_separates_lookalike_slots():
    assert same_contract("Vault::totalAssets", "Vault::totalSupply")
    assert not same_contract("Vault::totalAssets", "Registry::totalSupply")


# -- inheritance -----------------------------------------------------------

def test_inherited_state_resolves_to_the_declaring_contract():
    """An inherited variable is one storage slot, not two.

    Without this, namespacing would split `Parent::balances` from
    `Child::balances` and drop every edge crossing the inheritance boundary.
    """
    namespace = StateNamespace(_contracts("""
        pragma solidity ^0.8.20;
        contract Base { uint256 public balances; }
        contract Derived is Base { uint256 public drained; }
    """))
    assert namespace.qualify("Derived", "balances") == "Base::balances"
    assert namespace.qualify("Base", "balances") == "Base::balances"
    assert namespace.qualify("Derived", "drained") == "Derived::drained"


def test_a_name_declared_nowhere_stays_with_its_accessor():
    """A base contract outside the scanned set must not merge two children.

    Two ERC20 subclasses both touching an unscanned `_balances` are different
    deployments, so attributing the slot to each accessor is the safe default.
    """
    namespace = StateNamespace(_contracts("""
        pragma solidity ^0.8.20;
        contract A { function f() external {} }
        contract B { function g() external {} }
    """))
    assert namespace.qualify("A", "_balances") == "A::_balances"
    assert namespace.qualify("B", "_balances") == "B::_balances"


# -- the symbolic engine ---------------------------------------------------

def test_cross_contract_sequence_keeps_identical_names_apart():
    """The headline bug: two contracts' `balances` netting to a false zero."""
    effect = SymbolicEngine(_contracts(COLLIDING)).execute_sequence(
        ["Alpha.credit", "Beta.debit"]
    )
    assert effect.deltas["Alpha::balances"] == "ARG:a#1"
    assert effect.deltas["Beta::balances"] == "-ARG:a#2"
    assert "balances" not in effect.deltas
    assert set(effect.touched) == {
        "Alpha::balances", "Alpha::totalAssets",
        "Beta::balances", "Beta::totalSupply",
    }


def test_cross_contract_initial_symbols_are_distinct():
    effect = SymbolicEngine(_contracts(COLLIDING)).execute_sequence(
        ["Alpha.credit", "Beta.debit"]
    )
    assert effect.before["Alpha::balances"] == "S0:Alpha::balances"
    assert effect.before["Beta::balances"] == "S0:Beta::balances"


def test_one_contract_sequence_still_shares_a_slot():
    """Namespacing must separate contracts without splitting a single one."""
    effect = SymbolicEngine(_contracts(SINGLE)).execute_sequence(
        ["Vault.deposit", "Vault.withdraw"]
    )
    assert effect.deltas == {"Vault::totalAssets": "ARG:a#1 - ARG:a#2"}


def test_single_function_effects_are_not_namespaced():
    """A function effect never spans contracts, so it keeps the bare names the
    rest of that contract's model — reads, writes, state_vars — is keyed by."""
    engine = SymbolicEngine(_contracts(COLLIDING))
    effect = engine.execute_function(engine.functions["Alpha.credit"])
    assert set(effect.deltas) == {"balances", "totalAssets"}


# -- the causal state graph ------------------------------------------------

def test_a_name_collision_does_not_invent_a_causal_edge():
    """Alpha and Beta share only a spelling, so they share no causal edge."""
    graph = build_state_graph(_contracts(COLLIDING))
    crossing = [
        edge for edge in graph.causal_edges
        if edge.source_contract != edge.target_contract
    ]
    assert crossing == []


def test_state_nodes_are_not_overwritten_by_a_same_named_variable():
    graph = build_state_graph(_contracts(COLLIDING))
    owners = {node.name: node.contract for node in graph.state_nodes}
    assert owners["Alpha::balances"] == "Alpha"
    assert owners["Beta::balances"] == "Beta"


def test_transitions_carry_qualified_reads_and_writes():
    graph = build_state_graph(_contracts(COLLIDING))
    credit = next(t for t in graph.transitions if t.function == "Alpha.credit")
    assert credit.writes == {"Alpha::balances", "Alpha::totalAssets"}


# -- classification is namespace-blind -------------------------------------

def test_classify_state_ignores_the_namespace():
    """Category is a question about meaning, not identity."""
    for name in ("balances", "Vault::balances", "Registry::balances"):
        assert classify_state(name) == "balance"
    assert classify_state("Registry::owner") == "ownership"
    assert classify_state("Session::nonce") == "nonce"


# -- accounting pairs ------------------------------------------------------

DECLARED = {
    "Vault::totalAssets", "Vault::totalSupply",
    "Registry::totalAssets", "Registry::totalSupply",
}


def test_accounting_pair_binds_within_one_contract():
    anomalies = detect_delta_anomalies([_delta({
        "Vault::totalAssets": "ARG:a#1 + ARG:a#2",
        "Vault::totalSupply": "ARG:a#1",
    })], None, DECLARED)
    asymmetries = [a for a in anomalies if a.kind == "asset-share-asymmetry"]
    assert asymmetries
    assert asymmetries[0].state == "Vault::totalAssets"
    assert asymmetries[0].counterpart == "Vault::totalSupply"


def test_namespacing_is_declared_as_a_capability():
    from crystal.cli import CAPABILITIES
    assert "cross-contract-state-namespacing" in CAPABILITIES


def test_accounting_pair_does_not_bind_across_contracts():
    """One contract's assets against another's supply is not a relation.

    Each side is instead compared against its own contract's counterpart, so
    the two movements are reported as two one-sided mutations rather than one
    fabricated asymmetry between unrelated protocols.
    """
    anomalies = detect_delta_anomalies([_delta({
        "Vault::totalAssets": "ARG:a#1",
        "Registry::totalSupply": "-ARG:b#2",
    })], None, DECLARED)
    assert not [a for a in anomalies if a.kind == "asset-share-asymmetry"]

    by_kind = {a.kind: a for a in anomalies}
    assert by_kind["asset-only-mutation"].state == "Vault::totalAssets"
    assert by_kind["asset-only-mutation"].counterpart == "Vault::totalSupply"
    assert by_kind["share-only-mutation"].state == "Registry::totalSupply"
    assert by_kind["share-only-mutation"].counterpart == "Registry::totalAssets"
