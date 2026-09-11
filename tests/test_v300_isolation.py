"""Build 017 — what an analysed target's tooling can reach.

Measured on this checkout, 2026-09-11: before the change, a child launched by
`process.run` saw the operator's complete environment. That is not a
theoretical exposure. Foundry hands a Solidity harness `vm.envUint("PRIVATE_KEY")`
and `vm.envString(...)`, and the harness is compiled from a contract Crystal
did not write — so a deploy key, an RPC URL carrying an API token or a cloud
credential was readable from inside the target's own code.

The tests that assert a secret does not escape run a real subprocess and look
at what it actually received. Inspecting the dictionary Crystal builds would
pass even if the dictionary were never passed to `subprocess.run`, which is
precisely the bug being fixed.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from crystal import process
from crystal.isolation import (
    ALWAYS,
    OPT_IN_VARIABLE,
    child_environment,
    isolation_report,
    withheld_names,
)
from crystal.isolation.environment import decide

SECRET = "PRIVATE_KEY"
SECRET_VALUE = "0xc0ffee_operator_deploy_key"

READ_ENV = (
    "import os,sys;"
    "sys.stdout.write('|'.join(f'{k}={os.environ.get(k, \"<absent>\")}' "
    "for k in sys.argv[1:]))"
)

VAULT = """
pragma solidity ^0.8.0;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    function deposit(uint256 a) external {
        totalAssets += a;
        totalSupply += a;
    }
}
"""


def child_sees(names, timeout=60, **kwargs):
    """What a real child process actually received, as a dict."""
    result = process.run(
        [sys.executable, "-c", READ_ENV, *names], timeout=timeout, **kwargs,
    )
    assert result.ok, result.error
    return dict(
        entry.split("=", 1) for entry in result.stdout.split("|") if "=" in entry
    )


# -- the promise: target tooling never receives a credential ------------------

@pytest.mark.invariant
def test_a_secret_in_the_parent_does_not_reach_a_launched_tool(monkeypatch):
    monkeypatch.setenv(SECRET, SECRET_VALUE)
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.example/KEYMATERIAL")
    monkeypatch.setenv("ETHERSCAN_API_KEY", "ABCDEF")

    seen = child_sees([SECRET, "ETH_RPC_URL", "ETHERSCAN_API_KEY"])
    assert seen == {
        SECRET: "<absent>",
        "ETH_RPC_URL": "<absent>",
        "ETHERSCAN_API_KEY": "<absent>",
    }


@pytest.mark.invariant
def test_the_policy_is_an_allowlist_not_a_list_of_known_secrets(monkeypatch):
    """A denylist is a list of the secrets somebody thought of.

    The one that leaks is the one nobody named, so an unremarkable variable
    with no secret-looking spelling must be withheld too.
    """
    monkeypatch.setenv("ZZ_UNFORESEEN_INTERNAL_THING", "value")
    assert "ZZ_UNFORESEEN_INTERNAL_THING" in withheld_names()
    assert "ZZ_UNFORESEEN_INTERNAL_THING" not in child_environment()


@pytest.mark.invariant
def test_the_report_states_that_the_process_is_not_sandboxed():
    """The honest line, guarded so it cannot be quietly dropped.

    Crystal confines the filesystem and the environment and does not isolate
    the process. Reporting the first two while omitting the third would read as
    a stronger guarantee than Crystal offers, which is the one thing the
    evidence discipline forbids.
    """
    report = isolation_report()
    assert report["process"]["policy"] == "not sandboxed"
    assert "does not claim" in report["process"]["note"]


def test_the_report_names_what_is_withheld_and_never_its_value(monkeypatch):
    monkeypatch.setenv(SECRET, SECRET_VALUE)
    rendered = repr(isolation_report())
    assert SECRET_VALUE not in rendered
    assert SECRET not in rendered          # not even in `passed`
    assert isolation_report()["environment"]["withheld_count"] >= 1


# -- and the tools still run --------------------------------------------------

def test_the_child_keeps_what_it_needs_to_start():
    seen = child_sees(["PATH"])
    assert seen["PATH"] not in ("", "<absent>")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows loader requirement")
def test_windows_keeps_systemroot():
    """Without `SystemRoot` a Windows child fails before its first instruction."""
    assert "SystemRoot" in ALWAYS
    seen = child_sees(["SystemRoot"])
    assert seen["SystemRoot"] != "<absent>"


def test_a_real_toolchain_probe_still_resolves():
    """git needs PATH, HOME and the platform essentials — nothing withheld."""
    result = process.run(["git", "--version"], timeout=30)
    assert result.ok and result.returncode == 0
    assert "git" in result.stdout.lower()


# -- the operator's escape hatches -------------------------------------------

def test_an_operator_can_opt_a_name_back_in(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.example/KEYMATERIAL")
    monkeypatch.setenv(OPT_IN_VARIABLE, "ETH_RPC_URL")
    seen = child_sees(["ETH_RPC_URL"])
    assert seen["ETH_RPC_URL"] == "https://rpc.example/KEYMATERIAL"


def test_an_opt_in_is_recorded_so_the_decision_stays_visible(monkeypatch):
    monkeypatch.setenv("ETH_RPC_URL", "https://rpc.example/KEYMATERIAL")
    monkeypatch.setenv(OPT_IN_VARIABLE, "ETH_RPC_URL")
    assert isolation_report()["environment"]["opted_in"] == ["ETH_RPC_URL"]


def test_inherit_environment_is_available_for_crystals_own_assets(monkeypatch):
    """`pip install -e .` runs on the operator's checkout, not on a target."""
    monkeypatch.setenv(SECRET, SECRET_VALUE)
    seen = child_sees([SECRET], inherit_environment=True)
    assert seen[SECRET] == SECRET_VALUE


