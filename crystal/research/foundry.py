"""Foundry execution backend.

v1 refused every function that took an argument, which meant almost everything
on real code returned UNSUPPORTED. v2 generates typed boundary arguments from
the parsed signature, deploys constructors whose arguments it can derive, and
supports multi-contract sequences.

The zero-fabrication rule is unchanged: when a constructor argument, an ABI
type or a source file cannot be derived from the target, Crystal returns
UNSUPPORTED with the precise reason instead of guessing.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from .. import process
from ..paths import rglob_files

ATTACKER = "address(0xA11CE)"
VICTIM = "address(0xB0B)"

PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")


@dataclass(frozen=True)
class FoundryCapabilities:
    forge: bool
    anvil: bool
    version: str | None
    reason: str


@dataclass(frozen=True)
class FoundryExecution:
    hypothesis: list[str]
    status: str
    backend: str
    returncode: int | None
    stdout: str
    stderr: str
    trace: str | None
    proof_evidence: tuple[str, ...]
    reproducible: bool
    harness: str | None = None
    arguments: tuple[str, ...] = ()
    unsupported_reason: str | None = None


@dataclass
class _Plan:
    contracts: list[str] = field(default_factory=list)
    sources: dict[str, Path] = field(default_factory=dict)
    calls: list[tuple[str, str, list[str]]] = field(default_factory=list)
    constructors: dict[str, list[str]] = field(default_factory=dict)
    pragma: str = "^0.8.20"


def detect_foundry():
    forge = shutil.which("forge")
    anvil = shutil.which("anvil")
    if not forge:
        return FoundryCapabilities(False, bool(anvil), None,
                                   "forge executable not installed")
    probe = process.run([forge, "--version"], timeout=15)
    if not probe.ok or probe.returncode != 0:
        return FoundryCapabilities(True, bool(anvil), None,
                                   f"forge probe failed: {probe.error or probe.stderr[:200]}")
    return FoundryCapabilities(True, bool(anvil), probe.first_line() or None, "ok")


def _contract_map(contracts):
    return {f"{c.name}.{f.name}": (c, f) for c in contracts for f in c.functions}


def argument_literal(type_name: str, index: int) -> str | None:
    """A concrete literal for a Solidity type, or None when undecidable."""
    cleaned = re.sub(r"\s+", " ", (type_name or "")).strip()
    cleaned = re.sub(r"\b(memory|calldata|storage|payable)\b", "", cleaned).strip()
    if not cleaned:
        return None
    if cleaned.endswith("[]"):
        inner = cleaned[:-2].strip()
        element = argument_literal(inner, index)
        return f"new {inner}[](0)" if element is not None else None
    if re.fullmatch(r"u?int\d*", cleaned):
        return ("1", "0", "type(uint256).max")[index % 3] \
            if cleaned.startswith("uint") else ("1", "0", "-1")[index % 3]
    if cleaned == "address":
        return (ATTACKER, VICTIM, "address(0)")[index % 3]
    if cleaned == "bool":
        return ("true", "false")[index % 2]
    if cleaned == "string":
        return '""'
    if cleaned == "bytes":
        return '""'
    if re.fullmatch(r"bytes\d+", cleaned):
        return f"{cleaned}(0)"
    return None


def _pragma_of(path: Path) -> str:
    try:
        match = PRAGMA_RE.search(path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return "^0.8.20"
    return match.group(1).strip() if match else "^0.8.20"


def _locate_source(project, contract) -> Path | None:
    if contract.path:
        candidate = Path(contract.path)
        if candidate.exists():
            return candidate
    root = Path(project).resolve()
    direct = root / f"{contract.name}.sol"
    if direct.exists():
        return direct
    for found in rglob_files(root, f"{contract.name}.sol"):
        return found
    return None


def build_plan(project, contracts, hypothesis) -> tuple[_Plan | None, str]:
    functions = _contract_map(contracts)
    plan = _Plan()

    for qualified in hypothesis:
        entry = functions.get(qualified)
        if entry is None:
            return None, f"{qualified} is not a parsed function"
        contract, function = entry
        if function.visibility not in {"public", "external"}:
            return None, f"{qualified} is not externally reachable"
        if function.kind in {"constructor", "modifier"}:
            return None, f"{qualified} is not a callable entry point"

        arguments: list[str] = []
        for index, parameter in enumerate(function.params):
            literal = argument_literal(parameter.type_name, index)
            if literal is None:
                return None, (
                    f"cannot derive a value for parameter "
                    f"`{parameter.type_name} {parameter.name}` of {qualified}"
                )
            arguments.append(literal)

        if contract.name not in plan.sources:
            source = _locate_source(project, contract)
            if source is None:
                return None, f"source file for {contract.name} not found"
            plan.sources[contract.name] = source
            plan.contracts.append(contract.name)
            plan.pragma = _pragma_of(source)

            constructor = next(
                (f for f in contract.functions if f.kind == "constructor"), None
            )
            constructor_arguments: list[str] = []
            if constructor is not None:
                for index, parameter in enumerate(constructor.params):
                    literal = argument_literal(parameter.type_name, index)
                    if literal is None:
                        return None, (
                            f"cannot derive constructor argument "
                            f"`{parameter.type_name} {parameter.name}` for "
                            f"{contract.name}"
                        )
                    constructor_arguments.append(literal)
            plan.constructors[contract.name] = constructor_arguments

        plan.calls.append((contract.name, function.name, arguments))
    return plan, "ok"


def render_harness(plan: _Plan) -> str:
    lines = [
        "// SPDX-License-Identifier: UNLICENSED",
        f"pragma solidity {plan.pragma};",
        "",
    ]
    lines += [f'import "../src/{name}.sol";' for name in plan.contracts]
    lines += [
        "",
        "contract CrystalFoundryHarness {",
        "    event CRYSTAL_STEP(uint256 index, string label);",
    ]
    lines += [
        f"    {name} internal target_{name};" for name in plan.contracts
    ]
    lines += [
        "",
        "    function testCrystalSequence() public {",
    ]
    for name in plan.contracts:
        arguments = ", ".join(plan.constructors.get(name, []))
        lines.append(f"        target_{name} = new {name}({arguments});")
    lines.append('        emit CRYSTAL_STEP(0, "setup");')
    for index, (contract_name, function_name, arguments) in enumerate(plan.calls, 1):
        joined = ", ".join(arguments)
        lines.append(f"        target_{contract_name}.{function_name}({joined});")
        lines.append(
            f'        emit CRYSTAL_STEP({index}, "{contract_name}.{function_name}");'
        )
    lines += ["    }", "}", ""]
    return "\n".join(lines)


def plan_hypothesis(project, contracts, hypothesis):
    """Generate the harness without running it.

    Used once the real-EVM budget is exhausted: the reviewer still receives a
    runnable proof-of-concept scaffold, and Crystal still refuses to guess.
    """
    plan, reason = build_plan(project, contracts, hypothesis)
    if plan is None:
        return FoundryExecution(
            list(hypothesis), "UNSUPPORTED", "foundry", None, "",
            "Crystal refuses to fabricate ABI arguments or constructor inputs.",
            None, (), False, None, (), reason,
        )
    return FoundryExecution(
        list(hypothesis), "HARNESS_ONLY", "foundry", None, "", "", None, (),
        False, render_harness(plan),
        tuple(f"{c}.{f}({', '.join(v)})" for c, f, v in plan.calls),
        "real-EVM execution budget exhausted; harness generated for replay",
    )


def execute_hypothesis(project, contracts, hypothesis, timeout=120):
    capabilities = detect_foundry()
    plan, reason = build_plan(project, contracts, hypothesis)

    if plan is None:
        return FoundryExecution(
            list(hypothesis), "UNSUPPORTED", "foundry", None, "",
            "Crystal refuses to fabricate ABI arguments or constructor inputs.",
            None, (), False, None, (), reason,
        )

    harness = render_harness(plan)
    arguments = tuple(
        f"{contract}.{function}({', '.join(values)})"
        for contract, function, values in plan.calls
    )
    if not capabilities.forge:
        return FoundryExecution(
            list(hypothesis), "UNAVAILABLE", "foundry", None, "",
            capabilities.reason, None, (), False, harness, arguments,
            capabilities.reason,
        )

    with tempfile.TemporaryDirectory(prefix="crystal-foundry-") as directory:
        root = Path(directory)
        (root / "src").mkdir()
        (root / "test").mkdir()
        for name, source in plan.sources.items():
            shutil.copy2(source, root / "src" / f"{name}.sol")
        (root / "test" / "CrystalHarness.t.sol").write_text(harness, encoding="utf-8")
        # `ffi` lets a test run arbitrary host commands, and this harness is
        # compiled from a contract Crystal did not write. Foundry defaults it
        # off; the file is ours, so the decision is stated rather than assumed.
        (root / "foundry.toml").write_text(
            "[profile.default]\nsrc = 'src'\ntest = 'test'\nout = 'out'\n"
            "ffi = false\n",
            encoding="utf-8",
        )
        completed = process.run(
            ["forge", "test", "--match-test", "testCrystalSequence", "-vvv"],
            cwd=root, timeout=timeout,
        )
        if completed.error == "timeout":
            return FoundryExecution(
                list(hypothesis), "TIMEOUT", "foundry", None,
                completed.stdout, completed.stderr,
                None, (), False, harness, arguments, "forge test timed out",
            )
        if not completed.ok:
            return FoundryExecution(
                list(hypothesis), "UNAVAILABLE", "foundry", None, "",
                completed.stderr, None, (), False, harness, arguments,
                completed.error,
            )

        output = completed.output
        evidence = []
        if completed.returncode == 0:
            evidence.append("forge-test-passed")
        if "CRYSTAL_STEP" in output:
            evidence.append("crystal-step-markers-observed")
        if "Compiler run failed" in output or "compilation failed" in output.lower():
            status = "UNSUPPORTED"
            reason = "generated harness did not compile against the target sources"
        else:
            status = "EXECUTED_PASS" if completed.returncode == 0 else "EXECUTED_REVERT"
            reason = None
        return FoundryExecution(
            list(hypothesis), status, "foundry", completed.returncode,
            completed.stdout, completed.stderr, output[-12000:] or None,
            tuple(evidence), completed.returncode == 0 and status != "UNSUPPORTED",
            harness, arguments, reason,
        )
