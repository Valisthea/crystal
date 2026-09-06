"""Build 015 — the three traps are found before any backend is launched.

Measured on one protocol on 2026-09-05: the repository declares `Quotes.sol`
twice and both Medusa and Halmos linked the wrong one (5 sound Medusa runs out
of 10); a harness whose actors were never funded produced 35 greens over a
million calls; Halmos pinned `block.number` to 1 and passed every property
behind a block delay. The replica below rebuilds each trap in `tmp_path`; the
last test runs against the real target when it is present on the machine.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from crystal import process
from crystal.backends import (
    BACKENDS,
    echidna,
    halmos,
    medusa,
    preflight,
    preflight_report,
    run_backend,
    traits,
)
from crystal.backends.base import REFUSED, BackendCapabilities, derive_properties
from crystal.backends.preflight import (
    FROZEN_BLOCK,
    HOMONYM,
    INFO,
    NO_MUTATION,
    REFUSE,
    UNSUPPORTED,
    WARN,
    ZERO_BALANCE,
    find_homonyms,
    harness_actions,
)
from crystal.backends.verdict import UNSUPPORTED as VERDICT_UNSUPPORTED
from crystal.parsers import solidity_regex
from crystal.protocol.invariants import derive_protocol_invariants
from crystal.protocol.model import build_protocol_model

REAL_TARGET = Path("C:/Users/admin/Desktop/Vercel Sandbox/rootstock/lbc")

LEGACY_QUOTES = """// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;
library Quotes {
    struct PeginQuote { bytes20 fedBtcAddress; address lbcAddress; uint256 value; }
    function hashQuote(PeginQuote memory q) internal pure returns (bytes32) { return keccak256(abi.encode(q)); }
}
"""

CURRENT_QUOTES = """// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;
library Quotes {
    struct PegInQuote { uint256 chainId; uint256 callFee; uint256 value; address lbcAddress; }
    function hashQuote(PegInQuote memory q) internal pure returns (bytes32) { return keccak256(abi.encode(q)); }
}
"""

PEGIN = """// SPDX-License-Identifier: MIT
pragma solidity 0.8.25;
import {Quotes} from "./libraries/Quotes.sol";
contract PegInContract {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public nonce;
    function deposit(uint256 assets) external payable {
        require(msg.value >= assets, "underpaid");
        totalAssets += assets; totalSupply += assets; nonce += 1;
    }
}
"""

VAULT = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public nonce;
    function deposit(uint256 assets) external payable { totalAssets += assets; totalSupply += assets; nonce += 1; }
    function withdraw(uint256 assets) external { totalAssets -= assets; totalSupply -= assets; }
}
"""

DELAYED = """
pragma solidity ^0.8.20;
contract Collateral {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public nonce;
    mapping(address => uint256) public since;
    uint256 public delay;
    function resign() external { since[msg.sender] = block.number; nonce += 1; }
    function withdraw() external { _withdraw(msg.sender); }
    function _withdraw(address who) internal {
        if (block.number - since[who] < delay) revert("too early");
        totalAssets -= 1; totalSupply -= 1;
    }
    function add() external payable { totalAssets += msg.value; totalSupply += msg.value; }
}
"""

ONLY_DELAYED = """
pragma solidity ^0.8.20;
contract Locked {
    uint256 public nonce;
    uint256 public unlockBlock;
    function bump() external { require(block.number > unlockBlock, "locked"); nonce += 1; }
}
"""


def write_trap(root: Path) -> None:
    (root / "src" / "libraries").mkdir(parents=True)
    (root / "src" / "legacy").mkdir(parents=True)
    (root / "src" / "libraries" / "Quotes.sol").write_text(CURRENT_QUOTES, encoding="utf-8")
    (root / "src" / "legacy" / "Quotes.sol").write_text(LEGACY_QUOTES, encoding="utf-8")
    (root / "src" / "PegInContract.sol").write_text(PEGIN, encoding="utf-8")
    # The artifact tree as Foundry leaves it: the cache maps both sources to the
    # artifact name, and the by-name artifact was built from the legacy copy.
    (root / "cache").mkdir()
    (root / "cache" / "solidity-files-cache.json").write_text(json.dumps({
        "files": {
            "src/legacy/Quotes.sol": {"artifacts": {"Quotes": {}}},
            "src/libraries/Quotes.sol": {"artifacts": {"Quotes": {}}},
            "src/PegInContract.sol": {"artifacts": {"PegInContract": {}}},
        }
    }), encoding="utf-8")
    (root / "out" / "Quotes.sol").mkdir(parents=True)
    (root / "out" / "Quotes.sol" / "Quotes.json").write_text(json.dumps({
        "abi": [], "ast": {"absolutePath": "src/legacy/Quotes.sol"},
    }), encoding="utf-8")