def test_extra_values_crystal_sets_itself_reach_the_child():
    seen = child_sees(["CRYSTAL_TEST_MARKER"],
                      env_extra={"CRYSTAL_TEST_MARKER": "set-by-crystal"})
    assert seen["CRYSTAL_TEST_MARKER"] == "set-by-crystal"


# -- operator code is executed only when named on the command line ------------

@pytest.mark.invariant
def test_pack_discovery_never_reads_the_analysed_target(tmp_path):
    """A pack is Python. Discovering one inside a scanned repository would be
    arbitrary code execution from an untrusted target."""
    from crystal.campaigns import discover_packs

    hostile = tmp_path / "evil_pack.py"
    hostile.write_text(
        "raise AssertionError('a pack inside the target was executed')",
        encoding="utf-8",
    )
    registry = discover_packs()          # the call the engine and CLI make
    assert registry.list_campaigns()
    assert set(registry.list_packs()) <= {
        "generic", "defi", "registry", "authorization", "migration", "economic",
    }
    assert not registry.operator_campaign_ids
    assert hostile.exists()              # present, and never read


def test_a_pack_named_on_the_command_line_still_loads(tmp_path):
    from crystal.campaigns import discover_packs

    pack = tmp_path / "operator_pack.py"
    pack.write_text("CAMPAIGNS = []\n", encoding="utf-8")
    registry = discover_packs(packs=(str(pack),))
    assert registry.load_report[str(pack)] == 0


# -- generated tool configuration ---------------------------------------------

def test_the_generated_halmos_config_disables_ffi(tmp_path):
    """`vm.ffi` hands the host to a harness built from the target's code.

    Asserted on the artifact Crystal actually writes, not on the source that
    writes it: a constant can be correct while the code path that emits it is
    not the one that runs.
    """
    from crystal.backends import halmos
    from crystal.engine import research

    (tmp_path / "Vault.sol").write_text(VAULT, encoding="utf-8")
    parsed = research(tmp_path, use_solc=False, use_foundry=False)
    generated = halmos.generate(
        tmp_path, parsed["contracts"], parsed["protocol_invariants"],
    )
    configs = [
        content
        for execution in generated
        for name, content in (execution.artifacts or {}).items()
        if name == "foundry.toml"
    ]
    assert configs, "halmos generated no foundry.toml to check"
    for config in configs:
        assert "ffi = false" in config


def test_the_research_foundry_harness_disables_ffi():
    from crystal.research import foundry

    assert "ffi = false" in Path(foundry.__file__).read_text(encoding="utf-8")


# -- the decision object ------------------------------------------------------

def test_decide_partitions_the_environment_exactly():
    source = {"PATH": "/bin", "PRIVATE_KEY": "x", "GOCACHE": "/c"}
    decision = decide(source)
    assert set(decision.passed) | set(decision.withheld) == set(source)
    assert not set(decision.passed) & set(decision.withheld)
    assert "PRIVATE_KEY" in decision.withheld
    assert "GOCACHE" in decision.passed
