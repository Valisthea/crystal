"""Build 015 — no verdict without a witness.

Measured on 2026-09-05: 35 tests green over 1,049,043 calls with zero
successful deposits. These tests pin the discipline that makes such a green
impossible to report as HELD: the verdict model refuses HELD without a witness
that shows a state-mutating transition succeeded, and each backend's parser
decodes the tool's own output into that witness rather than into a boolean.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from crystal.backends import echidna, halmos, medusa, run_backend
from crystal.backends.actions import (
    block_dependence,
    fuzzable_parameters,
    mutating_actions,
    property_subject,
    transitive_writes,
)
from crystal.backends.base import (
    EXECUTED_FAIL,
    EXECUTED_PASS,
    EXECUTED_VACUOUS,
    TOOL_ERROR,
    Property,
    derive_properties,
    execution_status,
)
from crystal.backends.harness import WITNESS_LINE, fuzz_handlers
from crystal.backends.selectors import (
    SelectorTable,
    canonical_signature,
    keccak256,
    selector,
    selector_table,
)
from crystal.backends.verdict import (
    HELD,
    UNSUPPORTED,
    VACUOUS,
    VIOLATED,
    ActionOutcome,
    ExecutionWitness,
    PropertyVerdict,
    WitnessRequired,
    decide,
    summarise,
)
from crystal.parsers import solidity_regex
from crystal.protocol.invariants import derive_protocol_invariants
from crystal.protocol.model import build_protocol_model

VAULT = """
pragma solidity ^0.8.20;
contract Vault {
    uint256 public totalAssets;
    uint256 public totalSupply;
    uint256 public nonce;
    mapping(address => uint256) public balances;
    error InsufficientFunds(uint256 available, uint256 requested);

    function deposit(uint256 assets, address to) external payable returns (uint256) {
        if (msg.value < assets) revert InsufficientFunds(msg.value, assets);
        _credit(assets, to);
        return assets;
    }
    function _credit(uint256 assets, address to) internal {
        totalAssets += assets;
        totalSupply += assets;
        nonce += 1;
        balances[to] += assets;
    }
    function withdraw(uint256 assets) external {
        require(balances[msg.sender] >= assets, "insufficient balance");
        totalAssets -= assets; totalSupply -= assets; balances[msg.sender] -= assets;
    }
    function poke() external { }
}
"""


def parse(source=VAULT, name="Vault.sol", tmp_path=None):
    if tmp_path is not None:
        path = tmp_path / name
        path.write_text(source, encoding="utf-8")
        return solidity_regex.parse_text(source, str(path))
    return solidity_regex.parse_text(source, name)


def invariants(parsed):
    return derive_protocol_invariants(build_protocol_model(parsed))


def vault_properties(parsed):
    properties, _ = derive_properties(parsed[0], invariants(parsed))
    return properties


def witness(actions=(), checked=True, subject=("totalAssets", "totalSupply"), calls=None, **extra):
    return ExecutionWitness(
        "medusa", "test", tuple(subject), checked=checked, calls=calls,
        actions=tuple(actions), **extra,
    )


# -- HELD is structurally unreachable without a witness -----------------------------

@pytest.mark.invariant
def test_held_cannot_be_constructed_without_a_witness():
    with pytest.raises(WitnessRequired):
        PropertyVerdict("property_x", HELD, "trust me")


@pytest.mark.invariant
def test_held_cannot_be_constructed_with_a_witness_that_shows_no_mutation():
    never = ActionOutcome("Vault.deposit", 1000, 0, 1000, (("InsufficientFunds", 1000),), True)
    with pytest.raises(WitnessRequired, match="no state-mutating transition succeeded"):
        PropertyVerdict("property_x", HELD, "green", witness([never]))


@pytest.mark.invariant
def test_held_cannot_be_smuggled_in_through_replace():
    never = ActionOutcome("Vault.deposit", 1000, 0, 1000, (("InsufficientFunds", 1000),), True)
    vacuous = decide("property_x", witness([never]))
    assert vacuous.verdict == VACUOUS
    with pytest.raises(WitnessRequired):
        dataclasses.replace(vacuous, verdict=HELD)


@pytest.mark.invariant
def test_held_requires_the_tool_to_have_evaluated_the_property():
    good = ActionOutcome("Vault.deposit", 10, 3, 7, (), True)
    with pytest.raises(WitnessRequired, match="never reported evaluating"):
        PropertyVerdict("property_x", HELD, "green", witness([good], checked=False))


@pytest.mark.invariant
def test_held_requires_a_subject():
    good = ActionOutcome("Vault.deposit", 10, 3, 7, (), True)
    with pytest.raises(WitnessRequired, match="not tied to any state variable"):
        PropertyVerdict("property_x", HELD, "green", witness([good], subject=()))


@pytest.mark.invariant
def test_held_is_reachable_only_through_a_substantiating_witness():
    good = ActionOutcome("Vault.deposit", 10, 3, 7, (("InsufficientFunds", 7),), True)
    verdict = decide("property_x", witness([good], calls=10))
    assert verdict.verdict == HELD
    assert verdict.witness is not None and verdict.witness.sufficient
    assert "Vault.deposit 3/10 succeeded" in verdict.reason
    assert verdict.witness.actions[0].success_ratio == pytest.approx(0.3)


def test_unknown_verdicts_and_bare_violations_are_rejected():
    with pytest.raises(ValueError):
        PropertyVerdict("p", "GREEN", "no such verdict")
    with pytest.raises(ValueError, match="counterexample"):
        PropertyVerdict("p", VIOLATED, "failed")
    with pytest.raises(ValueError, match="reason"):
        PropertyVerdict("p", VACUOUS, "")


# -- VACUOUS names what never succeeded ---------------------------------------------

def test_vacuous_reason_names_the_action_that_never_succeeded():
    never = ActionOutcome(
        "Vault.deposit", 1049043, 0, 1049043, (("InsufficientFunds", 1049043),), True,
    )
    verdict = decide("property_totalAssets_totalSupply_coherent", witness([never]))
    assert verdict.verdict == VACUOUS
    assert "Vault.deposit 0/1049043 succeeded" in verdict.reason
    assert "InsufficientFunds x1049043" in verdict.reason


def test_vacuous_when_nothing_ran_at_all():
    verdict = decide("property_x", witness([], calls=0))
    assert verdict.verdict == VACUOUS
    assert "no per-action execution metrics" in verdict.reason
    assert decide("property_x", None).verdict == VACUOUS


def test_vacuous_when_only_unrelated_actions_succeeded():
    unrelated = ActionOutcome("Vault.poke", 500, 500, 0, (), mutates_subject=False)
    verdict = decide("property_x", witness([unrelated]))
    assert verdict.verdict == VACUOUS
    assert "no executed entry point writes totalAssets, totalSupply" in verdict.reason
    assert "Vault.poke" in verdict.reason


def test_a_counterexample_is_conclusive_and_unsupported_is_not_judged():
    never = ActionOutcome("Vault.deposit", 10, 0, 10, (), True)
    violated = decide("property_x", witness([never]), counterexamples=("h_deposit(1, 0xA11CE)",))
    assert violated.verdict == VIOLATED and violated.counterexamples
    unsupported = decide("property_x", witness([never]), unsupported_reason="block delta")
    assert unsupported.verdict == UNSUPPORTED and unsupported.reason == "block delta"


def test_summary_and_status_follow_the_verdicts_not_the_exit_code():
    good = ActionOutcome("Vault.deposit", 10, 3, 7, (), True)
    never = ActionOutcome("Vault.deposit", 10, 0, 10, (), True)
    held = decide("a", witness([good]))
    vacuous = decide("b", witness([never]))
    text = summarise([held, vacuous])
    assert text.startswith("HELD 1, VACUOUS 1")
    assert "b: no state-mutating transition succeeded" in text
    assert execution_status([held, vacuous], evaluated=True) == EXECUTED_PASS
    assert execution_status([vacuous], evaluated=True) == EXECUTED_VACUOUS
    assert execution_status([vacuous], evaluated=False) == TOOL_ERROR
    violated = decide("c", witness([good]), counterexamples=("trace",))
    assert execution_status([held, violated], evaluated=True) == EXECUTED_FAIL


def test_witness_serialises_with_its_gap():
    never = ActionOutcome("Vault.deposit", 10, 0, 10, (), True)
    data = decide("p", witness([never])).to_dict()
    assert data["verdict"] == VACUOUS
    assert data["witness"]["gap"].startswith("no state-mutating transition succeeded")
    json.dumps(data)


# -- Subject and mutating actions follow internal calls ------------------------------

def test_property_subject_is_read_from_the_expression():
    parsed = parse()
    properties = vault_properties(parsed)
    coherent = next(p for p in properties if "coherent" in p.name)
    monotonic = next(p for p in properties if "monotonic" in p.name)
    assert property_subject(coherent, parsed[0]) == ("totalAssets", "totalSupply")
    assert property_subject(monotonic, parsed[0]) == ("nonce",)


def test_property_subject_accepts_the_compiler_shape():
    class Compiled:
        backend = "medusa"
        filename = "property_nonce_monotonic.sol"
        source = "function property_nonce_monotonic() public view returns (bool) { return target.nonce() >= 0; }"
        unsupported_reason = ""

    parsed = parse()
    assert property_subject(Compiled(), parsed[0]) == ("nonce",)
    assert property_subject(Property("p", "target.nothing()", "", "kind"), parsed[0]) == ()


def test_mutating_actions_follow_internal_calls():
    parsed = parse()
    contract = parsed[0]
    deposit = next(f for f in contract.functions if f.name == "deposit")
    assert "nonce" in transitive_writes(contract, deposit, parsed)
    names = {f.name for f in mutating_actions(contract, ("nonce",), parsed)}
    assert names == {"deposit"}
    names = {f.name for f in mutating_actions(contract, ("totalAssets",), parsed)}
    assert names == {"deposit", "withdraw"}


def test_block_dependence_is_read_through_callees():
    source = """
    pragma solidity ^0.8.20;
    contract Delayed {
        uint256 public total;
        uint256 public since;
        uint256 public delay;
        function resign() external { since = block.number; }
        function withdraw() external { _withdraw(); }
        function _withdraw() internal {
            require(block.number - since >= delay, "too early");
            total -= 1;
        }
        function add() external { total += 1; }
    }
    """
    parsed = parse(source, "Delayed.sol")
    contract = parsed[0]
    withdraw = next(f for f in contract.functions if f.name == "withdraw")
    add = next(f for f in contract.functions if f.name == "add")
    clauses = block_dependence(contract, withdraw, parsed)
    assert clauses and "block.number - since" in clauses[0]
    assert block_dependence(contract, add, parsed) == ()


def test_fuzzable_parameters_refuse_structs():
    parsed = parse()
    deposit = next(f for f in parsed[0].functions if f.name == "deposit")
    assert fuzzable_parameters(deposit) == [("uint256", "assets"), ("address", "to")]

    class Fake:
        params = [type("P", (), {"type_name": "Quote memory", "name": "q"})()]

    assert fuzzable_parameters(Fake()) is None


# -- Harness handlers give the fuzzer something to move ------------------------------

def test_fuzz_harness_forwards_entry_points_and_updates_ghosts(tmp_path):
    parsed = parse(tmp_path=tmp_path)
    properties = vault_properties(parsed)
    plan = fuzz_handlers(parsed[0], properties, parsed)
    assert set(plan.names) == {"h_deposit", "h_withdraw", "h_poke"}
    assert plan.action("h_deposit") == "Vault.deposit"
    source = medusa.harness(parsed[0], properties, plan, parsed)
    assert "function h_deposit(uint256 p_assets, address p_to) public payable" in source
    assert "target.deposit{value: msg.value}(p_assets, p_to);" in source
    assert WITNESS_LINE in source
    assert "_ghost_nonce = _crystalMax(_ghost_nonce, target.nonce());" in source
    assert "function property_nonce_monotonic() public view returns (bool)" in source


def test_medusa_config_compiles_the_harness_file_and_reports_reverts():
    data = medusa.config("CrystalMedusaHarness")
    assert data["compilation"]["platformConfig"]["target"] == "CrystalMedusaHarness.sol"
    assert data["fuzzing"]["revertReporterEnabled"] is True
    assert data["fuzzing"]["senderAddresses"]
    assert "deploymentOrder" not in data["fuzzing"]


# -- Medusa output decodes into a witness (formats measured on 1.5.1) ------------------

MEDUSA_STDOUT = """
\x1b[1m[NOT STARTED] Property Test: CrystalMedusaHarness.property_nonce_monotonic()\x1b[0m
fuzz: elapsed:       3s, calls:      23163 (  7720/sec), seq/s:     76, branches:    193, corpus:     4, failures: 0/231, gas/s:    303449056
Fuzzer stopped, test results follow below ...
[PASSED] Property Test: CrystalMedusaHarness.property_nonce_monotonic()
[FAILED] Property Test: CrystalMedusaHarness.property_totalAssets_totalSupply_coherent()
Test for method "CrystalMedusaHarness.property_totalAssets_totalSupply_coherent()" failed after the following call sequence:
1) CrystalMedusaHarness.h_deposit(1, 0xA11CE) (block=2, time=3, gas=12500000, gasprice=1, value=1, sender=0x10000)
Test summary: 1 test(s) passed, 1 test(s) failed
"""


def revert_report(deposit_successes: int, deposit_calls: int = 11575,
                  withdraw_successes: int = 1817, withdraw_calls: int = 11592) -> str:
    """Shaped exactly like Medusa 1.5.1's `corpus/coverage/revert_report.json`."""
    return json.dumps({"contractRevertMetrics": {"CrystalMedusaHarness": {
        "name": "CrystalMedusaHarness",
        "functionRevertMetrics": {
            "": {"name": "", "totalCalls": 1400, "totalReverts": 0, "revertReasonMetrics": {}},
            "h_deposit": {
                "name": "h_deposit", "totalCalls": deposit_calls,
                "totalReverts": deposit_calls - deposit_successes,
                "revertReasonMetrics": {
                    "InsufficientFunds": {"reason": "InsufficientFunds", "count": deposit_calls - deposit_successes},
                },
            },
            "h_withdraw": {
                "name": "h_withdraw", "totalCalls": withdraw_calls,
                "totalReverts": withdraw_calls - withdraw_successes,
                "revertReasonMetrics": {
                    "0x08c379a0" + "0" * 62 + "20" + "0" * 62 + "14" + b"insufficient balance".hex().ljust(64, "0"):
                        {"count": withdraw_calls - withdraw_successes},
                },
            },
            "h_poke": {"name": "h_poke", "totalCalls": 5, "totalReverts": 0, "revertReasonMetrics": {}},
        },
    }}})


def test_medusa_parses_statuses_calls_and_counterexamples():
    statuses, traces = medusa.parse_properties(medusa.ANSI_RE.sub("", MEDUSA_STDOUT))
    assert statuses == {
        "property_nonce_monotonic": "PASSED",
        "property_totalAssets_totalSupply_coherent": "FAILED",
    }
    assert traces["property_totalAssets_totalSupply_coherent"][-1].startswith("1) CrystalMedusaHarness.h_deposit")
    assert medusa.parse_calls(MEDUSA_STDOUT) == 23163


def test_medusa_revert_report_becomes_per_action_outcomes():
    parsed = parse()
    plan = fuzz_handlers(parsed[0], vault_properties(parsed), parsed)
    table = selector_table(parsed)
    outcomes = {o.action: o for o in medusa.action_outcomes(revert_report(3471), plan, table)}
    deposit = outcomes["Vault.deposit"]
    assert (deposit.calls, deposit.successes, deposit.reverts) == (11575, 3471, 8104)
    assert deposit.revert_reasons == (("InsufficientFunds", 8104),)
    assert outcomes["Vault.withdraw"].revert_reasons == (('Error("insufficient balance")', 9775),)
    assert outcomes["Vault.poke"].successes == 5


def test_medusa_zero_successful_deposits_is_vacuous_not_held(tmp_path, monkeypatch):
    """The measured failure: green over a million calls, no deposit ever
    succeeded — and with nothing deposited, no withdrawal could either."""
    parsed = parse(tmp_path=tmp_path)
    passed = "\n".join(
        line for line in MEDUSA_STDOUT.splitlines() if "FAILED" not in line
    ).replace("[NOT STARTED]", "[PASSED]") + "\n[PASSED] Property Test: CrystalMedusaHarness.property_totalAssets_totalSupply_coherent()\n"
    report = revert_report(0, 1049043, withdraw_successes=0)
    results = _run_medusa_with(monkeypatch, tmp_path, parsed, passed, report)
    assert len(results) == 1
    result = results[0]
    assert result.status == EXECUTED_VACUOUS
    assert not result.reproducible
    verdicts = {v.property: v for v in result.verdicts}
    coherent = verdicts["property_totalAssets_totalSupply_coherent"]
    assert coherent.verdict == VACUOUS
    assert "Vault.deposit 0/1049043 succeeded" in coherent.reason
    assert "InsufficientFunds x1049043" in coherent.reason
    assert "Vault.withdraw 0/11592 succeeded" in coherent.reason
    # poke succeeded 5 times but writes nothing the property reads: not a witness.
    assert "Vault.poke" not in coherent.reason
    monotonic = verdicts["property_nonce_monotonic"]
    assert monotonic.verdict == VACUOUS
    assert "Vault.deposit 0/1049043" in monotonic.reason
    assert "Vault.withdraw" not in monotonic.reason


def test_medusa_a_success_on_an_unrelated_write_is_not_a_witness(tmp_path, monkeypatch):
    """withdraw succeeding witnesses totalAssets, never nonce."""
    parsed = parse(tmp_path=tmp_path)
    passed = "\n".join(
        line for line in MEDUSA_STDOUT.splitlines() if "FAILED" not in line
    ).replace("[NOT STARTED]", "[PASSED]") + "\n[PASSED] Property Test: CrystalMedusaHarness.property_totalAssets_totalSupply_coherent()\n"
    result = _run_medusa_with(monkeypatch, tmp_path, parsed, passed, revert_report(0))[0]
    verdicts = {v.property: v for v in result.verdicts}
    assert verdicts["property_totalAssets_totalSupply_coherent"].verdict == HELD
    assert verdicts["property_nonce_monotonic"].verdict == VACUOUS
    assert result.status == EXECUTED_PASS


def test_medusa_held_only_with_successful_mutations(tmp_path, monkeypatch):
    parsed = parse(tmp_path=tmp_path)
    passed = "\n".join(
        line for line in MEDUSA_STDOUT.splitlines() if "FAILED" not in line
    ).replace("[NOT STARTED]", "[PASSED]") + "\n[PASSED] Property Test: CrystalMedusaHarness.property_totalAssets_totalSupply_coherent()\n"
    result = _run_medusa_with(monkeypatch, tmp_path, parsed, passed, revert_report(3471))[0]
    assert result.status == EXECUTED_PASS
    assert result.reproducible
    for verdict in result.verdicts:
        assert verdict.verdict == HELD
        assert verdict.witness.calls == 23163
        assert verdict.witness.sequences is None
        assert "InsufficientFunds" in verdict.witness.revert_selectors
    assert "medusa.revert_report.json" in result.artifacts


def test_medusa_counterexample_is_violated_and_tool_crash_is_not_a_pass(tmp_path, monkeypatch):
    parsed = parse(tmp_path=tmp_path)
    result = _run_medusa_with(monkeypatch, tmp_path, parsed, MEDUSA_STDOUT, revert_report(3471))[0]
    assert result.status == EXECUTED_FAIL
    assert result.verdict_of("property_totalAssets_totalSupply_coherent").verdict == VIOLATED
    assert result.counterexamples
    crashed = _run_medusa_with(
        monkeypatch, tmp_path, parsed,
        "error Failed to start fuzzer\nno assertion, property, optimization, or custom tests were found to fuzz",
        "", returncode=6,
    )[0]
    assert crashed.status == TOOL_ERROR
    assert "exited 6" in crashed.reason
    assert all(v.verdict == VACUOUS for v in crashed.verdicts)


def _run_medusa_with(monkeypatch, tmp_path, parsed, stdout, report_text, returncode=0):
    from crystal import process
    from crystal.backends.base import BackendCapabilities

    monkeypatch.setattr(medusa, "detect", lambda: BackendCapabilities("medusa", True, "1.5.1", "ok"))

    def fake_run(command, cwd=None, timeout=120, stdin=None):
        if report_text:
            target = cwd / medusa.REVERT_REPORT
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report_text, encoding="utf-8")
        return process.ProcessResult(returncode, stdout, "", True)

    monkeypatch.setattr(process, "run", fake_run)
    return run_backend("medusa", tmp_path, parsed, invariants(parsed))


