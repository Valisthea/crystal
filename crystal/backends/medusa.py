"""Medusa property-fuzzing backend.

Output formats below were read off Medusa 1.5.1 on 2026-09-05:

* property lines   `[PASSED] Property Test: CrystalMedusaHarness.property_x()`
                   (`[FAILED]`, `[NOT STARTED]`), colour codes stripped;
* metrics line     `fuzz: elapsed: 0s, calls: 0 (0/sec), seq/s: 0, ...`;
* revert report    `<corpus>/coverage/revert_report.json`, per harness function
                   `totalCalls`, `totalReverts`, `revertReasonMetrics`. The
                   directory has to exist before the run or Medusa fails to
                   write it.

The revert report is the witness: successes of a forwarding handler are
successes of the entry point it forwards, and the decoded reasons say why the
rest never got through.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path

from .. import process
from ..research.foundry import argument_literal
from . import preflight
from .actions import property_subject, transitive_writes
from .base import (
    GENERATED,
    REFUSED,
    TIMEOUT,
    UNAVAILABLE,
    UNSUPPORTED,
    BackendExecution,
    BackendTraits,
    derive_properties,
    execution_status,
    probe,
)
from .harness import HandlerPlan, declarations, fuzz_handlers
from .selectors import artifact_files, selector_table
from .verdict import HELD, VIOLATED, ActionOutcome, ExecutionWitness, decide, summarise

NAME = "medusa"
HARNESS_CONTRACT = "CrystalMedusaHarness"
HARNESS_FILE = f"{HARNESS_CONTRACT}.sol"
CONFIG_FILE = "medusa.json"
REVERT_REPORT = Path("corpus") / "coverage" / "revert_report.json"
PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")

TRAITS = BackendTraits(
    NAME, advances_block=True, funds_senders=True, fuzzes_nested_deployments=False,
    note="advances block.number/timestamp (blockNumberDelayMax); senders funded at "
         "genesis; does not call contracts the harness deploys (measured 1.5.1)",
)

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
PROPERTY_RE = re.compile(
    r"\[(PASSED|FAILED|NOT STARTED|RUNNING)\]\s+Property Test:\s*(?:\w+\.)?(property_\w+)\("
)
STATUS_LINE_RE = re.compile(r"^\s*\[(?:PASSED|FAILED|NOT STARTED|RUNNING)\]|^\s*Test summary")
CALLS_RE = re.compile(r"\bcalls:\s*(\d+)")


def detect():
    return probe("medusa", "--version")


def config(contract_name: str, test_limit: int = 50000,
           harness_file: str = HARNESS_FILE) -> dict:
    """Keys as Medusa 1.5.1 writes them with `medusa init`.

    The compilation target is the harness *file*: `crytic-compile .` on the
    directory silently dropped the harness and compiled the target alone.
    """
    return {
        "fuzzing": {
            "workers": 4,
            "testLimit": test_limit,
            "callSequenceLength": 100,
            "corpusDirectory": "corpus",
            "coverageEnabled": True,
            "coverageFormats": ["lcov"],
            "revertReporterEnabled": True,
            "targetContracts": [contract_name],
            "senderAddresses": ["0x10000", "0x20000", "0x30000"],
            "deployerAddress": "0x30000",
            "blockNumberDelayMax": 60480,
            "blockTimestampDelayMax": 604800,
            "testing": {
                "stopOnFailedTest": False,
                "stopOnNoTests": True,
                "testAllContracts": False,
                "testViewMethods": False,
                "assertionTesting": {"enabled": True},
                "propertyTesting": {"enabled": True, "testPrefixes": ["property_"]},
                "optimizationTesting": {"enabled": False},
            },
        },
        "compilation": {
            "platform": "crytic-compile",
            "platformConfig": {"target": harness_file},
        },
        "slither": {"useSlither": False},
        "logging": {"level": "info", "noColor": True},
    }


def harness(contract, properties, plan: HandlerPlan | None = None, contracts=()) -> str:
    constructor = next(
        (f for f in contract.functions if f.kind == "constructor"), None
    )
    arguments = []
    if constructor is not None:
        for index, parameter in enumerate(constructor.params):
            literal = argument_literal(parameter.type_name, index)
            if literal is None:
                return ""
            arguments.append(literal)

    pragma = "^0.8.20"
    if contract.path:
        try:
            match = PRAGMA_RE.search(
                Path(contract.path).read_text(encoding="utf-8", errors="ignore")
            )
            pragma = match.group(1).strip() if match else pragma
        except OSError:
            pass

    plan = plan if plan is not None else fuzz_handlers(contract, properties, contracts)
    lines = [
        "// SPDX-License-Identifier: UNLICENSED",
        f"pragma solidity {pragma};",
        "",
        f'import "./{Path(contract.path).name}";' if contract.path else "",
        "",
        f"contract {HARNESS_CONTRACT} {{",
        f"    {contract.name} internal target;",
    ]
    lines += declarations(properties)
    lines += [
        "",
        "    constructor() {",
        f"        target = new {contract.name}({', '.join(arguments)});",
        "    }",
        "",
    ]
    lines += plan.lines
    for prop in properties:
        lines += [
            f"    // {prop.comment} (derived from {prop.origin})",
            f"    function {prop.name}() public view returns (bool) {{",
            f"        return {prop.expression};",
            "    }",
            "",
        ]
    lines.append("}")
    return "\n".join(line for line in lines if line is not None)


@dataclass(frozen=True)
class _Planned:
    contract: object
    properties: tuple
    unsupported: tuple[str, ...]
    plan: HandlerPlan
    artifacts: dict


def _plan(contracts, protocol_invariants, results: list) -> list[_Planned]:
    planned: list[_Planned] = []
    for contract in contracts:
        if contract.kind in {"interface", "library", "file"} or contract.language != "solidity":
            continue
        properties, unsupported = derive_properties(contract, protocol_invariants)
        if not properties:
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported),
                "no property could be derived without fabricating protocol "
                "assumptions",
            ))
            continue
        plan = fuzz_handlers(contract, properties, contracts)
        source = harness(contract, properties, plan, contracts)
        if not source:
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported),
                "constructor arguments could not be derived from the signature",
            ))
            continue
        artifacts = {
            HARNESS_FILE: source,
            CONFIG_FILE: json.dumps(config(HARNESS_CONTRACT), indent=2),
        }
        planned.append(_Planned(
            contract, tuple(properties), tuple(unsupported) + plan.skipped, plan, artifacts,
        ))
    return planned


def _preflight(project, planned, contracts) -> preflight.PreflightReport:
    return preflight.run(
        project, [item.contract for item in planned], NAME, TRAITS,
        {item.contract.name: item.properties for item in planned},
        {item.contract.name: item.artifacts[HARNESS_FILE] for item in planned},
        {item.contract.name: item.artifacts[CONFIG_FILE] for item in planned},
        all_contracts=contracts,
    )


def run(project, contracts, protocol_invariants=(), timeout=300) -> list[BackendExecution]:
    results: list[BackendExecution] = []
    planned = _plan(contracts, protocol_invariants, results)
    if not planned:
        return results

    # Pre-flight reads the target, the harness and the config — never the tool.
    report = _preflight(project, planned, contracts)
    capabilities = detect()

    for item in planned:
        name = item.contract.name
        names = tuple(prop.name for prop in item.properties)
        lines = tuple(issue.line() for issue in report.for_contract(name))
        blocking = report.blocking(name)
        if blocking:
            results.append(BackendExecution(
                NAME, REFUSED, name, names, item.artifacts, None, "", "", (),
                item.unsupported,
                "pre-flight refused to launch: " + " | ".join(issue.message for issue in blocking),
                False, (), lines,
            ))
            continue
        if not capabilities.available:
            results.append(BackendExecution(
                NAME, UNAVAILABLE, name, names, item.artifacts, None, "",
                capabilities.reason, (), item.unsupported, capabilities.reason,
                False, (), lines,
            ))
            continue
        results.append(_execute(project, item, contracts, report, lines, timeout))
    return results


def _execute(project, item: _Planned, contracts, report, lines, timeout) -> BackendExecution:
    contract = item.contract
    names = tuple(prop.name for prop in item.properties)
    with tempfile.TemporaryDirectory(prefix="crystal-medusa-") as directory:
        root = Path(directory)
        if contract.path and Path(contract.path).exists():
            shutil.copy2(contract.path, root / Path(contract.path).name)
        for filename, content in item.artifacts.items():
            (root / filename).write_text(content, encoding="utf-8")
        (root / REVERT_REPORT).parent.mkdir(parents=True, exist_ok=True)
        completed = process.run(
            ["medusa", "fuzz", "--config", CONFIG_FILE], cwd=root, timeout=timeout,
        )
        if completed.error == "timeout":
            return BackendExecution(
                NAME, TIMEOUT, contract.name, names, item.artifacts, None,
                completed.stdout, completed.stderr, (), item.unsupported,
                "medusa timed out", False, (), lines,
            )
        report_text = ""
        report_path = root / REVERT_REPORT
        if report_path.is_file():
            try:
                report_text = report_path.read_text(encoding="utf-8")
            except OSError:
                report_text = ""

    output = ANSI_RE.sub("", completed.output)
    statuses, counterexamples = parse_properties(output)
    calls = parse_calls(output)
    table = selector_table(
        _declaring(contract, contracts),
        artifact_files(project, [contract.name, *contract.bases]),
    )
    outcomes = action_outcomes(report_text, item.plan, table)
    verdicts = []
    for prop in item.properties:
        subject = property_subject(prop, contract)
        writers = {
            handler for handler, function in item.plan.handlers.items()
            if transitive_writes(contract, function, contracts) & set(subject)
        }
        actions = tuple(
            replace(outcome, mutates_subject=outcome.handler in writers)
            for outcome in outcomes
        )
        notes = []
        if not report_text:
            notes.append("no revert report was written; per-action metrics unavailable")
        if not item.plan.handlers:
            notes.append("no entry point could be forwarded by the harness")
        witness = ExecutionWitness(
            NAME, f"{REVERT_REPORT.as_posix()} + stdout", subject,
            checked=statuses.get(prop.name) in {"PASSED", "FAILED"},
            calls=calls, sequences=None, actions=actions,
            revert_selectors=tuple(dict.fromkeys(
                reason for outcome in outcomes for reason, _ in outcome.revert_reasons
            )),
            notes=tuple(notes),
        )
        verdicts.append(decide(
            prop.name, witness, counterexamples.get(prop.name, ()),
            report.undecidable(contract.name, prop.name),
        ))

    status = execution_status(verdicts, evaluated=bool(statuses))
    reason = summarise(verdicts)
    if not statuses:
        reason = (
            f"medusa exited {completed.returncode} before evaluating any property: "
            f"{_last_error(output)}"
        )
    artifacts = dict(item.artifacts)
    if report_text:
        artifacts["medusa.revert_report.json"] = report_text
    return BackendExecution(
        NAME, status, contract.name, names, artifacts, completed.returncode,
        completed.stdout[-8000:], completed.stderr[-4000:],
        tuple(trace for traces in counterexamples.values() for trace in traces)[:20],
        item.unsupported, reason,
        any(item.verdict in {HELD, VIOLATED} for item in verdicts),
        tuple(verdicts), lines,
    )


def _declaring(contract, contracts):
    names = {contract.name, *contract.bases}
    return [candidate for candidate in contracts if candidate.name in names] or [contract]


def _last_error(output: str) -> str:
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped and not stripped.startswith("fuzz:"):
            return stripped[:300]
    return "no output"


def parse_properties(output: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    """Final status per property, and the call sequence printed under a failure."""
    statuses: dict[str, str] = {}
    counterexamples: dict[str, tuple[str, ...]] = {}
    lines = output.splitlines()
    for index, line in enumerate(lines):
        match = PROPERTY_RE.search(line)
        if not match:
            continue
        status, name = match.group(1), match.group(2)
        if status in {"PASSED", "FAILED"} or name not in statuses:
            statuses[name] = status
        if status != "FAILED":
            continue
        trace = []
        for following in lines[index + 1:index + 41]:
            if STATUS_LINE_RE.match(following):
                break
            if following.strip():
                trace.append(following.strip())
        counterexamples[name] = tuple(trace) or (line.strip(),)
    return statuses, counterexamples


def parse_calls(output: str) -> int | None:
    found = CALLS_RE.findall(output)
    return int(found[-1]) if found else None


def action_outcomes(report_text: str, plan: HandlerPlan, table) -> tuple[ActionOutcome, ...]:
    """Per-handler outcomes from the revert report, decoded and mapped back to
    the entry point each handler forwards."""
    metrics: dict[str, dict] = {}
    if report_text:
        try:
            data = json.loads(report_text)
        except ValueError:
            data = {}
        for contract_metrics in (data.get("contractRevertMetrics") or {}).values():
            if not isinstance(contract_metrics, dict):
                continue
            for name, entry in (contract_metrics.get("functionRevertMetrics") or {}).items():
                if name and isinstance(entry, dict):
                    metrics[name] = entry
    outcomes = []
    for handler in plan.names:
        action = plan.action(handler)
        entry = metrics.get(handler)
        if entry is None:
            outcomes.append(
                ActionOutcome(action, 0, 0, 0, handler=handler) if report_text
                else ActionOutcome(action, handler=handler)
            )
            continue
        calls = int(entry.get("totalCalls") or 0)
        reverts = int(entry.get("totalReverts") or 0)
        reasons = sorted(
            (
                (table.decode(str(reason)), int((detail or {}).get("count") or 0))
                for reason, detail in (entry.get("revertReasonMetrics") or {}).items()
            ),
            key=lambda pair: -pair[1],
        )
        outcomes.append(ActionOutcome(
            action, calls, max(calls - reverts, 0), reverts, tuple(reasons), handler=handler,
        ))
    return tuple(outcomes)


def generate(project, contracts, protocol_invariants=()) -> list[BackendExecution]:
    """Produce artifacts without running the tool; pre-flight still reports."""
    results: list[BackendExecution] = []
    planned = _plan(contracts, protocol_invariants, results)
    out: list[BackendExecution] = []
    if not planned:
        return out
    report = _preflight(project, planned, contracts)
    for item in planned:
        lines = tuple(issue.line() for issue in report.for_contract(item.contract.name))
        out.append(BackendExecution(
            NAME, GENERATED, item.contract.name,
            tuple(prop.name for prop in item.properties),
            dict(item.artifacts), unsupported=item.unsupported,
            reason="; ".join(issue.message for issue in report.blocking(item.contract.name)),
            preflight=lines,
        ))
    return out
