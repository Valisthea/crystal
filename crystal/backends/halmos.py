"""Halmos symbolic-verification backend.

Halmos proves a property for *all* inputs of the declared types, so the
generated tests take symbolic arguments derived from the parsed signature
instead of concrete boundary values.

Two things measured on the installed Halmos on 2026-09-05 shape the witness:

* a test whose every path reverts is reported `[ERROR]` with the warning
  "all paths have been reverted", and `paths:` counts reverting paths too. So
  the evidence that a transition reached the assertion is the `[PASS]` line
  itself, not the path count;
* `block.number` is pinned to 1 and the harness never rolls it. Any entry
  point whose reachability depends on a block or timestamp delta is excluded
  from the harness and the property behind it is reported UNSUPPORTED on this
  backend, instead of returning a PASS on an unreachable branch.

Monotonicity is checked as a single-transition snapshot (`before` vs `after`)
rather than against a ghost the harness could not declare.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from .. import process
from ..research.foundry import argument_literal
from . import preflight
from .actions import entry_points, mutating_actions, needs_value, property_subject
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
from .verdict import HELD, VIOLATED, ActionOutcome, ExecutionWitness, decide, summarise

NAME = "halmos"
HARNESS_CONTRACT = "CrystalHalmosTest"
HARNESS_FILE = f"test/{HARNESS_CONTRACT}.t.sol"
PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")

TRAITS = BackendTraits(
    NAME, advances_block=False, funds_senders=False, fuzzes_nested_deployments=False,
    note="pins block.number to 1 (measured); caller has no balance unless the "
         "harness deals it; tests are explicit, nothing is fuzzed; cannot "
         "execute the hashing precompiles",
    kind="symbolic",
    # Measured on halmos 0.3.3: a settlement path reaching `sha256` ran 26
    # minutes and produced no verdict. Not a crash and not a PASS — it does not
    # finish. Naming it turns an unbounded wait into an UNSUPPORTED, which an
    # operator can act on.
    unmodelled_precompiles=("sha256", "ripemd160", "ecrecover", "modexp"),
)

SYMBOLIC_TYPES = {
    "uint256", "uint128", "uint64", "uint32", "uint16", "uint8",
    "int256", "address", "bool", "bytes32",
}

RESULT_RE = re.compile(r"\[(PASS|FAIL|ERROR|TIMEOUT)\]\s+(check_\w+)\(([^)]*)\)\s*\(paths:\s*(\d+)")
ALL_REVERTED_RE = re.compile(r"(check_\w+)\([^)]*\):\s*all\s+paths\s+have\s+been\s+reverted", re.S)
COUNTEREXAMPLE_RE = re.compile(r"^\s*Counterexample", re.I)
ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")

CHEATCODE_INTERFACE = [
    "interface CrystalVm {",
    "    function deal(address account, uint256 newBalance) external;",
    "}",
    "",
]


def detect():
    return probe("halmos", "--version")


def _symbolic_parameters(function) -> list[str] | None:
    parameters = []
    for parameter in function.params:
        cleaned = re.sub(r"\s+", "", parameter.type_name or "")
        cleaned = re.sub(r"\b(memory|calldata|storage|payable)\b", "", cleaned)
        if cleaned not in SYMBOLIC_TYPES:
            return None
        parameters.append(f"{cleaned} {parameter.name or 'arg'}")
    return parameters


def _assertion(prop) -> tuple[list[str], str]:
    """Pre-call lines and the assertion for one property in one transition."""
    if getattr(prop, "kind", "") == "monotonicity" and getattr(prop, "subject", ""):
        return (
            [f"        uint256 before = target.{prop.subject}();"],
            f"target.{prop.subject}() >= before",
        )
    return [], prop.expression


def harness(contract, properties, excluded=None, contracts=()) -> tuple[str, dict]:
    """The test source and `test name -> (property name, entry point)`.

    `excluded` maps a property name to entry points the pre-flight found
    undecidable on this backend; no test is generated for those pairs.
    """
    excluded = excluded or {}
    constructor = next(
        (f for f in contract.functions if f.kind == "constructor"), None
    )
    constructor_arguments = []
    if constructor is not None:
        for index, parameter in enumerate(constructor.params):
            literal = argument_literal(parameter.type_name, index)
            if literal is None:
                return "", {}
            constructor_arguments.append(literal)

    pragma = "^0.8.20"
    if contract.path:
        try:
            match = PRAGMA_RE.search(
                Path(contract.path).read_text(encoding="utf-8", errors="ignore")
            )
            pragma = match.group(1).strip() if match else pragma
        except OSError:
            pass

    candidates = [
        function for function in entry_points(contract)
        if _symbolic_parameters(function) is not None
    ][:6]
    uses_value = any(needs_value(contract, function, contracts) for function in candidates)

    body: list[str] = []
    generated: dict[str, tuple[str, object]] = {}
    for prop in properties:
        dropped = set(excluded.get(prop.name, ()))
        pre_lines, assertion = _assertion(prop)
        for function in candidates:
            if f"{contract.name}.{function.name}" in dropped:
                continue
            parameters = _symbolic_parameters(function)
            test_name = f"check_{prop.name}_after_{function.name}"
            if test_name in generated:
                test_name = f"{test_name}_{len(generated)}"
            call_arguments = ", ".join(parameter.split()[-1] for parameter in parameters)
            payable = needs_value(contract, function, contracts)
            signature = list(parameters) + (["uint256 crystalValue"] if payable else [])
            body += [
                f"    // {prop.comment} (derived from {prop.origin})",
                f"    function {test_name}({', '.join(signature)}) public {{",
            ]
            body += pre_lines
            if payable:
                body += [
                    "        vm.deal(address(this), crystalValue);",
                    f"        target.{function.name}{{value: crystalValue}}({call_arguments});",
                ]
            else:
                body.append(f"        target.{function.name}({call_arguments});")
            body += [f"        assert({assertion});", "    }", ""]
            generated[test_name] = (prop.name, function)
    if not generated:
        return "", {}

    lines = [
        "// SPDX-License-Identifier: UNLICENSED",
        f"pragma solidity {pragma};",
        "",
        f'import "./{Path(contract.path).name}";' if contract.path else "",
        "",
    ]
    if uses_value:
        lines += CHEATCODE_INTERFACE
    lines += [
        f"contract {HARNESS_CONTRACT} {{",
        f"    {contract.name} internal target;",
    ]
    if uses_value:
        lines.append(
            '    CrystalVm internal constant vm = '
            'CrystalVm(address(uint160(uint256(keccak256("hevm cheat code")))));'
        )
    lines += [
        "",
        "    function setUp() public {",
        f"        target = new {contract.name}({', '.join(constructor_arguments)});",
        "    }",
        "",
    ]
    lines += body
    lines.append("}")
    return "\n".join(line for line in lines if line is not None), generated


def _plan(project, contracts, protocol_invariants, results: list):
    """Properties, block-exclusions, harness and artifacts per contract, plus
    the pre-flight report — the block check runs before the harness exists
    because it decides what the harness may contain."""
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
        block = preflight.block_issues(NAME, TRAITS, contract, properties, contracts)
        excluded: dict[str, tuple[str, ...]] = {}
        for issue in block:
            if issue.severity in {preflight.WARN, preflight.UNSUPPORTED}:
                for name in issue.properties:
                    excluded[name] = tuple(dict.fromkeys(excluded.get(name, ()) + issue.actions))
        source, tests = harness(contract, properties, excluded, contracts)
        if not source:
            reason = "no entry point had a fully symbolic-representable signature"
            if excluded and not any(
                f"{contract.name}.{function.name}" not in set().union(*excluded.values())
                for function in entry_points(contract)
            ):
                reason = "every entry point hides behind a block or timestamp delta halmos cannot advance past"
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported) + tuple(issue.message for issue in block),
                reason,
            ))
            continue
        artifacts = {
            HARNESS_FILE: source,
            "foundry.toml": "[profile.default]\nsrc = 'src'\ntest = 'test'\n",
        }
        planned.append((contract, tuple(properties), tuple(unsupported), tests, artifacts))
    if not planned:
        return [], None
    report = preflight.run(
        project, [item[0] for item in planned], NAME, TRAITS,
        {item[0].name: item[1] for item in planned},
        {item[0].name: item[4][HARNESS_FILE] for item in planned},
        all_contracts=contracts,
    )
    return planned, report


def run(project, contracts, protocol_invariants=(), timeout=600) -> list[BackendExecution]:
    results: list[BackendExecution] = []
    planned, report = _plan(project, contracts, protocol_invariants, results)
    if not planned:
        return results
    capabilities = detect()

    for contract, properties, unsupported, tests, artifacts in planned:
        names = tuple(tests)
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

        with tempfile.TemporaryDirectory(prefix="crystal-halmos-") as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / "test").mkdir()
            if contract.path and Path(contract.path).exists():
                shutil.copy2(contract.path, root / "src" / Path(contract.path).name)
            for filename, content in artifacts.items():
                target_path = root / filename
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_text(
                    content.replace(
                        f'import "./{Path(contract.path).name}";',
                        f'import "../src/{Path(contract.path).name}";',
                    ) if contract.path else content,
                    encoding="utf-8",
                )
            completed = process.run(
                ["halmos", "--contract", HARNESS_CONTRACT], cwd=root, timeout=timeout
            )
        if completed.error == "timeout":
            results.append(BackendExecution(
                NAME, TIMEOUT, contract.name, names, artifacts, None,
                completed.stdout, completed.stderr, (), unsupported,
                "halmos timed out", False, (), lines,
            ))
            continue

        output = ANSI_RE.sub("", completed.output)
        outcomes, counterexamples = parse_results(output)
        verdicts = []
        for prop in properties:
            subject = property_subject(prop, contract)
            writers = {
                f"{contract.name}.{function.name}"
                for function in mutating_actions(contract, subject, contracts)
            }
            actions = []
            traces: list[str] = []
            checked = False
            for test_name, (owner, function) in tests.items():
                if owner != prop.name:
                    continue
                result = outcomes.get(test_name)
                action = f"{contract.name}.{function.name}"
                if result is None:
                    actions.append(ActionOutcome(action, mutates_subject=action in writers, handler=test_name))
                    continue
                checked = True
                status, paths, all_reverted = result
                actions.append(ActionOutcome(
                    action, mutates_subject=action in writers, handler=test_name,
                    executed_mutation=(status in {"PASS", "FAIL"}) and not all_reverted,
                    revert_reasons=(("all paths reverted", paths),) if all_reverted else (),
                ))
                if status == "FAIL":
                    traces.extend(counterexamples.get(test_name, (f"[FAIL] {test_name}",)))
            witness = ExecutionWitness(
                NAME, "halmos stdout ([PASS]/[FAIL]/[ERROR] per test)", subject,
                checked=checked, calls=None, sequences=None, actions=tuple(actions),
                revert_selectors=("all paths reverted",) if any(
                    action.revert_reasons for action in actions
                ) else (),
                notes=("symbolic: evidence is a [PASS] line whose paths were not all reverted; "
                       "paths explored are reported per test, not call counts",),
            )
            verdicts.append(decide(
                prop.name, witness, tuple(traces),
                report.undecidable(contract.name, prop.name),
            ))
        status = execution_status(verdicts, evaluated=bool(outcomes))
        reason = summarise(verdicts) if outcomes else (
            f"halmos exited {completed.returncode} before evaluating any test: "
            f"{_last_error(output)}"
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


def _last_error(output: str) -> str:
    for line in reversed(output.splitlines()):
        if line.strip():
            return line.strip()[:300]
    return "no output"


def parse_results(output: str) -> tuple[dict[str, tuple[str, int, bool]], dict[str, tuple[str, ...]]]:
    """`test -> (status, paths, all_reverted)` and the counterexample block
    printed before each `[FAIL]` line."""
    reverted = set(ALL_REVERTED_RE.findall(output))
    outcomes: dict[str, tuple[str, int, bool]] = {}
    counterexamples: dict[str, tuple[str, ...]] = {}
    pending: list[str] = []
    collecting = False
    for line in output.splitlines():
        match = RESULT_RE.search(line)
        if match:
            status, name, paths = match.group(1), match.group(2), int(match.group(4))
            outcomes[name] = (status, paths, name in reverted)
            if status == "FAIL":
                counterexamples[name] = tuple(pending) or (line.strip(),)
            pending, collecting = [], False
            continue
        if COUNTEREXAMPLE_RE.match(line):
            collecting = True
            pending = [line.strip()]
            continue
        if collecting and line.strip():
            pending.append(line.strip())
    return outcomes, counterexamples


def generate(project, contracts, protocol_invariants=()) -> list[BackendExecution]:
    results: list[BackendExecution] = []
    planned, report = _plan(project, contracts, protocol_invariants, results)
    out: list[BackendExecution] = []
    for contract, properties, unsupported, tests, artifacts in planned:
        lines = tuple(issue.line() for issue in report.for_contract(contract.name))
        out.append(BackendExecution(
            NAME, GENERATED, contract.name, tuple(tests),
            {HARNESS_FILE: artifacts[HARNESS_FILE]}, unsupported=unsupported,
            reason="; ".join(issue.message for issue in report.blocking(contract.name)),
            preflight=lines,
        ))
    return out