def parse(root: Path, name: str, source: str):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return solidity_regex.parse_text(source, str(path))


def invariants(parsed):
    return derive_protocol_invariants(build_protocol_model(parsed))


# -- Homonym artifacts -----------------------------------------------------------------

def test_homonyms_are_found_in_the_tree_as_it_stands(tmp_path):
    write_trap(tmp_path)
    homonyms = {item.name: item for item in find_homonyms(tmp_path)}
    assert set(homonyms) == {"Quotes"}
    quotes = homonyms["Quotes"]
    assert quotes.paths == ("src/legacy/Quotes.sol", "src/libraries/Quotes.sol")
    assert quotes.kinds == ("library",)
    assert not quotes.identical
    assert quotes.linked == "src/legacy/Quotes.sol"
    assert quotes.artifact == "out/Quotes.sol/Quotes.json"


def test_homonyms_are_found_from_sources_alone_when_scoped_to_a_subdirectory(tmp_path):
    """Scoping the scan to `src/` puts the artifact tree out of reach, so the
    source pass is the only one that can answer. It used to prune `legacy/`
    with the *research* fixture filter and find nothing — silently missing the
    one collision the check exists for, on the tree the compiler still builds."""
    write_trap(tmp_path)
    homonyms = {item.name: item for item in find_homonyms(tmp_path / "src")}
    assert "Quotes" in homonyms, "the source pass must see legacy/ and libraries/"
    quotes = homonyms["Quotes"]
    assert quotes.paths == ("legacy/Quotes.sol", "libraries/Quotes.sol")
    assert not quotes.identical


def test_homonym_refuses_the_contract_that_links_it_before_any_launch(tmp_path, monkeypatch):
    write_trap(tmp_path)
    parsed = solidity_regex.parse_text(PEGIN, str(tmp_path / "src" / "PegInContract.sol"))
    launched = []
    monkeypatch.setattr(process, "run", lambda *args, **kwargs: launched.append(args) or process.ProcessResult(0, "", "", True))
    monkeypatch.setattr(medusa, "detect", lambda: BackendCapabilities("medusa", True, "1.5.1", "ok"))
    results = run_backend("medusa", tmp_path, parsed, invariants(parsed))
    assert launched == [], "medusa was launched despite the homonym"
    assert len(results) == 1
    result = results[0]
    assert result.status == REFUSED
    assert "src/legacy/Quotes.sol" in result.reason and "src/libraries/Quotes.sol" in result.reason
    assert "PegInContract imports src/libraries/Quotes.sol" in result.reason
    assert "not the src/legacy/Quotes.sol the artifact tree linked" in result.reason
    assert any(line.startswith("[REFUSE] homonym") for line in result.preflight)
    assert result.verdicts == ()
    assert not result.reproducible


def test_homonym_is_reported_even_when_the_tool_is_absent(tmp_path, monkeypatch):
    write_trap(tmp_path)
    parsed = solidity_regex.parse_text(PEGIN, str(tmp_path / "src" / "PegInContract.sol"))
    monkeypatch.setattr(halmos, "detect", lambda: BackendCapabilities("halmos", False, None, "halmos executable not installed"))
    result = run_backend("halmos", tmp_path, parsed, invariants(parsed))[0]
    assert result.status == REFUSED
    assert "Quotes" in result.reason