# -- Halmos: a PASS whose paths were not all reverted, block-gated tests excluded --------

HALMOS_OUTPUT = """
Running 4 tests for test/CrystalHalmosTest.t.sol:CrystalHalmosTest
WARNING  check_property_nonce_monotonic_after_withdraw(uint256): all paths have been reverted; the setup state
         or inputs may have been too restrictive.
         (see https://github.com/a16z/halmos/wiki/warnings#revert-all)
[ERROR] check_property_nonce_monotonic_after_withdraw(uint256) (paths: 1, time: 0.06s, bounds: [])
[PASS] check_property_nonce_monotonic_after_deposit(uint256,address,uint256) (paths: 3, time: 0.07s, bounds: [])
Counterexample:
    p_assets_uint256 = 0x01
    p_to_address = 0x00
[FAIL] check_property_totalAssets_totalSupply_coherent_after_deposit(uint256,address,uint256) (paths: 3, time: 0.09s, bounds: [])
[PASS] check_property_totalAssets_totalSupply_coherent_after_withdraw(uint256) (paths: 1, time: 0.02s, bounds: [])
Symbolic test result: 2 passed; 1 failed; time: 0.35s
"""


def test_halmos_parses_pass_fail_error_and_all_reverted():
    outcomes, traces = halmos.parse_results(HALMOS_OUTPUT)
    assert outcomes["check_property_nonce_monotonic_after_withdraw"] == ("ERROR", 1, True)
    assert outcomes["check_property_nonce_monotonic_after_deposit"] == ("PASS", 3, False)
    assert outcomes["check_property_totalAssets_totalSupply_coherent_after_deposit"][0] == "FAIL"
    assert traces["check_property_totalAssets_totalSupply_coherent_after_deposit"][1] == "p_assets_uint256 = 0x01"


