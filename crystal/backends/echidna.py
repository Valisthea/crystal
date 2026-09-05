"""Echidna property-testing backend.

Echidna was not installed on the machine this was written on, so the output
shapes below are from its documentation rather than a measured run:

* property lines   `property_x: passing` / `property_x: failed!💥` followed by
                   an indented `Call sequence:` block;
* status lines     `[status] tests: 0/2, fuzzing: 1234/50000, ...` (calls);
* coverage file    `<corpusDir>/covered.<timestamp>.txt`, one section per
                   source file, each line prefixed with markers: `*` executed,
                   `r` reverted, `o` out of gas, `e` error.

Echidna publishes no per-function revert counts, so the witness here is
coverage-shaped: each forwarding handler ends with a line that only executes
when the forwarded call succeeded, and that line's `*` marker is the evidence.
Counts stay None rather than being estimated. Until a measured run confirms
these formats, a green Echidna run decodes to VACUOUS, never to HELD.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from .. import process
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
from .harness import WITNESS_COUNTER, HandlerPlan, fuzz_handlers
from .medusa import harness as solidity_harness
from .verdict import HELD, VIOLATED, ActionOutcome, ExecutionWitness, decide, summarise

NAME = "echidna"
HARNESS_CONTRACT = "CrystalEchidnaHarness"
HARNESS_FILE = f"{HARNESS_CONTRACT}.sol"
CONFIG_FILE = "echidna.yaml"

TRAITS = BackendTraits(
    NAME, advances_block=True, funds_senders=True, fuzzes_nested_deployments=False,
    note="advances blocks (maxBlockDelay); actors funded by balanceAddr; only "
         "calls the contract under test unless allContracts is set",
)

STATUS_RE = re.compile(r"^\s*(property_\w+):\s*(passing|passed|failed|falsified)", re.I)
CALLS_RE = re.compile(r"fuzzing:\s*(\d+)\s*/\s*\d+")
COVER_LINE_RE = re.compile(r"^\s*(?:\d+\s*\|)?\s*([*rioe ]*?)\s*\|\s?(.*)$")
HANDLER_RE = re.compile(r"function\s+(h_\w+)\s*\(")


def detect():
    return probe("echidna", "--version")


def config(test_limit: int = 50000) -> str:
    return "\n".join([
        f"testLimit: {test_limit}",
        "testMode: property",
        'prefix: "property_"',
        "corpusDir: corpus",
        "shrinkLimit: 5000",
        "coverage: true",
        "",
    ])


def _harness(contract, properties, plan=None, contracts=()) -> str:
    source = solidity_harness(contract, properties, plan, contracts)
    return source.replace("CrystalMedusaHarness", HARNESS_CONTRACT) if source else ""


def _plan(contracts, protocol_invariants, results: list) -> list[tuple]:
    planned = []
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
        source = _harness(contract, properties, plan, contracts)
        if not source:
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported),
                "constructor arguments could not be derived from the signature",
            ))
            continue
        artifacts = {HARNESS_FILE: source, CONFIG_FILE: config()}
        planned.append((contract, tuple(properties), tuple(unsupported) + plan.skipped, plan, artifacts))
    return planned


def _preflight(project, planned, contracts):
    return preflight.run(
        project, [item[0] for item in planned], NAME, TRAITS,
        {item[0].name: item[1] for item in planned},
        {item[0].name: item[4][HARNESS_FILE] for item in planned},
        {item[0].name: item[4][CONFIG_FILE] for item in planned},
        all_contracts=contracts,
    )


def run(project, contracts, protocol_invariants=(), timeout=300) -> list[BackendExecution]:
    results: list[BackendExecution] = []
    planned = _plan(contracts, protocol_invariants, results)
    if not planned:
        return results
    report = _preflight(project, planned, contracts)
    capabilities = detect()

    for contract, properties, unsupported, plan, artifacts in planned:
        names = tuple(prop.name for prop in properties)
        lines = tuple(issue.line() for issue in report.for_contract(contract.name))
        blocking = report.blocking(contract.name)
        if blocking:
            results.append(BackendExecution(
                NAME, REFUSED, contract.name, names, artifacts, None, "", "", (),
                unsupported,
                "pre-flight refused to launch: " + " | ".join(issue.message for issue in blocking),
                False, (), lines,
            ))
            continue
        if not capabilities.available:
            results.append(BackendExecution(
                NAME, UNAVAILABLE, contract.name, names, artifacts, None, "",
                capabilities.reason, (), unsupported, capabilities.reason,
                False, (), lines,
            ))
            continue

        with tempfile.TemporaryDirectory(prefix="crystal-echidna-") as directory:
            root = Path(directory)
            if contract.path and Path(contract.path).exists():
                shutil.copy2(contract.path, root / Path(contract.path).name)
            for filename, content in artifacts.items():
                (root / filename).write_text(content, encoding="utf-8")
            completed = process.run(
                ["echidna", HARNESS_FILE, "--contract", HARNESS_CONTRACT,
                 "--config", CONFIG_FILE, "--format", "text"],
                cwd=root, timeout=timeout,
            )
            if completed.error == "timeout":
                results.append(BackendExecution(
                    NAME, TIMEOUT, contract.name, names, artifacts, None,
                    completed.stdout, completed.stderr, (), unsupported,
                    "echidna timed out", False, (), lines,
                ))
                continue
            coverage_text = _coverage_text(root / "corpus")

        output = completed.output
        statuses, counterexamples = parse_properties(output)
        calls = parse_calls(output)
        outcomes = coverage_outcomes(coverage_text, plan)
        verdicts = []
        for prop in properties:
            subject = property_subject(prop, contract)
            writers = {
                handler for handler, function in plan.handlers.items()
                if transitive_writes(contract, function, contracts) & set(subject)
            }
            actions = tuple(
                replace(outcome, mutates_subject=outcome.handler in writers)
                for outcome in outcomes
            )
            notes = ("echidna reports no per-function call counts; evidence is coverage of the handler witness line",)
            if not coverage_text:
                notes += ("no coverage file was found under corpus/",)
            witness = ExecutionWitness(
                NAME, "corpus/covered.*.txt + stdout", subject,
                checked=statuses.get(prop.name) in {"passing", "passed", "failed", "falsified"},
                calls=calls, sequences=None, actions=actions, notes=notes,
            )
            verdicts.append(decide(
                prop.name, witness, counterexamples.get(prop.name, ()),
                report.undecidable(contract.name, prop.name),
            ))
        status = execution_status(verdicts, evaluated=bool(statuses))
        reason = summarise(verdicts) if statuses else (
            f"echidna exited {completed.returncode} before evaluating any property"
        )
        results.append(BackendExecution(
            NAME, status, contract.name, names, artifacts, completed.returncode,
            completed.stdout[-8000:], completed.stderr[-4000:],
            tuple(trace for traces in counterexamples.values() for trace in traces)[:20],
            unsupported, reason,
            any(item.verdict in {HELD, VIOLATED} for item in verdicts),
            tuple(verdicts), lines,
        ))
    return results


def _coverage_text(corpus: Path) -> str:
    if not corpus.is_dir():
        return ""
    files = sorted(corpus.glob("covered.*.txt"))
    if not files:
        return ""
    try:
        return files[-1].read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""


def parse_properties(output: str) -> tuple[dict[str, str], dict[str, tuple[str, ...]]]:
    statuses: dict[str, str] = {}
    counterexamples: dict[str, tuple[str, ...]] = {}
    lines = output.splitlines()
    for index, line in enumerate(lines):
        match = STATUS_RE.match(line)
        if not match:
            continue
        name, status = match.group(1), match.group(2).lower()
        statuses[name] = status
        if status not in {"failed", "falsified"}:
            continue
        trace = []
        for following in lines[index + 1:index + 41]:
            if not following.strip() or STATUS_RE.match(following):
                break
            trace.append(following.strip())
        counterexamples[name] = tuple(trace) or (line.strip(),)
    return statuses, counterexamples


def parse_calls(output: str) -> int | None:
    found = CALLS_RE.findall(output)
    return int(found[-1]) if found else None


def coverage_outcomes(coverage_text: str, plan: HandlerPlan) -> tuple[ActionOutcome, ...]:
    """Whether each handler's witness line was ever executed without reverting."""
    executed: dict[str, bool] = {}
    current = None
    for raw in coverage_text.splitlines():
        match = COVER_LINE_RE.match(raw)
        if not match:
            continue
        markers, source = match.group(1) or "", match.group(2)
        handler = HANDLER_RE.search(source)
        if handler:
            current = handler.group(1)
            continue
        if current and f"{WITNESS_COUNTER} += 1" in source:
            executed[current] = "*" in markers
            current = None
    return tuple(
        ActionOutcome(
            plan.action(handler), handler=handler,
            executed_mutation=executed.get(handler) if coverage_text else None,
        )
        for handler in plan.names
    )


def generate(project, contracts, protocol_invariants=()) -> list[BackendExecution]:
    results: list[BackendExecution] = []
    planned = _plan(contracts, protocol_invariants, results)
    out: list[BackendExecution] = []
    if not planned:
        return out
    report = _preflight(project, planned, contracts)
    for contract, properties, unsupported, plan, artifacts in planned:
        lines = tuple(issue.line() for issue in report.for_contract(contract.name))
        out.append(BackendExecution(
            NAME, GENERATED, contract.name,
            tuple(prop.name for prop in properties), dict(artifacts),
            unsupported=unsupported,
            reason="; ".join(issue.message for issue in report.blocking(contract.name)),
            preflight=lines,
        ))
    return out
