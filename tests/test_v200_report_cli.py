import json
import subprocess
import sys

import pytest

from crystal import __build__, __version__
from crystal.cli import environment_report, main
from crystal.engine import research
from crystal.report import arcadia, markdown, payload, sarif, write_sarif

SOURCE = """
pragma solidity ^0.8.20;
contract Vault {
    mapping(address => uint256) public balances;
    uint256 public totalAssets;
    uint256 public totalSupply;
    address public owner;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
        totalAssets += msg.value;
        totalSupply += msg.value;
    }
    function withdraw(uint256 amount) external {
        require(balances[msg.sender] >= amount);
        (bool ok,) = msg.sender.call{value: amount}("");
        require(ok);
        balances[msg.sender] -= amount;
        totalAssets -= amount;
        totalSupply -= amount;
    }
    function setOwner(address x) external { owner = x; }
}
"""

# The v1 JSON contract. Removing any of these keys is a breaking change.
V1_TOP_LEVEL = {
    "tool", "version", "status", "summary", "finding_gate", "evidence_records",
    "output_contract", "constraints", "concrete_validation",
    "foundry_capabilities", "foundry_validation", "novel_behaviors",
    "unknown_behavior_candidates", "state_deltas", "delta_anomalies",
    "composition_candidates",
}
V1_SUMMARY = {
    "sources", "contracts", "research_candidates", "behavior_relations",
    "differential_candidates", "mutations", "impact_paths", "state_deltas",
    "delta_anomalies", "composition_candidates", "novel_behaviors",
    "unknown_behavior_candidates", "constraints", "concrete_hypotheses",
    "foundry_available", "foundry_hypotheses", "foundry_executed",
    "counterexample_hypotheses", "confirmed_findings", "output_role",
    "protocol_invariants", "sequence_hypotheses", "causal_edges",
    "solc_available", "token_functions", "value_flows", "accounting_relations",
    "oracle_signals", "fee_signals",
}


@pytest.fixture(scope="module")
def data(tmp_path_factory):
    target = tmp_path_factory.mktemp("report")
    (target / "Vault.sol").write_text(SOURCE, encoding="utf-8")
    return payload(research(target, use_solc=False, use_foundry=False)), target


def test_v1_json_fields_are_preserved(data):
    result, _ = data
    assert V1_TOP_LEVEL <= set(result)
    assert V1_SUMMARY <= set(result["summary"])
    assert result["tool"] == "crystal"
    assert result["version"] == __version__
    assert result["summary"]["confirmed_findings"] == 0
    assert result["summary"]["output_role"] == "evidence-only"
    assert result["output_contract"]["final_finding_decision"] is False


def test_json_is_serialisable(data):
    result, _ = data
    text = json.dumps(result, default=list)
    assert json.loads(text)["tool"] == "crystal"


def test_v2_fields_are_present(data):
    result, _ = data
    assert result["build"] == __build__
    assert result["summary"]["detector_signals"] >= 1
    assert result["summary"]["languages"] == {"solidity": 1}
    assert result["contracts"][0]["name"] == "Vault"
    assert result["parsers"]["solidity"]["backend"] in {"tree-sitter", "regex"}
    assert result["quality_report"]["passed"] is True


def test_markdown_report_has_sections(data):
    result, _ = data
    text = markdown(result)
    for heading in ("# Crystal Security Report", "## Detector signals",
                    "## Contracts", "## Symbolic state deltas",
                    "## Finding gate", "## Confirmed findings"):
        assert heading in text
    assert "evidence-only" in text
    assert "Falsify this before believing it" in text


def test_markdown_includes_mermaid_when_result_passed(tmp_path):
    (tmp_path / "Vault.sol").write_text(SOURCE, encoding="utf-8")
    result = research(tmp_path, use_solc=False, use_foundry=False)
    text = markdown(payload(result), result)
    assert "```mermaid" in text
    assert "graph LR" in text


@pytest.mark.invariant
def test_sarif_is_well_formed(data):
    result, _ = data
    document = sarif(result)
    assert document["version"] == "2.1.0"
    run = document["runs"][0]
    assert run["tool"]["driver"]["name"] == "Crystal Security Engine"
    assert run["properties"]["confirmedFindings"] == 0
    assert run["results"]
    for finding in run["results"]:
        # Evidence-only means SARIF severity never escalates past "note".
        assert finding["level"] == "note"
        assert finding["kind"] == "review"
        assert finding["properties"]["validationRequired"] is True
        location = finding["locations"][0]["physicalLocation"]
        assert location["region"]["startLine"] >= 1
    rule_ids = {rule["id"] for rule in run["tool"]["driver"]["rules"]}
    assert {finding["ruleId"] for finding in run["results"]} <= rule_ids


