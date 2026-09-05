"""Scaffolding leaves research once, by path, before anything is built.

Build 012 excluded scaffolding *after* the pipeline had already built the state
graph, the sequence candidates and the protocol model from the full set, and it
excluded by contract *name*. Both were wrong in ways only a real target showed:

* **Name is not identity.** `Quotes` and `SignatureValidator` exist in both
  `libraries/` (live) and `legacy/` (superseded) on a real protocol, so
  excluding the name dropped the live libraries with the dead ones. A stateful
  contract in that position would lose its graph silently.

* **Filtering afterwards spends the budget first.** Sequence candidates are
  capped at 250 and were generated from the unfiltered graph; on that target
  156 of the 250 were chains through vendored Gnosis Safe code, so only 94 live
  candidates ever reached research. The cap was being spent on code the report
  said had been excluded.

Both are fixed at a single point: contracts leave research in
`crystal.engine.research`, by path, before anything is derived from them.
"""

from crystal.engine import research


def _write(root, relative, text):
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _project(tmp_path):
    """A live library and a superseded copy that share a name."""
    root = tmp_path / "src"
    _write(root, "libraries/Shared.sol", """
pragma solidity ^0.8.20;
library Shared {
    function scale(uint256 a) internal pure returns (uint256) { return a * 2; }
}
""")
    _write(root, "legacy/Shared.sol", """
pragma solidity ^0.8.20;
library Shared {
    function scale(uint256 a) internal pure returns (uint256) { return a; }
}
""")
    _write(root, "Vault.sol", """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public total;
    function deposit(uint256 a) external { total += a; }
    function withdraw(uint256 a) external { total -= a; }
}
""")
    _write(root, "legacy/OldVault.sol", """
pragma solidity ^0.8.20;
contract OldVault {
    uint256 public total;
    function deposit(uint256 a) external { total += a; }
}
""")
    return root


def test_a_live_contract_is_not_dropped_for_sharing_a_name(tmp_path):
    result = research(str(_project(tmp_path)), use_solc=False, use_foundry=False)
    kept = {c.name: c.path for c in result["contracts"]}
    assert "Shared" in kept, "the live library was dropped with its legacy namesake"
    assert "libraries" in kept["Shared"].replace("\\", "/")
    assert "OldVault" not in kept


def test_the_superseded_copy_is_excluded_with_a_reason(tmp_path):
    result = research(str(_project(tmp_path)), use_solc=False, use_foundry=False)
    reasons = {
        (row["contract"], row["path"].replace("\\", "/").split("src/")[-1]):
            row["reason"]
        for row in result["excluded_scaffolding"]
    }
    assert ("OldVault", "legacy/OldVault.sol") in reasons
    assert ("Shared", "legacy/Shared.sol") in reasons
    assert all(reasons.values()), "an exclusion without a reason is invisible"
    # The live copy is not in the excluded list at all.
    assert ("Shared", "libraries/Shared.sol") not in reasons


def test_nothing_downstream_is_built_from_excluded_code(tmp_path):
    """The graph, the sequences and the model describe one contract set.

    Filtering after the fact left every one of them describing a different set
    than the report did.
    """
    result = research(str(_project(tmp_path)), use_solc=False, use_foundry=False)
    live = {f"{c.name}.{f.name}" for c in result["contracts"] for f in c.functions}

    for transition in result["state_graph"].transitions:
        assert transition.function in live, transition.function
    for edge in result["state_graph"].causal_edges:
        assert edge.source in live and edge.target in live
    for hypothesis in result["sequence_hypotheses"]:
        for step in hypothesis.sequence:
            assert step in live, step
    for delta in result["state_deltas"]:
        for step in delta.sequence:
            assert step in live, step


def test_include_tests_restores_the_excluded_code(tmp_path):
    result = research(str(_project(tmp_path)), use_solc=False,
                      use_foundry=False, include_tests=True)
    names = [c.name for c in result["contracts"]]
    assert "OldVault" in names
    assert names.count("Shared") == 2
    assert result["excluded_scaffolding"] == []
