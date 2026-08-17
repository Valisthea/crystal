"""Result serialisation: JSON, Markdown, SARIF and the Arcadia hand-off format.

Every v1 JSON field is preserved. v2 only adds fields, so existing consumers
keep parsing Crystal output unchanged.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import __build__, __version__

SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
ARCADIA_SCHEMA_VERSION = "crystal-arcadia/2.0"

MAX_DETECTORS = 500
MAX_DELTAS = 400
MAX_ANOMALIES = 400
MAX_COMPOSITIONS = 400


def _plain(value):
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return [_plain(x) for x in value]
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, Path):
        return str(value)
    return value


def _contract_summaries(result):
    summaries = []
    detectors_by_contract: dict[str, int] = {}
    for signal in result.get("detectors", []):
        detectors_by_contract[signal.contract] = \
            detectors_by_contract.get(signal.contract, 0) + 1
    for contract in result["contracts"]:
        summaries.append({
            "name": contract.name,
            "kind": contract.kind,
            "language": contract.language,
            "parser": contract.parser,
            "path": contract.path,
            "line": contract.line,
            "bases": list(contract.bases),
            "state_variables": len(contract.state_vars),
            "functions": len(contract.functions),
            "entry_points": sum(1 for f in contract.functions if f.is_entry_point),
            "events": len(contract.events),
            "errors": len(contract.errors),
            "modifiers": [m.name for m in contract.modifier_definitions],
            "detector_signals": detectors_by_contract.get(contract.name, 0),
            "functions_with_ir": sum(1 for f in contract.functions if f.has_ir),
        })
    return summaries


def _composition_payload(model) -> dict:
    """Pipeline topology, plus the limits that stop it being over-read."""
    if model is None:
        return {"available": False, "pipelines": [], "warning": "", "limits": ""}
    return {
        "available": bool(model.pipelines),
        "warning": model.warning,
        "limits": model.limits,
        "workspace": {
            "is_workspace": model.topology.profile.is_workspace,
            "runtime_found": model.topology.profile.has_runtime,
            "runtime_files": model.topology.profile.runtime_files[:10],
            "substrate": model.topology.profile.substrate,
        } if model.topology and model.topology.profile else {},
        "runtime_modules": [
            {"index": m.index, "alias": m.alias, "crate": m.crate}
            for m in (model.topology.modules if model.topology else [])
        ],
        "associated_types": model.associated_types,
        "pipelines": [
            {
                "name": pipeline.name,
                "path": pipeline.path,
                "line": pipeline.line,
                "stages": [
                    {
                        "index": stage.index,
                        "type": stage.name,
                        "crate": stage.crate,
                        "role": stage.role,
                        "scope": stage.scope(),
                        "resolved": stage.resolved,
                        "mechanisms": list(stage.mechanisms),
                        "consulted_guards": list(stage.consulted_guards),
                        "metadata_checks": list(stage.metadata_checks),
                        "routes": list(stage.routes),
                        "external_routes": list(stage.external_routes),
                    }
                    for stage in pipeline.stages
                ],
            }
            for pipeline in model.pipelines
        ],
        "boundary_crossings": [
            {
                "pipeline": crossing.pipeline,
                "guard_stage": {
                    "index": crossing.guard.index, "type": crossing.guard.name,
                    "role": crossing.guard.role, "scope": crossing.guard.scope(),
                    "mechanism": crossing.guard.guard_evidence[:1],
                },
                "value_stage": {
                    "index": crossing.mover.index, "type": crossing.mover.name,
                    "role": crossing.mover.role, "scope": crossing.mover.scope(),
                    "mechanism": crossing.mover.value_evidence[:1],
                    "routes": list(crossing.mover.routes),
                },
                "boundary": crossing.boundary,
                "confidence": crossing.confidence,
                "notes": list(crossing.notes),
            }
            for crossing in model.crossings
        ],
    }


def payload(result):
    compiler_model = result.get("compiler_model")
    protocol_model = result["protocol_model"]
    concrete = result.get("concrete_validation", [])
    detectors = result.get("detectors", [])
    quality = result.get("quality_report")

    return {
        "tool": "crystal",
        "version": __version__,
        "build": __build__,
        "status": "ok",
        "project": str(result.get("project", "")),
        "summary": {
            "sources": len(result["sources"]),
            "contracts": len(result["contracts"]),
            "research_candidates": len(result.get("research_candidates", [])),
            "behavior_relations": len(result["behavior_relations"]),
            "differential_candidates": len(result["differential_candidates"]),
            "mutations": len(result["mutations"]),
            "impact_paths": len(result["impact_paths"]),
            "state_deltas": len(result["state_deltas"]),
            "delta_anomalies": len(result["delta_anomalies"]),
            "composition_candidates": len(result["composition_candidates"]),
            "novel_behaviors": len(result["novel_behaviors"]),
            "unknown_behavior_candidates": len(result["unknown_behavior_candidates"]),
            "constraints": len(result["constraints"]),
            "concrete_hypotheses": len(concrete),
            "foundry_available": bool(
                result.get("foundry_capabilities")
                and result["foundry_capabilities"].forge
            ),
            "foundry_hypotheses": len(result.get("foundry_validation", [])),
            "foundry_executed": sum(
                x.status.startswith("EXECUTED")
                for x in result.get("foundry_validation", [])
            ),
            "counterexample_hypotheses": sum(
                x.status == "COUNTEREXAMPLE" for x in concrete
            ),
            "confirmed_findings": 0,
            "output_role": "evidence-only",
            "protocol_invariants": len(result["protocol_invariants"]),
            "sequence_hypotheses": len(result["sequence_hypotheses"]),
            "causal_edges": len(result["state_graph"].causal_edges),
            "solc_available": bool(compiler_model and compiler_model.available),
            "token_functions": len(protocol_model.token_functions),
            "value_flows": len(protocol_model.value_flows),
            "accounting_relations": len(protocol_model.accounting_relations),
            "oracle_signals": len(protocol_model.oracle_signals),
            "fee_signals": len(protocol_model.fee_signals),
            # v2 additions.
            "detector_signals": len(detectors),
            "languages": dict(result.get("project_profile").languages)
            if result.get("project_profile") else {},
            "frameworks": list(result.get("project_profile").frameworks)
            if result.get("project_profile") else [],
            "parser_backends": result.get("parser_backends", "unknown"),
            "functions_with_ir": sum(
                1 for c in result["contracts"] for f in c.functions if f.has_ir
            ),
            "evidence_records": len(result.get("evidence_records", [])),
            "quality_passed": bool(quality.passed) if quality else None,
            "test_contracts_excluded": len(result.get("test_contracts", []))
            if not result.get("include_tests") else 0,
            "contracts_parsed_total": len(result.get("all_contracts",
                                                     result["contracts"])),
        },
        "excluded_test_contracts": sorted(
            f"{c.name} ({Path(c.path).name})" for c in result.get("test_contracts", [])
        ) if not result.get("include_tests") else [],
        "composition": _composition_payload(result.get("composition")),
        "finding_gate": _plain(result.get("finding_gate", {})),
        "evidence_records": [asdict(x) for x in result.get("evidence_records", [])],
        "output_contract": {
            "role": "evidence-only",
            "final_finding_decision": False,
            "severity_decision": False,
            "submission_decision": False,
        },
        "constraints": [asdict(x) for x in result.get("constraints", [])],
        "concrete_validation": [asdict(x) for x in concrete],
        "foundry_capabilities": asdict(result["foundry_capabilities"])
        if result.get("foundry_capabilities") else None,
        "foundry_validation": [asdict(x) for x in result.get("foundry_validation", [])],
        "novel_behaviors": [asdict(x) for x in result["novel_behaviors"]],
        "unknown_behavior_candidates": [
            asdict(x) for x in result["unknown_behavior_candidates"]
        ],
        "state_deltas": [asdict(x) for x in result["state_deltas"][:MAX_DELTAS]],
        "delta_anomalies": [
            asdict(x) for x in result["delta_anomalies"][:MAX_ANOMALIES]
        ],
        "composition_candidates": [
            asdict(x) for x in result["composition_candidates"][:MAX_COMPOSITIONS]
        ],
        # v2 additions.
        "detectors": [asdict(x) for x in detectors[:MAX_DETECTORS]],
        "differential_candidates": [
            asdict(x) for x in result["differential_candidates"][:MAX_DETECTORS]
        ],
        "contracts": _contract_summaries(result),
        "parsers": _plain(result.get("parsers", {})),
        "parse_diagnostics": _plain(result.get("parse_diagnostics", [])),
        "project_profile": _plain(result.get("project_profile")),
        "protocol_invariants": [asdict(x) for x in result["protocol_invariants"]],
        "invariant_candidates": [asdict(x) for x in result["invariant_candidates"]],
        "research_candidates": [
            asdict(x) for x in result.get("research_candidates", [])[:1000]
        ],
        "quality_report": _plain(quality),
    }


def write_json(path, data):
    Path(path).write_text(
        json.dumps(data, indent=2, default=list), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------

def _mermaid_call_graph(result, limit=60):
    edges = result.get("call_graph", [])[:limit]
    if not edges:
        return []
    lines = ["```mermaid", "graph LR"]
    for edge in edges:
        lines.append(
            f'  {_node(edge.source)}["{edge.source}"] --> '
            f'{_node(edge.target)}["{edge.target}"]'
        )
    lines.append("```")
    return lines


def _mermaid_state_graph(result, limit=40):
    edges = result["state_graph"].causal_edges[:limit]
    if not edges:
        return []
    lines = ["```mermaid", "graph LR"]
    for edge in edges:
        label = ",".join(edge.consumed[:3])
        lines.append(
            f'  {_node(edge.source)}["{edge.source}"] -->|{label}| '
            f'{_node(edge.target)}["{edge.target}"]'
        )
    lines.append("```")
    return lines


def _node(name: str) -> str:
    return "n_" + "".join(c if c.isalnum() else "_" for c in name)


def markdown(data, result=None) -> str:
    summary = data["summary"]
    lines = [
        f"# Crystal Security Report — {data['project'] or 'target'}",
        "",
        f"`crystal {data['version']} build {data['build']}` — "
        "**evidence-only output**. Crystal never emits a confirmed finding; "
        "every item below is a research signal that requires validation.",
        "",
        "## Target",
        "",
        f"- Sources: **{summary['sources']}** — contracts: **{summary['contracts']}**",
        f"- Languages: {summary['languages'] or 'n/a'}",
        f"- Frameworks: {', '.join(summary['frameworks']) or 'none detected'}",
        f"- Parser backends: `{summary['parser_backends']}`",
        f"- Functions with statement IR: {summary['functions_with_ir']}",
        f"- solc available: {summary['solc_available']} — "
        f"Foundry available: {summary['foundry_available']}",
        "",
        "## Signal counts",
        "",
        "| Signal | Count |",
        "| --- | ---: |",
    ]
    for key in (
        "detector_signals", "state_deltas", "delta_anomalies",
        "composition_candidates", "unknown_behavior_candidates",
        "differential_candidates", "impact_paths", "sequence_hypotheses",
        "protocol_invariants", "evidence_records", "confirmed_findings",
    ):
        lines.append(f"| {key.replace('_', ' ')} | {summary.get(key, 0)} |")

    lines += ["", "## Detector signals", ""]
    if not data["detectors"]:
        lines.append("No structural detector produced a signal on this target.")
    for signal in data["detectors"][:40]:
        lines += [
            f"### {signal['title']}",
            "",
            f"- Detector: `{signal['detector']}` — confidence "
            f"{signal['confidence']} — status `{signal['status']}`",
            f"- Location: `{signal['path']}:{signal['line']}`",
            f"- Mechanism: {signal['reason']}",
            "",
            "Evidence:",
            "",
        ]
        lines += [f"- {item}" for item in signal["evidence"]]
        if signal["ordered_trace"]:
            lines += ["", "Ordered trace:", "", "```"]
            lines += list(signal["ordered_trace"])
            lines.append("```")
        if signal["falsification"]:
            lines += ["", "Falsify this before believing it:", ""]
            lines += [f"- {item}" for item in signal["falsification"]]
        lines.append("")

    if data.get("excluded_test_contracts"):
        lines += [
            "## Excluded fixtures", "",
            f"{len(data['excluded_test_contracts'])} type(s) were classified as test "
            "fixtures and kept out of research: a mock runtime mutates state and "
            "skips authority checks by design. Re-run with `--include-tests` to "
            "research them anyway.", "",
        ]
        lines += [f"- `{name}`" for name in data["excluded_test_contracts"][:40]]
        lines.append("")

    lines += ["## Contracts", "", "| Contract | Kind | Lang | Functions | Entry points | State | Signals |",
              "| --- | --- | --- | ---: | ---: | ---: | ---: |"]
    for contract in data["contracts"]:
        lines.append(
            f"| `{contract['name']}` | {contract['kind']} | {contract['language']} "
            f"| {contract['functions']} | {contract['entry_points']} "
            f"| {contract['state_variables']} | {contract['detector_signals']} |"
        )

    lines += ["", "## Symbolic state deltas", ""]
    if not data["state_deltas"]:
        lines.append("No sequence produced a symbolic delta.")
    else:
        lines += ["| Sequence | Changed state | Delta |", "| --- | --- | --- |"]
        for delta in data["state_deltas"][:40]:
            changes = "; ".join(
                f"{name}={value}" for name, value in delta["delta"].items()
                if value != "0"
            ) or "—"
            lines.append(
                f"| `{' -> '.join(delta['sequence'])}` "
                f"| {', '.join(delta['changed']) or '—'} | `{changes}` |"
            )

    lines += ["", "## Delta anomalies", ""]
    if not data["delta_anomalies"]:
        lines.append("No accounting asymmetry detected.")
    else:
        lines += ["| Kind | State | Delta | Sequence |", "| --- | --- | --- | --- |"]
        for anomaly in data["delta_anomalies"][:40]:
            lines.append(
                f"| {anomaly['kind']} | `{anomaly['state']}` "
                f"| `{anomaly['delta']}` | `{' -> '.join(anomaly['sequence'])}` |"
            )

    lines += ["", "## Candidate invariants", ""]
    if data["protocol_invariants"]:
        lines += ["| Category | Expression | Confidence |", "| --- | --- | ---: |"]
        for invariant in data["protocol_invariants"][:40]:
            lines.append(
                f"| {invariant['category']} | {invariant['expression']} "
                f"| {invariant['confidence']} |"
            )
    else:
        lines.append("No protocol invariant candidate derived.")

    lines += ["", "## Concrete validation", ""]
    for item in data["concrete_validation"][:100]:
        lines.append(
            f"- `{item['status']}` — `{' -> '.join(item['hypothesis'])}` — "
            f"trials={item['trials']} counterexamples={len(item['counterexamples'])}"
        )
        for assumption in item.get("assumptions", [])[:4]:
            lines.append(f"  - assumption: {assumption}")
    if not data["concrete_validation"]:
        lines.append("No hypothesis reached concrete validation.")

    lines += ["", "## Real-EVM validation (Foundry)", ""]
    for item in data["foundry_validation"][:50]:
        lines.append(
            f"- `{item['status']}` — `{' -> '.join(item['hypothesis'])}`"
            + (f" — {item['unsupported_reason']}" if item.get("unsupported_reason") else "")
        )
    if not data["foundry_validation"]:
        lines.append("No hypothesis was submitted to the Foundry backend.")

    if result is not None:
        call_graph = _mermaid_call_graph(result)
        if call_graph:
            lines += ["", "## Call graph", ""] + call_graph
        state_graph = _mermaid_state_graph(result)
        if state_graph:
            lines += ["", "## State causality graph", ""] + state_graph

    gate = data["finding_gate"]
    lines += [
        "", "## Finding gate", "",
        f"- Policy: `{gate.get('policy')}`",
        f"- Confirmed findings: **0** — concrete validation never bypasses the gate.",
        f"- Gates never set automatically: "
        f"{', '.join(gate.get('report', {}).get('human_only_gates', []))}",
        "",
        "Falsification checks applied to every candidate:",
        "",
    ]
    lines += [f"- {question}" for question in gate.get("anti_finding_checks", [])]

    poc = gate.get("report", {}).get("poc_requests", [])
    if poc:
        lines += ["", "### Proof-of-concept requests", "",
                  "Every machine-checkable gate is satisfied for these sequences; "
                  "only a reproducible trace is missing.", ""]
        lines += [f"- `{' -> '.join(item['sequence'])}`" for item in poc[:20]]

    lines += ["", "## Confirmed findings", "",
              "0 — deciding whether a signal is a vulnerability belongs to the "
              "reviewer, not to Crystal."]
    return "\n".join(lines) + "\n"


def write_markdown(path, data, result=None):
    Path(path).write_text(markdown(data, result), encoding="utf-8")


# ---------------------------------------------------------------------------
# SARIF
# ---------------------------------------------------------------------------

def sarif(data) -> dict:
    rules: dict[str, dict] = {}
    results = []

    for signal in data["detectors"]:
        rule_id = signal["detector"]
        rules.setdefault(rule_id, {
            "id": rule_id,
            "name": rule_id.replace("-", " ").title().replace(" ", ""),
            "shortDescription": {"text": signal["title"]},
            "fullDescription": {"text": signal["reason"]},
            "help": {
                "text": "Crystal emits research evidence, not confirmed findings. "
                        "Falsify before reporting: "
                        + " ".join(signal["falsification"]),
            },
            "properties": {
                "tags": ["security", "crystal-evidence"] + list(signal["references"]),
                "precision": "medium",
            },
            "defaultConfiguration": {"level": "note"},
        })
        results.append({
            "ruleId": rule_id,
            "level": "note",
            "kind": "review",
            "message": {
                "text": f"{signal['title']} — {signal['reason']} "
                        f"(evidence-only, confidence {signal['confidence']})",
            },
            "locations": [{
                "physicalLocation": {
                    "artifactLocation": {"uri": _uri(signal["path"])},
                    "region": {"startLine": max(1, int(signal["line"]))},
                },
            }],
            "properties": {
                "confidence": signal["confidence"],
                "status": signal["status"],
                "evidence": list(signal["evidence"]),
                "orderedTrace": list(signal["ordered_trace"]),
                "falsification": list(signal["falsification"]),
                "validationRequired": True,
            },
        })

    return {
        "$schema": SARIF_SCHEMA,
        "version": "2.1.0",
        "runs": [{
            "tool": {
                "driver": {
                    "name": "Crystal Security Engine",
                    "version": data["version"],
                    "semanticVersion": data["version"],
                    "informationUri": "https://github.com/Valisthea/crystal",
                    "rules": list(rules.values()),
                },
            },
            "results": results,
            "properties": {
                "outputRole": "evidence-only",
                "confirmedFindings": 0,
                "build": data["build"],
            },
        }],
    }


def _uri(path: str) -> str:
    if not path:
        return "unknown"
    return Path(path).as_posix()


def write_sarif(path, data):
    Path(path).write_text(json.dumps(sarif(data), indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Arcadia hand-off
# ---------------------------------------------------------------------------

def arcadia(data) -> dict:
    """Structured hand-off for Arcadia/OMEGA correlation."""
    return {
        "schema_version": ARCADIA_SCHEMA_VERSION,
        "producer": {
            "tool": "crystal",
            "version": data["version"],
            "build": data["build"],
            "role": "evidence-only",
            "decides_severity": False,
            "decides_submission": False,
        },
        "target": {
            "project": data["project"],
            "languages": data["summary"]["languages"],
            "frameworks": data["summary"]["frameworks"],
            "contracts": data["contracts"],
            "parsers": data["parsers"],
            "parse_diagnostics": data["parse_diagnostics"],
        },
        "signals": {
            "detectors": data["detectors"],
            "delta_anomalies": data["delta_anomalies"],
            "composition_candidates": data["composition_candidates"],
            "unknown_behaviors": data["unknown_behavior_candidates"],
            "differential_candidates": data.get("differential_candidates", []),
        },
        "state_model": {
            "state_deltas": data["state_deltas"],
            "invariant_candidates": data["invariant_candidates"],
            "protocol_invariants": data["protocol_invariants"],
        },
        "validation": {
            "concrete": data["concrete_validation"],
            "foundry": data["foundry_validation"],
            "constraints": data["constraints"],
        },
        "evidence_records": data["evidence_records"],
        "gate": data["finding_gate"],
        "quality": data["quality_report"],
        "counts": data["summary"],
    }


def write_arcadia(path, data):
    Path(path).write_text(
        json.dumps(arcadia(data), indent=2, default=list), encoding="utf-8"
    )


WRITERS = {
    "json": write_json,
    "markdown": write_markdown,
    "sarif": write_sarif,
    "arcadia": write_arcadia,
}