def test_sarif_file_roundtrip(data, tmp_path):
    result, _ = data
    destination = tmp_path / "out.sarif"
    write_sarif(destination, result)
    assert json.loads(destination.read_text(encoding="utf-8"))["version"] == "2.1.0"


def test_arcadia_handoff_shape(data):
    result, _ = data
    document = arcadia(result)
    assert document["schema_version"] == "crystal-arcadia/2.0"
    assert document["producer"]["role"] == "evidence-only"
    assert document["producer"]["decides_severity"] is False
    assert document["producer"]["decides_submission"] is False
    for section in ("target", "signals", "state_model", "validation",
                    "evidence_records", "gate", "counts"):
        assert section in document
    assert document["counts"]["confirmed_findings"] == 0


def test_evidence_records_carry_provenance(data):
    result, _ = data
    assert result["evidence_records"]
    for record in result["evidence_records"]:
        assert record["schema_version"]
        assert record["provenance"]
        assert record["status"] in {"RESEARCH", "VALIDATION", "EVIDENCE"}
        assert "severity" not in record


# -- CLI -------------------------------------------------------------------

def test_environment_report_shape():
    report = environment_report()
    assert report["crystal"]["version"] == __version__
    assert report["python"]["ok"] is True
    assert set(report["parsers"]) >= {"solidity", "rust", "move", "vyper"}
    assert set(report["backends"]) == {"medusa", "echidna", "halmos"}
    assert report["detectors"]


def test_cli_capabilities_lists_v2_features():
    process = subprocess.run(
        [sys.executable, "-m", "crystal.cli", "capabilities"],
        capture_output=True, text=True, check=True,
    )
    assert f"crystal {__version__}" in process.stdout
    for capability in ("tree-sitter-parsing", "symbolic-execution-engine",
                       "sarif-output", "arcadia-output", "zero-fabrication"):
        assert capability in process.stdout


def test_cli_doctor_json():
    process = subprocess.run(
        [sys.executable, "-m", "crystal.cli", "doctor", "--json"],
        capture_output=True, text=True,
    )
    assert json.loads(process.stdout)["crystal"]["build"] == __build__


def test_cli_version_flag():
    process = subprocess.run(
        [sys.executable, "-m", "crystal.cli", "--version"],
        capture_output=True, text=True, check=True,
    )
    assert __version__ in process.stdout


@pytest.mark.parametrize("output_format,filename,probe", [
    ("json", "out.json", lambda text: json.loads(text)["tool"] == "crystal"),
    ("markdown", "out.md", lambda text: "# Crystal Security Report" in text),
    ("sarif", "out.sarif", lambda text: json.loads(text)["version"] == "2.1.0"),
    ("arcadia", "out.json", lambda text: json.loads(text)["producer"]["role"] == "evidence-only"),
])
def test_cli_scan_writes_every_format(output_format, filename, probe, tmp_path):
    (tmp_path / "Vault.sol").write_text(SOURCE, encoding="utf-8")
    destination = tmp_path / filename
    code = main([
        "scan", str(tmp_path), "--no-solc", "--no-foundry", "--quiet",
        "--format", output_format, "-o", str(destination),
    ])
    assert code == 0
    assert probe(destination.read_text(encoding="utf-8"))


def test_cli_scan_rejects_missing_target(tmp_path, capsys):
    assert main(["scan", str(tmp_path / "nope"), "--quiet"]) == 2


def test_cli_scan_rejects_unknown_detector(tmp_path):
    (tmp_path / "Vault.sol").write_text(SOURCE, encoding="utf-8")
    with pytest.raises(SystemExit, match="unknown detector"):
        main(["scan", str(tmp_path), "--no-solc", "--no-foundry", "-q",
              "--detectors", "telepathy"])


def test_cli_validate_generate_only(tmp_path):
    (tmp_path / "Vault.sol").write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "artifacts"
    assert main([
        "validate", str(tmp_path), "--backend", "medusa",
        "--generate-only", "--out-dir", str(out),
    ]) == 0