def test_halmos_harness_snapshots_monotonicity_and_deals_value(tmp_path):
    parsed = parse(tmp_path=tmp_path)
    source, tests = halmos.harness(parsed[0], vault_properties(parsed), {}, parsed)
    assert "_ghost_nonce" not in source
    assert "uint256 before = target.nonce();" in source
    assert "assert(target.nonce() >= before);" in source
    assert "vm.deal(address(this), crystalValue);" in source
    assert "target.deposit{value: crystalValue}(assets, to);" in source
    assert 'keccak256("hevm cheat code")' in source
    assert all(owner in {"property_nonce_monotonic", "property_totalAssets_totalSupply_coherent"}
               for owner, _ in tests.values())


def test_halmos_verdicts_from_measured_output(tmp_path, monkeypatch):
    from crystal import process
    from crystal.backends.base import BackendCapabilities

    parsed = parse(tmp_path=tmp_path)
    monkeypatch.setattr(halmos, "detect", lambda: BackendCapabilities("halmos", True, "0.3", "ok"))
    monkeypatch.setattr(
        process, "run",
        lambda command, cwd=None, timeout=120, stdin=None: process.ProcessResult(1, HALMOS_OUTPUT, "", True),
    )
    result = run_backend("halmos", tmp_path, parsed, invariants(parsed))[0]
    assert result.status == EXECUTED_FAIL
    verdicts = {v.property: v for v in result.verdicts}
    assert verdicts["property_totalAssets_totalSupply_coherent"].verdict == VIOLATED
    monotonic = verdicts["property_nonce_monotonic"]
    assert monotonic.verdict == HELD
    deposit = next(a for a in monotonic.witness.actions if a.action == "Vault.deposit")
    withdraw = next(a for a in monotonic.witness.actions if a.action == "Vault.withdraw")
    assert deposit.executed_mutation is True and deposit.mutates_subject
    assert withdraw.executed_mutation is False and withdraw.revert_reasons == (("all paths reverted", 1),)


