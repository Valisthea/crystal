"""Result serialisation: JSON, Markdown, SARIF and the Arcadia hand-off format.

Every v1 JSON field is preserved. v2 only adds fields, so existing consumers
keep parsing Crystal output unchanged.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import asdict, is_dataclass
from pathlib import Path

from . import __build__, __version__

SARIF_SCHEMA = "https://raw.githubusercontent.com/oasis-tcs/sarif-spec/master/Schemata/sarif-schema-2.1.0.json"
ARCADIA_SCHEMA_VERSION = "crystal-arcadia/2.0"

MAX_DETECTORS = 500
MAX_DELTAS = 400
MAX_ANOMALIES = 400
MAX_COMPOSITIONS = 400

# Markdown details the first few of each in full. Whatever it does not detail
# it still counts, so a capped section says how much the JSON payload holds.
MARKDOWN_DETECTOR_DETAILS = 40
MARKDOWN_CANDIDATES_PER_CAMPAIGN = 5

# Reason recorded for a contract the parser classified as a fixture while
# parsing (`test/`, `mock/`, `*.t.sol`, a `Mock`/`Test` name or base). The
# parser keeps only a flag; the research engine records a reason per contract
# only for the directory rules it applies afterwards (`excluded_scaffolding`).
PARSER_FIXTURE_REASON = "test fixture (parser classification)"

# A detector that grades how tightly a differentiating guard is tied to the
# shared call writes the grade into its evidence as `coupling [<grade>]: ...`.
COUPLING_PREFIX = "coupling ["


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


def _coupling_grade(evidence) -> str | None:
    """The coupling grade a detector wrote into its evidence, or None.

    `asymmetric-side-effect` grades a differentiating guard by what it is
    about — state the shared callee writes, arguments it consumes — and the
    grade sets the confidence band. It is lifted out of the evidence here so a
    reader can sort on it without reading every line; nothing is inferred.
    """
    for line in evidence or ():
        if isinstance(line, str) and line.startswith(COUPLING_PREFIX):
            grade = line[len(COUPLING_PREFIX):].split("]", 1)[0].strip()
            return grade or None
    return None


def _detector_payload(signal) -> dict:
    record = asdict(signal)
    # v2 addition: the grade beside the confidence it governs.
    record["coupling"] = _coupling_grade(signal.evidence)
    return record


def _excluded_contracts(result) -> list[dict]:
    """Every contract kept out of research, each with the reason it was.

    Two mechanisms exclude a contract. The parser flags fixtures while parsing
    and records no reason; the research engine then drops whole directories
    (`legacy/`, `test-contracts/`) and records a reason per contract in
    `excluded_scaffolding`. Joining the two gives the excluded bucket a reason
    on every row, so no contract vanishes from research without saying why.
    """
    if result.get("include_tests"):
        return []
    scaffolding = result.get("excluded_scaffolding") or []
    reasons = {
        (entry["contract"], entry["path"]): entry["reason"]
        for entry in scaffolding
    }
    rows = [
        {
            "contract": contract.name,
            "path": str(contract.path),
            "reason": reasons.get((contract.name, contract.path),
                                  PARSER_FIXTURE_REASON),
        }
        for contract in result.get("test_contracts", [])
    ]
    # The engine files every directory exclusion under `test_contracts` as
    # well; should that ever stop being true, the reason must still surface.
    listed = {(row["contract"], row["path"]) for row in rows}
    rows += [
        {"contract": entry["contract"], "path": str(entry["path"]),
         "reason": entry["reason"]}
        for entry in scaffolding
        if (entry["contract"], entry["path"]) not in listed
    ]
    return sorted(rows, key=lambda row: (row["contract"], row["path"]))


def _relative(path, root) -> str:
    """`path` relative to the scanned root when it lies beneath it, as POSIX.

    Contract paths are absolute; the root is whatever the operator typed, so
    both the literal and the resolved root are tried before giving up.
    """
    if not path:
        return ""
    target = Path(path)
    if root:
        for base in (Path(root), Path(root).resolve()):
            try:
                return target.relative_to(base).as_posix()
            except (ValueError, OSError):
                continue
    return target.as_posix()


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


def _campaign_payload(result) -> list[dict]:
    campaign_results = result.get("campaign_results", [])
    out = []
    for cr in campaign_results:
        out.append({
            "campaign_id": cr.campaign_id,
            "campaign_name": cr.campaign_name,
            "candidates": [asdict(c) for c in cr.candidates],
            "deferred": cr.deferred,
            "total_sequences_explored": cr.total_sequences_explored,
            "total_sequences_pruned": cr.total_sequences_pruned,
            "pruned_by": dict(getattr(cr, "pruned_by", {}) or {}),
            "warning": cr.warning,
        })
    return out


def _campaigns_selecting(campaigns) -> dict[tuple[str, ...], list[str]]:
    """Every campaign that selected each chain, keyed by the chain.

    The runner collapses a chain several campaigns selected onto one survivor
    annotated with `selected_by_campaigns`; the hosting campaign is joined with
    that annotation here. A payload built without the runner's pass (a single
    campaign run on its own) still lists the chain under every campaign that
    holds it, so the same aggregation is made from the payload itself.
    """
    selecting: dict[tuple[str, ...], list[str]] = {}
    for campaign in campaigns:
        for candidate in campaign["candidates"]:
            names = selecting.setdefault(tuple(candidate["state_sequence"]), [])
            for campaign_id in (campaign["campaign_id"],
                                *(candidate.get("selected_by_campaigns") or ())):
                if campaign_id not in names:
                    names.append(campaign_id)
    return selecting


def _campaign_markdown(data) -> list[str]:
    """Render campaigns, including the ones that reported nothing.

    A campaign that found nothing is a result, not an absence: it says the
    scope was searched. Printing only the productive ones makes a pack that
    never loaded look identical to a pack that loaded and stayed quiet.

    A chain is rendered once, however many campaigns selected it, and the
    rendering names all of them. What a section does not detail — candidates
    past the per-campaign cap, sequences the campaign deferred — is counted,
    so nothing leaves the Markdown silently.
    """
    campaigns = data.get("campaign_results") or []
    packs = data.get("campaign_packs") or {}
    lines = ["", "## Campaigns", ""]

    if packs:
        for spec, count in sorted(packs.items()):
            state = f"{count} campaign(s)" if count else "loaded nothing"
            lines.append(f"- requested pack `{spec}` — {state}")
        lines.append("")

    if not campaigns:
        lines.append("No campaign ran on this target.")
        return lines

    productive = [c for c in campaigns if c["candidates"]]
    lines.append(
        f"{len(campaigns)} campaign(s) ran; {len(productive)} produced candidates."
    )
    lines += ["", "| Campaign | Candidates | Deferred | Explored | Pruned "
                  "| Why pruned |",
              "| --- | ---: | ---: | ---: | ---: | --- |"]
    for campaign in campaigns:
        why = ", ".join(
            f"{rule}={count}"
            for rule, count in sorted((campaign.get("pruned_by") or {}).items())
        ) or "—"
        lines.append(
            f"| `{campaign['campaign_id']}` | {len(campaign['candidates'])} "
            f"| {len(campaign.get('deferred') or [])} "
            f"| {campaign['total_sequences_explored']} "
            f"| {campaign['total_sequences_pruned']} | {why} |"
        )

    # A warning on a campaign that reported nothing is the most important
    # thing it has to say, so warnings are listed for every campaign.
    warned = [c for c in campaigns if c.get("warning")]
    if warned:
        lines.append("")
        for campaign in warned:
            lines.append(f"- `{campaign['campaign_id']}` — {campaign['warning']}")

    selecting = _campaigns_selecting(campaigns)
    rendered: set[tuple[str, ...]] = set()
    for campaign in productive:
        lines += ["", f"### {campaign['campaign_name']} "
                      f"(`{campaign['campaign_id']}`)", ""]
        if campaign.get("warning"):
            lines.append(f"> {campaign['warning']}")
            lines.append("")
        shown = 0
        elsewhere = 0
        for candidate in campaign["candidates"]:
            chain = tuple(candidate["state_sequence"])
            if chain in rendered:
                elsewhere += 1
                continue
            if shown >= MARKDOWN_CANDIDATES_PER_CAMPAIGN:
                continue
            rendered.add(chain)
            shown += 1
            lines.append(
                f"- **{' -> '.join(chain)}** — "
                f"score {candidate['score']:.2f}, "
                f"confidence {candidate.get('confidence', 0.0):.2f}, "
                f"category `{candidate['category']}`, "
                f"validation `{candidate.get('validation_status') or 'n/a'}`"
            )
            names = selecting.get(chain) or [campaign["campaign_id"]]
            if len(names) > 1:
                lines.append(
                    f"  - selected by {len(names)} campaigns: "
                    + ", ".join(f"`{name}`" for name in names)
                )
            lines.append(f"  - {candidate['hypothesis']}")
            for item in list(candidate.get("evidence") or [])[:6]:
                lines.append(f"  - {item}")
            for question in list(candidate.get("questions") or []):
                lines.append(f"  - open question: {question}")
        hidden = len(campaign["candidates"]) - shown - elsewhere
        if hidden > 0:
            lines.append(
                f"- {hidden} more candidate(s) not detailed here; the JSON "
                "payload carries every one."
            )
        if elsewhere:
            lines.append(
                f"- {elsewhere} candidate(s) already rendered under another "
                "campaign above."
            )
        deferred = list(campaign.get("deferred") or [])
        if deferred:
            lines.append(
                f"- {len(deferred)} lower-scored candidate(s) deferred past "
                "the campaign's `max_candidates`; listed under `deferred` in "
                "the JSON payload."
            )
    return lines


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
    # The engine's per-contract directory exclusions, verbatim. Empty when
    # `--include-tests` restored everything to research.
    scaffolding = [] if result.get("include_tests") else _plain(
        result.get("excluded_scaffolding") or []
    )

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
            # Of `test_contracts_excluded`, how many the directory rules
            # (`legacy/`, `test-contracts/`) dropped after parsing.
            "scaffolding_excluded": len(scaffolding),
        },
        "excluded_test_contracts": sorted(
            f"{c.name} ({Path(c.path).name})" for c in result.get("test_contracts", [])
        ) if not result.get("include_tests") else [],
        # v2 additions: the same excluded bucket with a reason on every row.
        "excluded_scaffolding": scaffolding,
        "excluded_contracts": _excluded_contracts(result),
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
        "detectors": [_detector_payload(x) for x in detectors[:MAX_DETECTORS]],
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
        "campaign_results": _campaign_payload(result),
        "campaign_packs": dict(result.get("campaign_packs") or {}),
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


def _detector_markdown(data) -> list[str]:
    """Every signal in one sortable table, then the first few in full.

    The coupling grade is the discriminant that put a signal in its confidence
    band; a reader triaging forty signals needs it beside the confidence, not
    buried in the evidence of each one.
    """
    signals = data["detectors"]
    lines = ["", "## Detector signals", ""]
    if not signals:
        lines += ["No structural detector produced a signal on this target.", ""]
        return lines

    lines += [
        f"{len(signals)} signal(s). *Coupling* is the grade a detector gives "
        "the guard that differentiates two paths to the same call — how "
        "tightly it is tied to what that call does — and it sets the "
        "confidence band; `—` means the detector does not grade.",
        "",
        "| # | Detector | Function | Confidence | Coupling | Location |",
        "| ---: | --- | --- | ---: | --- | --- |",
    ]
    for index, signal in enumerate(signals, 1):
        coupling = signal.get("coupling") or _coupling_grade(signal["evidence"])
        location = f"{Path(signal['path']).name}:{signal['line']}" \
            if signal["path"] else f"line {signal['line']}"
        lines.append(
            f"| {index} | `{signal['detector']}` "
            f"| `{signal['contract']}.{signal['function']}` "
            f"| {signal['confidence']} | {coupling or '—'} | `{location}` |"
        )
    if len(signals) > MARKDOWN_DETECTOR_DETAILS:
        lines += ["", f"The first {MARKDOWN_DETECTOR_DETAILS} of {len(signals)} "
                      "are detailed below; the table lists every one, and so "
                      "does the JSON payload."]

    for index, signal in enumerate(signals[:MARKDOWN_DETECTOR_DETAILS], 1):
        coupling = signal.get("coupling") or _coupling_grade(signal["evidence"])
        lines += [
            "",
            f"### {index}. {signal['title']}",
            "",
            f"- Detector: `{signal['detector']}` — confidence "
            f"{signal['confidence']}"
            + (f" — coupling `{coupling}`" if coupling else "")
            + f" — status `{signal['status']}`",
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
    return lines


def _excluded_markdown(data) -> list[str]:
    """Every contract kept out of research, with the reason, uncapped.

    Fifty-two of sixty-nine contracts vanished from one real scan with a
    forty-name list and no reason on any of them. The list is now a table
    with a reason per row, and it is not truncated: an exclusion the operator
    cannot see is one they cannot dispute.
    """
    rows = data.get("excluded_contracts")
    if rows is None:
        # A payload from before the reasons existed: the bare names.
        names = data.get("excluded_test_contracts") or []
        if not names:
            return []
        return ["## Excluded fixtures", "",
                f"{len(names)} type(s) were classified as test fixtures and "
                "kept out of research. Re-run with `--include-tests` to "
                "research them anyway.", ""] + \
            [f"- `{name}`" for name in names] + [""]
    if not rows:
        return []

    by_reason = Counter(row["reason"] for row in rows)
    parser_count = by_reason.pop(PARSER_FIXTURE_REASON, 0)
    directory = ", ".join(
        f"{count} {reason}" for reason, count in sorted(by_reason.items())
    )
    summary = (
        f"{len(rows)} type(s) were parsed but kept out of research — "
        f"{parser_count} classified as a test fixture by the parser"
        + (f", {len(rows) - parser_count} by directory rule ({directory})"
           if directory else "")
        + ". A mock runtime mutates state and skips authority checks by "
        "design, and a superseded copy duplicates the live logic. Re-run with "
        "`--include-tests` to research them anyway."
    )
    lines = ["## Excluded fixtures", "", summary, "",
             "| Contract | Path | Reason |", "| --- | --- | --- |"]
    project = data.get("project", "")
    for row in sorted(rows, key=lambda r: (r["reason"], r["contract"], r["path"])):
        lines.append(
            f"| `{row['contract']}` | `{_relative(row['path'], project)}` "
            f"| {row['reason']} |"
        )
    lines.append("")
    return lines


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

    lines += _detector_markdown(data)
    lines += _excluded_markdown(data)

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

    lines += _campaign_markdown(data)

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
            "excluded_contracts": data.get("excluded_contracts", []),
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
        "campaigns": data.get("campaign_results", []),
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