def test_unrelated_and_identical_homonyms_only_warn(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    (tmp_path / "a" / "Lib.sol").write_text("pragma solidity ^0.8.20;\nlibrary Lib { function f() internal pure {} }\n", encoding="utf-8")
    (tmp_path / "b" / "Lib.sol").write_text("pragma solidity ^0.8.20;\nlibrary Lib { function f() internal pure {} }\n", encoding="utf-8")
    parsed = parse(tmp_path, "Vault.sol", VAULT)
    report = preflight_report(tmp_path, parsed)
    assert [issue.severity for issue in report.issues] == [WARN]
    assert report.homonyms[0].identical
    assert report.blocking() == ()


def test_comments_do_not_declare_units(tmp_path):
    (tmp_path / "A.sol").write_text("pragma solidity ^0.8.20;\n// contract Ghost {}\ncontract Real {}\n", encoding="utf-8")
    (tmp_path / "B.sol").write_text("pragma solidity ^0.8.20;\n/* contract Ghost {} */\ncontract Other {}\n", encoding="utf-8")
    assert find_homonyms(tmp_path) == ()


def test_generate_only_carries_preflight_without_refusing(tmp_path):
    write_trap(tmp_path)
    parsed = solidity_regex.parse_text(PEGIN, str(tmp_path / "src" / "PegInContract.sol"))
    result = run_backend("medusa", tmp_path, parsed, invariants(parsed), generate_only=True)[0]
    assert result.status == "GENERATED"
    assert result.artifacts
    assert "Quotes" in result.reason
    assert any(HOMONYM in line for line in result.preflight)


# -- Zero-balance actors ----------------------------------------------------------------

def test_zero_balance_actors_are_detected_from_the_harness(tmp_path):
    parsed = parse(tmp_path, "Vault.sol", VAULT)
    properties, _ = derive_properties(parsed[0], invariants(parsed))
    unfunded = """
    contract Harness {
        Vault internal target;
        constructor() { target = new Vault(); }
        function h_deposit(uint256 a) public { target.deposit(a); }
        function property_x() public view returns (bool) { return true; }
    }
    """
    issues = preflight.funding_issues("medusa", medusa.TRAITS, parsed[0], properties, unfunded, medusa.config("Harness"), parsed)
    # the only forwarded action is payable and unfunded: nothing can be witnessed
    assert [(issue.kind, issue.severity) for issue in issues] == [(ZERO_BALANCE, REFUSE)]
    text = issues[0].message
    assert "Vault.deposit" in text and "payable" in text and "never forwards msg.value" in text
    # with a non-payable action also forwarded, the properties can still be decided through it
    partial = unfunded.replace(
        "function property_x()", "function h_withdraw(uint256 a) public { target.withdraw(a); }\n        function property_x()",
    )
    issues = preflight.funding_issues("medusa", medusa.TRAITS, parsed[0], properties, partial, medusa.config("Harness"), parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(ZERO_BALANCE, WARN)]
    assert "decided only through the non-payable ones" in issues[0].message
    funded = unfunded.replace("public { target.deposit(a); }", "public payable { target.deposit{value: msg.value}(a); }")
    assert preflight.funding_issues("medusa", medusa.TRAITS, parsed[0], properties, funded, medusa.config("Harness"), parsed) == []


def test_zero_balance_refuses_when_no_property_can_be_witnessed(tmp_path):
    source = VAULT.replace("function withdraw(uint256 assets) external { totalAssets -= assets; totalSupply -= assets; }", "")
    parsed = parse(tmp_path, "Vault.sol", source)
    properties, _ = derive_properties(parsed[0], invariants(parsed))
    unfunded = "contract Harness { Vault internal target; function h_deposit(uint256 a) public { target.deposit(a); } }"
    issues = preflight.funding_issues("echidna", echidna.TRAITS, parsed[0], properties, unfunded, echidna.config(), parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(ZERO_BALANCE, REFUSE)]
    assert "can only be VACUOUS" in issues[0].message


def test_zero_balance_actors_are_detected_from_the_config(tmp_path):
    parsed = parse(tmp_path, "Vault.sol", VAULT)
    properties, _ = derive_properties(parsed[0], invariants(parsed))
    funded_harness = "contract H { function h_deposit(uint256 a) public payable { target.deposit{value: msg.value}(a); } }"
    issues = preflight.funding_issues("echidna", echidna.TRAITS, parsed[0], properties, funded_harness, "testLimit: 10\nbalanceAddr: 0\n", parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(ZERO_BALANCE, REFUSE)]
    assert "balanceAddr: 0" in issues[0].message
    config = medusa.config("H")
    config["fuzzing"]["senderAddresses"] = []
    issues = preflight.funding_issues("medusa", medusa.TRAITS, parsed[0], properties, funded_harness, config, parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(ZERO_BALANCE, REFUSE)]


def test_generated_fuzz_harnesses_fund_their_actors(tmp_path):
    parsed = parse(tmp_path, "Vault.sol", VAULT)
    for backend in ("medusa", "echidna"):
        result = run_backend(backend, tmp_path, parsed, invariants(parsed), generate_only=True)[0]
        assert not any(ZERO_BALANCE in line for line in result.preflight), result.preflight


def test_a_harness_without_mutating_entry_points_is_refused_for_fuzzers(tmp_path):
    parsed = parse(tmp_path, "Vault.sol", VAULT)
    view_only = """
    contract CrystalMedusaHarness {
        Vault internal target;
        constructor() { target = new Vault(); }
        function property_x() public view returns (bool) { return true; }
    }
    """
    assert harness_actions(view_only) == ()
    issues = preflight.mutation_issues("medusa", medusa.TRAITS, parsed[0], view_only, medusa.config("X"))
    assert [(issue.kind, issue.severity) for issue in issues] == [(NO_MUTATION, REFUSE)]
    assert preflight.mutation_issues("halmos", halmos.TRAITS, parsed[0], view_only, None) == []
    assert preflight.mutation_issues("echidna", echidna.TRAITS, parsed[0], view_only, "allContracts: true\n") == []


# -- Frozen block -----------------------------------------------------------------------

def test_frozen_block_marks_properties_halmos_cannot_decide(tmp_path):
    parsed = parse(tmp_path, "Locked.sol", ONLY_DELAYED)
    properties, _ = derive_properties(parsed[0], invariants(parsed))
    assert [prop.name for prop in properties] == ["property_nonce_monotonic"]
    issues = preflight.block_issues("halmos", halmos.TRAITS, parsed[0], properties, parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(FROZEN_BLOCK, UNSUPPORTED)]
    assert "block.number > unlockBlock" in issues[0].message
    assert "pins block.number to 1 and cannot decide it" in issues[0].message
    # the same property is reachable on a backend that advances blocks
    issues = preflight.block_issues("medusa", medusa.TRAITS, parsed[0], properties, parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(FROZEN_BLOCK, INFO)]


def test_frozen_block_is_read_through_internal_calls_and_excludes_the_action(tmp_path):
    parsed = parse(tmp_path, "Collateral.sol", DELAYED)
    properties, _ = derive_properties(parsed[0], invariants(parsed))
    coherent = next(prop for prop in properties if "coherent" in prop.name)
    issues = preflight.block_issues("halmos", halmos.TRAITS, parsed[0], [coherent], parsed)
    assert [(issue.kind, issue.severity) for issue in issues] == [(FROZEN_BLOCK, WARN)]
    assert issues[0].actions == ("Collateral.withdraw",)
    assert "block.number - since[who] < delay" in issues[0].message
    assert "decided only through Collateral.add" in issues[0].message
    result = run_backend("halmos", tmp_path, parsed, invariants(parsed), generate_only=True)[0]
    source = "\n".join(result.artifacts.values())
    assert "after_add" in source
    assert "coherent_after_withdraw" not in source


def test_halmos_reports_undecidable_properties_as_unsupported(tmp_path, monkeypatch):
    parsed = parse(tmp_path, "Locked.sol", ONLY_DELAYED)
    result = run_backend("halmos", tmp_path, parsed, invariants(parsed))[0]
    assert result.status == "UNSUPPORTED"
    assert "block or timestamp delta" in result.reason
    assert any("cannot decide" in item for item in result.unsupported)


def test_traits_declare_who_advances_the_block():
    declared = traits()
    assert set(declared) == set(BACKENDS)
    assert declared["halmos"].advances_block is False and declared["halmos"].kind == "symbolic"
    assert declared["medusa"].advances_block is True
    assert declared["medusa"].fuzzes_nested_deployments is False


# -- The real target, when it is on this machine ------------------------------------------

@pytest.mark.skipif(not REAL_TARGET.is_dir(), reason="reference target not present")
def test_real_target_reports_both_quotes_before_any_launch():
    from crystal.discovery import discover
    from crystal.parsers import parse_project

    contracts = parse_project(discover(REAL_TARGET)).contracts
    report = preflight_report(REAL_TARGET, contracts)
    quotes = next(item for item in report.homonyms if item.name == "Quotes")
    assert quotes.paths == ("src/legacy/Quotes.sol", "src/libraries/Quotes.sol")
    # Which copy the toolchain bound is read from the build cache — available
    # only when one exists, and NOT STABLE between builds. Observed on this
    # target: `src/legacy/Quotes.sol` on one build, `src/libraries/Quotes.sol`
    # on the next, with nothing changed but the build. That instability is the
    # whole reason the refusal exists, so the test pins that a winner is named
    # and refuses to pin which — pinning it would encode a coin flip.
    if quotes.linked:
        assert quotes.linked in quotes.paths
    refusals = [issue for issue in report.issues if issue.name == "Quotes" and issue.severity == REFUSE]
    assert {issue.contract for issue in refusals} >= {"PegInContract", "PegOutContract"}
    pegin = next(issue for issue in refusals if issue.contract == "PegInContract")
    assert "PegInContract imports src/libraries/Quotes.sol" in pegin.message
    # Naming the copy the toolchain bound needs a build cache to read; the
    # refusal itself does not, and the refusal is what the guarantee is.
    if quotes.linked:
        assert "not the src/legacy/Quotes.sol the artifact tree linked" in pegin.message
    assert any(item.name == "SignatureValidator" for item in report.homonyms)