def test_halmos_all_paths_reverted_is_vacuous(tmp_path, monkeypatch):
    from crystal import process
    from crystal.backends.base import BackendCapabilities

    parsed = parse(tmp_path=tmp_path)
    everything_reverted = "\n".join(
        line.replace("[PASS]", "[ERROR]").replace("[FAIL]", "[ERROR]")
        for line in HALMOS_OUTPUT.splitlines() if "Counterexample" not in line and "p_" not in line
    )
    monkeypatch.setattr(halmos, "detect", lambda: BackendCapabilities("halmos", True, "0.3", "ok"))
    monkeypatch.setattr(
        process, "run",
        lambda command, cwd=None, timeout=120, stdin=None: process.ProcessResult(1, everything_reverted, "", True),
    )
    result = run_backend("halmos", tmp_path, parsed, invariants(parsed))[0]
    assert result.status == EXECUTED_VACUOUS
    assert all(v.verdict == VACUOUS for v in result.verdicts)


# -- Echidna: coverage-shaped witness, documented formats -------------------------------

ECHIDNA_STDOUT = """
Analyzing contract: /tmp/x/CrystalEchidnaHarness.sol:CrystalEchidnaHarness
property_nonce_monotonic: passing
property_totalAssets_totalSupply_coherent: failed!💥
  Call sequence:
    CrystalEchidnaHarness.h_deposit(1, 0xa11ce) Value: 0x1

[status] tests: 1/2, fuzzing: 4321/50000, values: [], cov: 210, corpus: 6
Unique instructions: 210
"""

