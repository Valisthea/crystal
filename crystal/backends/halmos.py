"""Halmos symbolic-verification backend.

Halmos proves a property for *all* inputs of the declared types, so the
generated tests take symbolic arguments derived from the parsed signature
instead of concrete boundary values.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from pathlib import Path

from .. import process
from ..research.foundry import argument_literal
from .base import (
    EXECUTED_FAIL,
    EXECUTED_PASS,
    GENERATED,
    TIMEOUT,
    UNAVAILABLE,
    UNSUPPORTED,
    BackendExecution,
    derive_properties,
    probe,
)

NAME = "halmos"
HARNESS_CONTRACT = "CrystalHalmosTest"
PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")

SYMBOLIC_TYPES = {
    "uint256", "uint128", "uint64", "uint32", "uint16", "uint8",
    "int256", "address", "bool", "bytes32",
}


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


def harness(contract, properties) -> tuple[str, list[str]]:
    constructor = next(
        (f for f in contract.functions if f.kind == "constructor"), None
    )
    constructor_arguments = []
    if constructor is not None:
        for index, parameter in enumerate(constructor.params):
            literal = argument_literal(parameter.type_name, index)
            if literal is None:
                return "", []
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

    lines = [
        "// SPDX-License-Identifier: UNLICENSED",
        f"pragma solidity {pragma};",
        "",
        f'import "./{Path(contract.path).name}";' if contract.path else "",
        "",
        f"contract {HARNESS_CONTRACT} {{",
        f"    {contract.name} internal target;",
        "",
        "    function setUp() public {",
        f"        target = new {contract.name}({', '.join(constructor_arguments)});",
        "    }",
        "",
    ]

    generated: list[str] = []
    for prop in properties:
        entry_points = [
            function for function in contract.functions
            if function.is_entry_point and function.mutability not in {"view", "pure"}
        ]
        for function in entry_points[:6]:
            parameters = _symbolic_parameters(function)
            if parameters is None:
                continue
            test_name = f"check_{prop.name}_after_{function.name}"
            call_arguments = ", ".join(
                parameter.split()[-1] for parameter in parameters
            )
            lines += [
                f"    // {prop.comment} (derived from {prop.origin})",
                f"    function {test_name}({', '.join(parameters)}) public {{",
                f"        target.{function.name}({call_arguments});",
                f"        assert({prop.expression});",
                "    }",
                "",
            ]
            generated.append(test_name)
    lines.append("}")
    if not generated:
        return "", []
    return "\n".join(line for line in lines if line is not None), generated


def run(project, contracts, protocol_invariants=(), timeout=600) -> list[BackendExecution]:
    capabilities = detect()
    results: list[BackendExecution] = []

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

        source, tests = harness(contract, properties)
        if not source:
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported),
                "no entry point had a fully symbolic-representable signature",
            ))
            continue

        artifacts = {
            f"test/{HARNESS_CONTRACT}.t.sol": source,
            "foundry.toml": "[profile.default]\nsrc = 'src'\ntest = 'test'\n",
        }
        names = tuple(tests)

        if not capabilities.available:
            results.append(BackendExecution(
                NAME, UNAVAILABLE, contract.name, names, artifacts, None, "",
                capabilities.reason, (), tuple(unsupported), capabilities.reason,
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
                    completed.stdout, completed.stderr, (), tuple(unsupported),
                    "halmos timed out",
                ))
                continue
            output = completed.output
            failures = tuple(
                line.strip() for line in output.splitlines()
                if "counterexample" in line.lower() or line.strip().startswith("[FAIL]")
            )[:20]
            results.append(BackendExecution(
                NAME,
                EXECUTED_FAIL if failures else (
                    EXECUTED_PASS if completed.returncode == 0 else EXECUTED_FAIL
                ),
                contract.name, names, artifacts, completed.returncode,
                completed.stdout[-8000:], completed.stderr[-4000:], failures,
                tuple(unsupported), "", not failures and completed.returncode == 0,
            ))
    return results


def generate(project, contracts, protocol_invariants=()) -> list[BackendExecution]:
    out: list[BackendExecution] = []
    for contract in contracts:
        if contract.kind in {"interface", "library", "file"}:
            continue
        properties, unsupported = derive_properties(contract, protocol_invariants)
        if not properties:
            continue
        source, tests = harness(contract, properties)
        if not source:
            continue
        out.append(BackendExecution(
            NAME, GENERATED, contract.name, tuple(tests),
            {f"test/{HARNESS_CONTRACT}.t.sol": source},
            unsupported=tuple(unsupported),
        ))
    return out
