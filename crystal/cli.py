"""Crystal command line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from . import __build__, __release__, __version__, process
from .backends import backend_names, capabilities as backend_capabilities, run_backend
from .detectors import detector_names
from .discovery import discover, profile
from .engine import research
from .models import MOVE, RUST, SOLIDITY, VYPER
from .parsers import parser_report
from .report import WRITERS, markdown, payload

CAPABILITIES = [
    "known-weakness-baseline", "novel-behavior-engine",
    "unknown-interaction-discovery", "novelty-scoring",
    "state-delta-analysis", "differential-analysis",
    "causal-composition", "anti-finding-falsification",
    "zero-false-positive-confirmed-gate", "proof-checklist",
    "protocol-model", "compiler-ast-model", "validation-gate",
    "evidence-only-output", "foundry-backend", "zero-fabrication",
    # v2
    "tree-sitter-parsing", "multi-language-solidity-rust-move-vyper",
    "symbolic-execution-engine", "statement-ir", "structural-novelty",
    "reentrancy-ordering-detector", "access-control-detector",
    "first-depositor-detector", "oracle-manipulation-detector",
    "unbounded-input-detector", "test-fixture-classification",
    "transaction-decoded-input-taint", "pipeline-guard-bypass-detector",
    "ignored-outcome-detector", "cross-module-composition",
    "runtime-wiring-extraction",
    "medusa-backend", "echidna-backend", "halmos-backend",
    "sarif-output", "arcadia-output", "watch-mode", "environment-doctor",
]

LANGUAGES = {"solidity": SOLIDITY, "rust": RUST, "move": MOVE, "vyper": VYPER}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crystal",
        description="Independent protocol-oriented security research engine. "
                    "Produces evidence, never confirmed findings.",
    )
    parser.add_argument("--version", action="version",
                        version=f"crystal {__version__} build {__build__}")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="analyze a project")
    scan.add_argument("project")
    scan.add_argument("--format", choices=sorted(WRITERS), default="json")
    scan.add_argument("--output", "-o")
    scan.add_argument("--no-solc", action="store_true",
                      help="skip the solc AST pass")
    scan.add_argument("--no-treesitter", action="store_true",
                      help="force the regex fallback parsers")
    scan.add_argument("--lang", action="append", choices=sorted(LANGUAGES),
                      help="restrict discovery to a language (repeatable)")
    scan.add_argument("--detectors", help="comma-separated detector allowlist")
    scan.add_argument("--no-detectors", action="store_true")
    scan.add_argument("--no-foundry", action="store_true",
                      help="skip real-EVM execution (harnesses are still generated)")
    scan.add_argument("--include-tests", action="store_true",
                      help="research test fixtures too (excluded by default: a mock "
                           "runtime produces deltas and unguarded writes by design)")
    scan.add_argument("--quiet", "-q", action="store_true")

    sub.add_parser("capabilities", help="list engine capabilities")

    doctor = sub.add_parser("doctor", help="report environment readiness")
    doctor.add_argument("--json", action="store_true")

    watch = sub.add_parser("watch", help="re-scan when sources change")
    watch.add_argument("project")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--format", choices=sorted(WRITERS), default="markdown")
    watch.add_argument("--output", "-o")
    watch.add_argument("--no-solc", action="store_true")

    validate = sub.add_parser(
        "validate",
        help="run an execution backend on harnesses Crystal generates",
    )
    validate.add_argument("project")
    validate.add_argument("--backend", required=True,
                          choices=backend_names() + ["foundry"])
    validate.add_argument("--generate-only", action="store_true",
                          help="write the harness and config without running the tool")
    validate.add_argument("--out-dir", help="write generated artifacts here")
    validate.add_argument("--timeout", type=int, default=300)
    validate.add_argument("--no-solc", action="store_true")

    update = sub.add_parser("update", help="self-update a git checkout")
    update.add_argument("--check", action="store_true",
                        help="report the available update without applying it")
    return parser


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------

def _tool_version(executable: str, *args: str) -> tuple[bool, str]:
    path = shutil.which(executable)
    if not path:
        return False, "not installed"
    probe = process.run([path, *args], timeout=15)
    if not probe.ok:
        return True, f"probe failed: {probe.error}"
    return True, probe.first_line() or "installed"


def environment_report() -> dict:
    python_ok = sys.version_info >= (3, 10)
    tools = {
        name: dict(zip(("available", "version"), _tool_version(name, *args)))
        for name, args in (
            ("solc", ("--version",)),
            ("forge", ("--version",)),
            ("anvil", ("--version",)),
            ("medusa", ("--version",)),
            ("echidna", ("--version",)),
            ("halmos", ("--version",)),
            ("cargo", ("--version",)),
            ("git", ("--version",)),
        )
    }
    parsers = parser_report()
    blocking = []
    if not python_ok:
        blocking.append(f"Python {sys.version.split()[0]} < 3.10")
    return {
        "crystal": {"version": __version__, "build": __build__,
                    "release": __release__,
                    "location": str(Path(__file__).resolve().parent)},
        "python": {"version": sys.version.split()[0], "ok": python_ok,
                   "executable": sys.executable},
        "parsers": parsers,
        "tools": tools,
        "detectors": detector_names(),
        "backends": {
            name: {"available": item.available, "version": item.version,
                   "reason": item.reason}
            for name, item in backend_capabilities().items()
        },
        "blocking": blocking,
        "ready": not blocking,
    }


def _print_doctor(report: dict) -> None:
    mark = {True: "ok", False: "--"}
    print(f"crystal {report['crystal']['version']} build {report['crystal']['build']}")
    print(f"  location            {report['crystal']['location']}")
    print(f"[{mark[report['python']['ok']]}] python              "
          f"{report['python']['version']} ({report['python']['executable']})")
    print("")
    print("parsers")
    for language, info in report["parsers"].items():
        if language == "treesitter_enabled":
            continue
        available = info["treesitter_available"]
        print(f"  [{mark[bool(available)]}] {language:<10} backend="
              f"{info['backend']:<12} {info['status']}")
    if not report["parsers"]["treesitter_enabled"]:
        print("  note: tree-sitter disabled via CRYSTAL_NO_TREESITTER")
    print("")
    print("external tools")
    for name, info in report["tools"].items():
        print(f"  [{mark[info['available']]}] {name:<8} {info['version']}")
    print("")
    print("validation backends (opt-in via `crystal validate`)")
    for name, info in report["backends"].items():
        print(f"  [{mark[info['available']]}] {name:<8} "
              f"{info['version'] or info['reason']}")
    print("")
    print(f"detectors: {', '.join(report['detectors'])}")
    print("")
    if report["ready"]:
        print("environment ready. Missing optional tools only reduce coverage; "
              "Crystal degrades instead of failing.")
    else:
        for item in report["blocking"]:
            print(f"BLOCKING: {item}")


# ---------------------------------------------------------------------------
# scan / watch
# ---------------------------------------------------------------------------

def _run_scan(args) -> dict:
    if getattr(args, "no_treesitter", False):
        os.environ["CRYSTAL_NO_TREESITTER"] = "1"
    languages = [LANGUAGES[x] for x in (args.lang or [])] or None
    selected = None
    if getattr(args, "detectors", None):
        selected = [x.strip() for x in args.detectors.split(",") if x.strip()]
        unknown = sorted(set(selected) - set(detector_names()))
        if unknown:
            raise SystemExit(
                f"unknown detector(s): {', '.join(unknown)}. "
                f"Available: {', '.join(detector_names())}"
            )
    return research(
        args.project,
        use_solc=not args.no_solc,
        languages=languages,
        run_detector_pass=not getattr(args, "no_detectors", False),
        use_foundry=not getattr(args, "no_foundry", False),
        detectors=selected,
        include_tests=getattr(args, "include_tests", False),
    )


def _emit(result, output_format, output, quiet=False) -> None:
    data = payload(result)
    if output_format == "json" and not output:
        print(json.dumps(data, indent=2, default=list))
        return
    if output_format == "markdown" and not output:
        print(markdown(data, result))
        return
    default_names = {
        "json": "crystal-report.json",
        "markdown": "crystal-report.md",
        "sarif": "crystal-report.sarif",
        "arcadia": "crystal-arcadia.json",
    }
    destination = output or default_names[output_format]
    if output_format == "markdown":
        WRITERS[output_format](destination, data, result)
    else:
        WRITERS[output_format](destination, data)
    if not quiet:
        summary = data["summary"]
        print(f"Crystal {output_format} report written to {destination}")
        print(f"  contracts={summary['contracts']} "
              f"detector_signals={summary['detector_signals']} "
              f"state_deltas={summary['state_deltas']} "
              f"anomalies={summary['delta_anomalies']} "
              f"confirmed_findings={summary['confirmed_findings']}")


def _fingerprint(project, languages=None) -> str:
    digest = hashlib.sha256()
    for path in discover(project, languages=languages):
        try:
            stat = path.stat()
        except OSError:
            continue
        digest.update(str(path).encode())
        digest.update(str(stat.st_mtime_ns).encode())
        digest.update(str(stat.st_size).encode())
    return digest.hexdigest()


def _watch(args) -> int:
    print(f"crystal watch {Path(args.project).resolve()} "
          f"(interval {args.interval}s, Ctrl-C to stop)")
    previous = None
    try:
        while True:
            current = _fingerprint(args.project)
            if current != previous:
                previous = current
                started = time.monotonic()
                scan_args = argparse.Namespace(
                    project=args.project, no_solc=args.no_solc,
                    no_treesitter=False, lang=None, no_detectors=False,
                    no_foundry=True, detectors=None,
                )
                result = _run_scan(scan_args)
                _emit(result, args.format, args.output)
                print(f"  scan completed in {time.monotonic() - started:.2f}s")
            time.sleep(max(0.2, args.interval))
    except KeyboardInterrupt:
        print("\nwatch stopped")
    return 0


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------

def _validate(args) -> int:
    from .discovery import discover as discover_sources
    from .parsers import parse_project
    from .protocol.invariants import derive_protocol_invariants
    from .protocol.model import build_protocol_model
    from .research.foundry import build_plan, detect_foundry, render_harness
    from .semantics.inheritance import link_inheritance

    contracts = parse_project(discover_sources(args.project)).contracts
    link_inheritance(contracts)
    invariants = derive_protocol_invariants(build_protocol_model(contracts))

    if args.backend == "foundry":
        capabilities = detect_foundry()
        print(f"foundry: {'available' if capabilities.forge else capabilities.reason}")
        written = 0
        for contract in contracts:
            entry = next((f for f in contract.functions if f.is_entry_point), None)
            if entry is None:
                continue
            plan, reason = build_plan(
                args.project, contracts, [f"{contract.name}.{entry.name}"]
            )
            if plan is None:
                print(f"  [UNSUPPORTED] {contract.name}: {reason}")
                continue
            written += _write_artifacts(
                args.out_dir, contract.name,
                {"test/CrystalHarness.t.sol": render_harness(plan)},
            )
            print(f"  [GENERATED] {contract.name}: harness for {entry.name}")
        print(f"{written} artifact(s) written." if args.out_dir else
              "Pass --out-dir to persist the generated harnesses.")
        return 0

    results = run_backend(
        args.backend, args.project, contracts, invariants,
        generate_only=args.generate_only, timeout=args.timeout,
    )
    if not results:
        print(f"{args.backend}: nothing to run — no contract yielded a "
              f"derivable property.")
        return 0

    written = 0
    for result in results:
        print(f"[{result.status}] {args.backend} :: {result.contract}"
              + (f" — {result.reason}" if result.reason else ""))
        for name in result.properties:
            print(f"    property {name}")
        for item in result.unsupported:
            print(f"    unsupported: {item}")
        for item in result.counterexamples[:5]:
            print(f"    counterexample: {item}")
        written += _write_artifacts(args.out_dir, result.contract, result.artifacts)
    if args.out_dir:
        print(f"{written} artifact(s) written to {args.out_dir}")
    elif any(result.artifacts for result in results):
        print("Pass --out-dir to persist the generated harnesses and configs.")
    return 0


def _write_artifacts(out_dir, contract_name, artifacts) -> int:
    if not out_dir or not artifacts:
        return 0
    base = Path(out_dir) / contract_name
    count = 0
    for filename, content in artifacts.items():
        destination = base / filename
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(content, encoding="utf-8")
        count += 1
    return count


def _update(args) -> int:
    root = Path(__file__).resolve().parent.parent
    if not (root / ".git").exists():
        print(f"crystal is not installed from a git checkout ({root}).")
        print("Reinstall with: pip install --upgrade crystal-security-engine")
        return 1
    if not shutil.which("git"):
        print("git is not available on PATH.")
        return 1

    process.run(["git", "fetch", "--quiet"], cwd=root, timeout=120)
    behind = process.run(
        ["git", "rev-list", "--count", "HEAD..@{u}"], cwd=root, timeout=60
    )
    count = behind.stdout.strip() or "0"
    if not behind.ok or behind.returncode != 0:
        print(f"cannot compare with upstream: {behind.stderr.strip() or behind.error}")
        return 1
    if count == "0":
        print(f"crystal {__version__} build {__build__} is up to date.")
        return 0
    print(f"{count} commit(s) available upstream.")
    if args.check:
        return 0
    pull = process.run(["git", "pull", "--ff-only"], cwd=root, timeout=300)
    print(pull.stdout.strip() or pull.stderr.strip())
    if pull.returncode != 0:
        return pull.returncode or 1
    install = process.run(
        [sys.executable, "-m", "pip", "install", "-e", "."], cwd=root, timeout=600
    )
    if install.returncode != 0:
        print(install.stderr.strip()[-2000:])
        return install.returncode or 1
    print("crystal updated.")
    return 0


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "capabilities":
        print(f"crystal {__version__} build {__build__}")
        for capability in CAPABILITIES:
            print(f"- {capability}")
        return 0

    if args.command == "doctor":
        report = environment_report()
        if args.json:
            print(json.dumps(report, indent=2))
        else:
            _print_doctor(report)
        return 0 if report["ready"] else 1

    if args.command == "watch":
        return _watch(args)

    if args.command == "validate":
        return _validate(args)

    if args.command == "update":
        return _update(args)

    if args.command == "scan":
        target = Path(args.project)
        if not target.exists():
            print(f"target not found: {target}", file=sys.stderr)
            return 2
        if not args.quiet:
            found = profile(args.project)
            print(f"crystal {__version__} build {__build__} scanning {target.resolve()}",
                  file=sys.stderr)
            print(f"  languages={found.languages or 'none detected'} "
                  f"frameworks={found.frameworks or 'none'}", file=sys.stderr)
        result = _run_scan(args)
        _emit(result, args.format, args.output, args.quiet)
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