ECHIDNA_COVERAGE = """
/tmp/x/CrystalEchidnaHarness.sol
    |  contract CrystalEchidnaHarness {
  * |      function h_deposit(uint256 p_assets, address p_to) public payable {
  *r|          target.deposit{value: msg.value}(p_assets, p_to);
    |          _crystalWitness += 1; // reached only if the forwarded call succeeded
    |      }
  * |      function h_withdraw(uint256 p_assets) public {
  * |          target.withdraw(p_assets);
  * |          _crystalWitness += 1; // reached only if the forwarded call succeeded
    |      }
  * |      function h_poke() public {
  * |          target.poke();
  * |          _crystalWitness += 1; // reached only if the forwarded call succeeded
    |      }
"""


def test_echidna_parses_statuses_calls_and_coverage_witness_lines():
    statuses, traces = echidna.parse_properties(ECHIDNA_STDOUT)
    assert statuses == {
        "property_nonce_monotonic": "passing",
        "property_totalAssets_totalSupply_coherent": "failed",
    }
    assert any("h_deposit" in line for line in traces["property_totalAssets_totalSupply_coherent"])
    assert echidna.parse_calls(ECHIDNA_STDOUT) == 4321
    parsed = parse()
    plan = fuzz_handlers(parsed[0], vault_properties(parsed), parsed)
    outcomes = {o.action: o for o in echidna.coverage_outcomes(ECHIDNA_COVERAGE, plan)}
    assert outcomes["Vault.deposit"].executed_mutation is False
    assert outcomes["Vault.withdraw"].executed_mutation is True
    assert outcomes["Vault.poke"].executed_mutation is True


