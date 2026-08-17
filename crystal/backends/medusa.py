"""Medusa property-fuzzing backend."""

from __future__ import annotations

import json
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
    ghost_declarations,
    probe,
)

NAME = "medusa"
PRAGMA_RE = re.compile(r"pragma\s+solidity\s+([^;]+);")


def detect():
    return probe("medusa", "--version")


def config(contract_name: str, test_limit: int = 50000) -> dict:
    return {
        "fuzzing": {
            "workers": 4,
            "testLimit": test_limit,
            "callSequenceLength": 100,
            "corpusDirectory": "corpus",
            "deploymentOrder": [contract_name],
            "targetContracts": [contract_name],
            "testing": {
                "assertionTesting": {"enabled": True},
                "propertyTesting": {"enabled": True, "testPrefixes": ["property_"]},
                "optimizationTesting": {"enabled": False},
            },
        },
        "compilation": {
            "platform": "crytic-compile",
            "platformConfig": {"target": "."},
        },
    }


def harness(contract, properties) -> str:
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

    lines = [
        "// SPDX-License-Identifier: UNLICENSED",
        f"pragma solidity {pragma};",
        "",
        f'import "./{Path(contract.path).name}";' if contract.path else "",
        "",
        f"contract CrystalMedusaHarness {{",
        f"    {contract.name} internal target;",
    ]
    lines += ghost_declarations(properties)
    lines += [
        "",
        "    constructor() {",
        f"        target = new {contract.name}({', '.join(arguments)});",
        "    }",
        "",
    ]
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


def run(project, contracts, protocol_invariants=(), timeout=300) -> list[BackendExecution]:
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

        source = harness(contract, properties)
        if not source:
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported),
                "constructor arguments could not be derived from the signature",
            ))
            continue

        artifacts = {
            "CrystalMedusaHarness.sol": source,
            "medusa.json": json.dumps(config("CrystalMedusaHarness"), indent=2),
        }
        names = tuple(prop.name for prop in properties)

        if not capabilities.available:
            results.append(BackendExecution(
                NAME, UNAVAILABLE, contract.name, names, artifacts, None, "",
                capabilities.reason, (), tuple(unsupported), capabilities.reason,
            ))
            continue

        with tempfile.TemporaryDirectory(prefix="crystal-medusa-") as directory:
            root = Path(directory)
            if contract.path and Path(contract.path).exists():
                shutil.copy2(contract.path, root / Path(contract.path).name)
            for filename, content in artifacts.items():
                (root / filename).write_text(content, encoding="utf-8")
            completed = process.run(
                ["medusa", "fuzz", "--config", "medusa.json"],
                cwd=root, timeout=timeout,
            )
            if completed.error == "timeout":
                results.append(BackendExecution(
                    NAME, TIMEOUT, contract.name, names, artifacts, None,
                    completed.stdout, completed.stderr, (), tuple(unsupported),
                    "medusa timed out",
                ))
                continue
            output = completed.output
            failures = tuple(
                line.strip() for line in output.splitlines()
                if "failed" in line.lower() or "violated" in line.lower()
            )[:20]
            results.append(BackendExecution(
                NAME, EXECUTED_PASS if completed.returncode == 0 else EXECUTED_FAIL,
                contract.name, names, artifacts, completed.returncode,
                completed.stdout[-8000:], completed.stderr[-4000:], failures,
                tuple(unsupported), "", completed.returncode == 0,
            ))
    return results


def generate(project, contracts, protocol_invariants=()) -> list[BackendExecution]:
    """Produce artifacts without running the tool."""
    out: list[BackendExecution] = []
    for contract in contracts:
        if contract.kind in {"interface", "library", "file"}:
            continue
        properties, unsupported = derive_properties(contract, protocol_invariants)
        if not properties:
            continue
        source = harness(contract, properties)
        if not source:
            continue
        out.append(BackendExecution(
            NAME, GENERATED, contract.name,
            tuple(prop.name for prop in properties),
            {
                "CrystalMedusaHarness.sol": source,
                "medusa.json": json.dumps(config("CrystalMedusaHarness"), indent=2),
            },
            unsupported=tuple(unsupported),
        ))
    return out
