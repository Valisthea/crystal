"""Echidna property-testing backend."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

from .. import process
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
from .medusa import harness as solidity_harness

NAME = "echidna"
HARNESS_CONTRACT = "CrystalEchidnaHarness"


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


def _harness(contract, properties) -> str:
    source = solidity_harness(contract, properties)
    return source.replace("CrystalMedusaHarness", HARNESS_CONTRACT) if source else ""


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

        source = _harness(contract, properties)
        if not source:
            results.append(BackendExecution(
                NAME, UNSUPPORTED, contract.name, (), {}, None, "", "", (),
                tuple(unsupported),
                "constructor arguments could not be derived from the signature",
            ))
            continue

        artifacts = {
            f"{HARNESS_CONTRACT}.sol": source,
            "echidna.yaml": config(),
        }
        names = tuple(prop.name for prop in properties)

        if not capabilities.available:
            results.append(BackendExecution(
                NAME, UNAVAILABLE, contract.name, names, artifacts, None, "",
                capabilities.reason, (), tuple(unsupported), capabilities.reason,
            ))
            continue

        with tempfile.TemporaryDirectory(prefix="crystal-echidna-") as directory:
            root = Path(directory)
            if contract.path and Path(contract.path).exists():
                shutil.copy2(contract.path, root / Path(contract.path).name)
            for filename, content in artifacts.items():
                (root / filename).write_text(content, encoding="utf-8")
            completed = process.run(
                ["echidna", f"{HARNESS_CONTRACT}.sol", "--contract",
                 HARNESS_CONTRACT, "--config", "echidna.yaml"],
                cwd=root, timeout=timeout,
            )
            if completed.error == "timeout":
                results.append(BackendExecution(
                    NAME, TIMEOUT, contract.name, names, artifacts, None,
                    completed.stdout, completed.stderr, (), tuple(unsupported),
                    "echidna timed out",
                ))
                continue
            output = completed.output
            failures = tuple(
                line.strip() for line in output.splitlines()
                if "falsified" in line.lower() or "failed" in line.lower()
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
        source = _harness(contract, properties)
        if not source:
            continue
        out.append(BackendExecution(
            NAME, GENERATED, contract.name,
            tuple(prop.name for prop in properties),
            {f"{HARNESS_CONTRACT}.sol": source, "echidna.yaml": config()},
            unsupported=tuple(unsupported),
        ))
    return out