def test_echidna_green_without_a_mutating_success_is_vacuous(tmp_path, monkeypatch):
    from crystal import process
    from crystal.backends.base import BackendCapabilities

    parsed = parse(tmp_path=tmp_path)
    monkeypatch.setattr(echidna, "detect", lambda: BackendCapabilities("echidna", True, "2.2", "ok"))
    green = ECHIDNA_STDOUT.replace("failed!💥", "passing")

    def fake_run(command, cwd=None, timeout=120, stdin=None):
        corpus = cwd / "corpus"
        corpus.mkdir(exist_ok=True)
        (corpus / "covered.1700000000.txt").write_text(ECHIDNA_COVERAGE, encoding="utf-8")
        return process.ProcessResult(0, green, "", True)

    monkeypatch.setattr(process, "run", fake_run)
    result = run_backend("echidna", tmp_path, parsed, invariants(parsed))[0]
    verdicts = {v.property: v for v in result.verdicts}
    # nonce is only written by deposit, whose witness line never executed
    assert verdicts["property_nonce_monotonic"].verdict == VACUOUS
    assert "Vault.deposit never executed without reverting" in verdicts["property_nonce_monotonic"].reason
    # totalAssets is also written by withdraw, whose witness line did execute
    assert verdicts["property_totalAssets_totalSupply_coherent"].verdict == HELD
    assert result.status == EXECUTED_PASS


# -- Selectors: the reasons are decoded, never guessed -----------------------------------

def test_keccak_matches_published_vectors():
    assert keccak256(b"").hex() == "c5d2460186f7233c927e7db2dcc703c0e500b653ca82273b7bfad8045d85a470"
    assert keccak256(b"abc").hex() == "4e03657aea45a94fc7d47ba826c8d667c0d1e6e33a64a036ec44f58fa12d6c45"
    assert selector("transfer(address,uint256)") == "0xa9059cbb"
    assert selector("Error(string)") == "0x08c379a0"
    assert selector("Panic(uint256)") == "0x4e487b71"


def test_selector_table_is_built_from_declared_errors_only(tmp_path):
    parsed = parse(tmp_path=tmp_path)
    table = selector_table(parsed)
    custom = selector("InsufficientFunds(uint256,uint256)")
    assert table.signature(custom) == "InsufficientFunds(uint256,uint256)"
    assert table.decode(custom + "00" * 64).startswith("InsufficientFunds(uint256,uint256)")
    assert table.decode("0xdeadbeef") == "0xdeadbeef (undecoded selector)"
    assert table.decode("EnforcedPause") == "EnforcedPause"
    assert canonical_signature("Bad", ["Quote"]) is None
    assert canonical_signature("Ok", ["uint", "address payable", "bytes32[]"]) == "Ok(uint256,address,bytes32[])"
    panic = SelectorTable().decode("0x4e487b71" + "0" * 62 + "11")
    assert "overflow" in panic


# -- The generate-only path still produces artifacts, now with handlers ----------------

@pytest.mark.parametrize("backend", ["medusa", "echidna", "halmos"])
def test_generated_artifacts_carry_transitions(backend, tmp_path):
    parsed = parse(tmp_path=tmp_path)
    results = run_backend(backend, tmp_path, parsed, invariants(parsed), generate_only=True)
    body = "\n".join(results[0].artifacts.values())
    assert "target.deposit" in body
    assert results[0].status == "GENERATED"
    assert isinstance(results[0].preflight, tuple)
