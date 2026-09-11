"""Crystal command line interface."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from importlib import metadata as importlib_metadata
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import url2pathname

from . import __build__, __release__, __version__, process
from .backends import backend_names, capabilities as backend_capabilities, run_backend
from .campaigns import discover_packs
from .isolation import OPT_IN_VARIABLE, isolation_report
from .detectors import detector_names
from .discovery import discover, profile
from .engine import research
from .models import GO, MOVE, RUST, SOLIDITY, VYPER
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
    "runtime-wiring-extraction", "config-trait-resolution",
    "pipeline-stage-classification", "boundary-crossing-detection",
    "workspace-topology",
    "medusa-backend", "echidna-backend", "halmos-backend",
    "sarif-output", "arcadia-output", "watch-mode", "environment-doctor",
    # v3
    "campaign-system", "protocol-invariant-packs",
    "order-sensitivity-engine", "boundary-engine",
    "asymmetric-side-effect-detector", "enriched-causal-graph",
    "campaign-cli", "ens-preset",
    "cross-contract-state-namespacing",
    # v3.1
    "guard-asymmetry-coupling-grade", "companion-asymmetry-detector",
    "type-aware-call-extraction", "control-transfer-resolution",
    "scaffolding-exclusion-by-path", "build-drift-detection",
    # v3.2
    "invariant-property-compilation", "execution-witness-gate",
    "backend-preflight-refusal", "go-frontend",
    "detector-language-scope",
]

LANGUAGES = {"solidity": SOLIDITY, "rust": RUST, "go": GO,
             "move": MOVE, "vyper": VYPER}


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
    scan.add_argument("--pack", action="append", metavar="PACK",
                      help="load a campaign pack by dotted module or .py file "
                           "path (repeatable)")
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
    validate.add_argument("--pack", action="append", metavar="PACK",
                          help="compile this campaign pack's invariants into "
                               "harnesses (dotted module or .py path, repeatable)")
    validate.add_argument("--fixture", metavar="SPEC",
                          help="deployment fixture, as `module:factory` or a "
                               ".py path; without one, properties needing a "
                               "deployment are UNSUPPORTED rather than guessed")
    validate.add_argument("--ignore-preflight", action="store_true",
                          help="launch the backend even when preflight found a "
                               "condition that makes a green result meaningless")

    campaign = sub.add_parser("campaign", help="manage campaign packs")
    campaign_sub = campaign.add_subparsers(dest="campaign_command", required=True)
    campaign_list = campaign_sub.add_parser(
        "list", help="list registered campaigns")
    campaign_list.add_argument("--pack",
                               help="load an extra pack by module path")
    campaign_run = campaign_sub.add_parser("run",
                                           help="run a campaign against a project")
    campaign_run.add_argument("campaign_id")
    campaign_run.add_argument("project")
    campaign_run.add_argument("--no-solc", action="store_true")
    campaign_run.add_argument("--no-foundry", action="store_true")
    campaign_run.add_argument("--include-tests", action="store_true")
    campaign_run.add_argument("--pack", help="load an extra pack by module path")

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


# Build drift. The package is pip-installed editable, and the install target
# has been observed drifting to extracted ZIP snapshots frozen several builds
# behind the repo. Scans then run stale code for hours and nothing says so.
# Doctor therefore reports where the running package comes from, whether that
# place is a git checkout, and whether the running build matches HEAD. Every
# fact below is observed; when git is missing or fails the field degrades to
# None with the reason recorded, and doctor never crashes over it.

_BUILD_PATTERN = re.compile(r"""^__build__\s*=\s*["']([^"']*)["']""", re.MULTILINE)
_DISTRIBUTION = "crystal-security-engine"
_GIT_TIMEOUT = 15


def _nearest_git_entry(path: Path) -> Path | None:
    """Closest directory at or above `path` holding a `.git` entry.

    A `.git` *file* counts: linked worktrees and `--separate-git-dir` checkouts
    keep a pointer file where a plain clone keeps a directory.
    """
    for candidate in (path, *path.parents):
        try:
            if (candidate / ".git").exists():
                return candidate
        except OSError:
            continue
    return None


def _declared_build(text: str) -> str | None:
    match = _BUILD_PATTERN.search(text)
    return match.group(1) if match else None


def _failure(result: process.ProcessResult) -> str:
    return result.first_line() or result.error or "no output"


def source_report(location: Path | None = None, build: str = __build__) -> dict:
    """Where the imported package lives and whether it drifted from HEAD.

    `kind` is `checkout` (inside a git working tree and tracked by it),
    `snapshot` (an extracted archive: no working tree, or dropped untracked
    inside somebody else's), or `unknown` when git could not answer; `reason`
    then says why. Only `process.run` touches git, so a missing or failing
    binary degrades to None fields instead of an exception.
    """
    location = location or Path(__file__).resolve().parent
    info = {
        "kind": "unknown",
        "git_root": None,
        "head": None,
        "dirty": None,
        "modified_files": None,
        "head_build": None,
        "build_matches": None,
        "reason": None,
    }
    reasons: list[str] = []

    git = shutil.which("git")
    if git is None:
        root = _nearest_git_entry(location)
        if root is None:
            info["kind"] = "snapshot"
            reasons.append("git is not installed; judged from the missing .git "
                           "entry above the package")
        else:
            info["kind"] = "checkout"
            info["git_root"] = str(root)
            reasons.append("git is not installed; HEAD, working-tree state and "
                           "build match cannot be read")
        info["reason"] = "; ".join(reasons)
        return info

    probe = process.run(
        [git, "rev-parse", "--is-inside-work-tree", "--show-toplevel", "--show-prefix"],
        cwd=location, timeout=_GIT_TIMEOUT,
    )
    if not probe.ok:
        info["reason"] = f"git rev-parse did not run: {probe.error}"
        return info
    if probe.returncode != 0 or not probe.stdout.startswith("true"):
        if _nearest_git_entry(location) is None:
            info["kind"] = "snapshot"
            info["reason"] = "no git working tree above the package"
        else:
            info["reason"] = f"git cannot read the working tree: {_failure(probe)}"
        return info
    lines = probe.stdout.split("\n")
    root = lines[1].strip() if len(lines) > 1 else ""
    prefix = lines[2].strip() if len(lines) > 2 else ""
    info["git_root"] = str(Path(root)) if root else None

    tracked = process.run(
        [git, "ls-files", "--error-unmatch", "--", "__init__.py"],
        cwd=location, timeout=_GIT_TIMEOUT,
    )
    if tracked.ok and tracked.returncode != 0:
        info["kind"] = "snapshot"
        info["reason"] = (f"inside the git working tree at {info['git_root']} but "
                          f"not tracked by it: an extracted snapshot dropped inside "
                          f"another repository")
        return info
    info["kind"] = "checkout"
    if not tracked.ok:
        reasons.append(f"tracking could not be verified: {tracked.error}")

    head = process.run([git, "rev-parse", "--short", "HEAD"],
                       cwd=location, timeout=_GIT_TIMEOUT)
    if head.ok and head.returncode == 0 and head.stdout.strip():
        info["head"] = head.stdout.strip()
    else:
        reasons.append(f"HEAD unreadable: {_failure(head)}")

    status = process.run([git, "status", "--porcelain", "--untracked-files=no"],
                         cwd=location, timeout=_GIT_TIMEOUT)
    if status.ok and status.returncode == 0:
        changed = [line for line in status.stdout.splitlines() if line.strip()]
        info["dirty"] = bool(changed)
        info["modified_files"] = len(changed)
    else:
        reasons.append(f"working-tree state unreadable: {_failure(status)}")

    if info["head"]:
        shown = process.run([git, "show", f"HEAD:{prefix}__init__.py"],
                            cwd=location, timeout=_GIT_TIMEOUT)
        if shown.ok and shown.returncode == 0:
            info["head_build"] = _declared_build(shown.stdout)
            if info["head_build"] is None:
                reasons.append("HEAD's __init__.py declares no __build__")
            else:
                info["build_matches"] = info["head_build"] == build
        else:
            reasons.append(f"HEAD's __init__.py unreadable: {_failure(shown)}")

    info["reason"] = "; ".join(reasons) or None
    return info


def install_report(location: Path | None = None) -> dict:
    """Where pip believes the package lives.

    An editable install records its target in `direct_url.json`. Reporting it
    beside the imported location exposes the case this run cannot see from the
    inside: `python -m crystal.cli` picked up the checkout from the working
    directory while the `crystal` command still runs a stale target.
    """
    location = location or Path(__file__).resolve().parent
    info = {
        "distribution": _DISTRIBUTION,
        "editable": None,
        "target": None,
        "target_build": None,
        "covers_location": None,
        "reason": None,
    }
    try:
        distribution = importlib_metadata.distribution(_DISTRIBUTION)
    except importlib_metadata.PackageNotFoundError:
        info["reason"] = ("not pip-installed: the package was imported from "
                          "sys.path (working directory or PYTHONPATH)")
        return info
    try:
        raw = distribution.read_text("direct_url.json")
    except OSError as exc:
        info["reason"] = f"direct_url.json unreadable: {exc}"
        return info
    if not raw:
        info["reason"] = ("no direct_url.json recorded: installed from an index, "
                          "or by a tool other than pip")
        return info
    try:
        data = json.loads(raw)
    except ValueError as exc:
        info["reason"] = f"direct_url.json unparseable: {exc}"
        return info
    info["editable"] = bool((data.get("dir_info") or {}).get("editable"))
    url = data.get("url") or ""
    if not url.startswith("file:"):
        info["reason"] = f"installed from {url or 'an unrecorded source'}"
        return info
    target = Path(url2pathname(urlparse(url).path))
    info["target"] = str(target)
    init = target / "crystal" / "__init__.py"
    try:
        if init.is_file():
            info["target_build"] = _declared_build(init.read_text(encoding="utf-8"))
        info["covers_location"] = location.resolve().is_relative_to(target.resolve())
    except OSError as exc:
        info["reason"] = f"target unreadable: {exc}"
    return info


def drift_warnings(crystal: dict) -> list[str]:
    """Human-readable drift warnings derived from `source` and `install`.

    Warnings only: doctor's exit code reflects readiness, never drift.
    """
    source, install = crystal["source"], crystal["install"]
    build, location = crystal["build"], crystal["location"]
    warnings = []
    if source["kind"] == "snapshot":
        warnings.append(
            f"build {build} at {location} is a snapshot, not a checkout: it "
            f"cannot self-update and will not track the repo. Reinstall from the "
            f"git checkout (pip install -e <checkout>) to run current code."
        )
    elif source["kind"] == "checkout" and source["build_matches"] is False:
        detail = " (uncommitted local edits)" if source["dirty"] else ""
        warnings.append(
            f"running build {build} but HEAD {source['head']} declares build "
            f"{source['head_build']}{detail}."
        )
    if install["editable"] and install["target"] and install["covers_location"] is False:
        target_build = install["target_build"] or "unknown"
        warnings.append(
            f"pip's editable install points at {install['target']} (build "
            f"{target_build}), not at this run's location: the `crystal` command "
            f"runs that copy, not this code."
        )
    return warnings


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
    location = Path(__file__).resolve().parent
    crystal = {
        "version": __version__, "build": __build__, "release": __release__,
        "location": str(location),
        "source": source_report(location),
        "install": install_report(location),
    }
    return {
        "crystal": crystal,
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
        "warnings": drift_warnings(crystal),
        "isolation": isolation_report(),
        "ready": not blocking,
    }


def _print_isolation(isolation: dict) -> None:
    """What a target's tooling can reach. The third line is the one that matters.

    An operator deciding whether to point Crystal at something genuinely
    hostile needs to know that the filesystem and the environment are confined
    and the process is not. Printing the first two and omitting the third would
    read as a stronger guarantee than Crystal offers.
    """
    environment = isolation["environment"]
    print("isolation (what an analysed target's tooling can reach)")
    print(f"  environment         allowlist — {len(environment['passed'])} passed, "
          f"{environment['withheld_count']} withheld")
    if environment["opted_in"]:
        print(f"  opted back in       {', '.join(environment['opted_in'])} "
              f"(via {OPT_IN_VARIABLE})")
    print("  filesystem          disposable working directory per execution")
    print("  process             NOT sandboxed — tools run as you, on this host")


def _print_source(crystal: dict) -> None:
    source, install = crystal["source"], crystal["install"]
    if source["kind"] == "checkout":
        print(f"  source              git checkout at {source['git_root']}")
        if source["head"]:
            if source["dirty"] is None:
                state = "working-tree state unknown"
            elif source["dirty"]:
                state = f"dirty: {source['modified_files']} modified file(s)"
            else:
                state = "clean"
            print(f"  head                {source['head']} ({state})")
        else:
            print("  head                unknown")
        if source["build_matches"] is True:
            print(f"  build vs HEAD       matches (HEAD declares build "
                  f"{source['head_build']})")
        elif source["build_matches"] is False:
            print(f"  build vs HEAD       DIFFERS: running {crystal['build']}, "
                  f"HEAD declares {source['head_build']}")
        else:
            print("  build vs HEAD       unknown")
        if source["reason"]:
            print(f"  note                {source['reason']}")
    elif source["kind"] == "snapshot":
        print(f"  source              snapshot, not a checkout ({source['reason']})")
    else:
        print(f"  source              unknown ({source['reason']})")
    if install["target"]:
        mode = "editable" if install["editable"] else "non-editable"
        build = install["target_build"] or "unknown"
        if install["covers_location"] is None:
            covers = f"coverage unknown ({install['reason']})"
        elif install["covers_location"]:
            covers = "covers this location"
        else:
            covers = "does NOT cover this location"
        print(f"  pip install         {mode} target {install['target']} "
              f"(build {build}), {covers}")
    else:
        print(f"  pip install         {install['reason']}")


def _print_doctor(report: dict) -> None:
    mark = {True: "ok", False: "--"}
    print(f"crystal {report['crystal']['version']} build {report['crystal']['build']}")
    print(f"  location            {report['crystal']['location']}")
    _print_source(report["crystal"])
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
    _print_isolation(report["isolation"])
    print("")
    for warning in report.get("warnings", ()):
        print(f"WARNING: {warning}")
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
        packs=getattr(args, "pack", None) or (),
    )


def _emit(result, output_format, output, quiet=False) -> None:
    data = payload(result)
    warning = (data.get("composition") or {}).get("warning")
    if warning and not quiet:
        print(f"WARNING: {warning}", file=sys.stderr)
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

def _pack_invariants(packs):
    """The invariants an operator's campaign packs declare."""
    if not packs:
        return []
    registry = discover_packs(packs=packs)
    seen, out = set(), []
    for campaign in registry.list_campaigns():
        for invariant in campaign.invariants:
            key = (invariant.statement, invariant.category)
            if key not in seen:
                seen.add(key)
                out.append(invariant)
    return out


def _compile_pack_properties(args, contracts) -> int:
    """Compile a pack's invariants into harnesses for the chosen backend.

    This is the axis the execution engines leave open. They each verified the
    five properties an operator wrote by hand — ~50 minutes and 1688 harness
    lines for Foundry alone. A campaign pack already carries those properties
    as `CampaignInvariant`s; compiling them is the difference between an engine
    that tells you where to look and one that also hands the looking to a
    prover.

    A deployment fixture is still the operator's to supply: how an upgradeable
    stack is wired, who the actors are, and how to build a call whose argument
    is a signature or a Bitcoin transaction cannot be derived from sources
    without inventing them. Crystal refuses to invent, and says so per property.
    """
    from .properties import compile_properties, split_report, write_compiled

    invariants = _pack_invariants(getattr(args, "pack", None))
    if not invariants:
        return 0
    fixture = _load_fixture(getattr(args, "fixture", None))
    compiled = [
        item for item in compile_properties(invariants, contracts, fixture)
        if item.backend == args.backend
    ]
    if not compiled:
        return 0
    print(split_report(compiled))
    ready = [item for item in compiled if not item.unsupported_reason]
    if ready and args.out_dir:
        written = write_compiled(ready, args.out_dir)
        print(f"{len(written)} harness(es) written to {args.out_dir}")
    elif ready:
        print(f"{len(ready)} harness(es) compiled; pass --out-dir to persist them.")
    if not fixture:
        print("  no deployment fixture supplied (--fixture): properties that "
              "need one are UNSUPPORTED rather than guessed.")
    return len(ready)


def _load_fixture(spec):
    """Load a deployment fixture from `module:factory` or a .py file path."""
    if not spec:
        return None
    import importlib
    import importlib.util

    target, _, attribute = str(spec).partition(":")
    attribute = attribute or "fixture"
    path = Path(target)
    if path.suffix == ".py" and path.is_file():
        loaded = importlib.util.spec_from_file_location("crystal._fixture", path)
        module = importlib.util.module_from_spec(loaded)
        loaded.loader.exec_module(module)
    else:
        module = importlib.import_module(target)
    factory = getattr(module, attribute, None)
    return factory() if callable(factory) else factory


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

    # Before anything is launched: the traps that make a green run meaningless.
    from .backends import preflight
    report = preflight.run(args.project, contracts, args.backend)
    # Only a refusal about code a property will actually run against blocks the
    # launch. Two test fixtures sharing a name collide with each other and with
    # nothing that matters; refusing on those would make the gate unusable and
    # teach an operator to pass --ignore-preflight by reflex, which is how a
    # safety check becomes decoration.
    in_scope = {
        c.name for c in contracts
        if not getattr(c, "is_test", False) and c.kind != "interface"
    }
    refusals = [
        issue for issue in report.issues
        if issue.severity.lower() == "refuse" and issue.contract in in_scope
    ]
    # One ambiguous name affects every contract that imports it. That is one
    # problem with N witnesses, not N problems — print it once and name them.
    grouped: dict[tuple, list] = {}
    for issue in report.issues:
        grouped.setdefault((issue.severity, issue.kind, issue.name), []).append(issue)
    for (severity, kind, name), items in sorted(
        grouped.items(), key=lambda kv: (kv[0][0] != "refuse", kv[0][2])
    )[:14]:
        head = items[0]
        affected = sorted({i.contract for i in items if i.contract})
        print(head.line())
        if len(affected) > 1:
            print(f"    affects {len(affected)}: {', '.join(affected[:6])}"
                  + (" ..." if len(affected) > 6 else ""))
    if len(grouped) > 14:
        print(f"  ... {len(grouped) - 14} further preflight finding(s)")
    if refusals and not getattr(args, "ignore_preflight", False):
        print(f"\n{len(refusals)} blocking issue(s): refusing to launch "
              f"{args.backend}. A green run under these conditions would mean "
              f"nothing. Pass --ignore-preflight to override.")
        return 2

    if _compile_pack_properties(args, contracts):
        return 0

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
        [sys.executable, "-m", "pip", "install", "-e", "."], cwd=root, timeout=600,
        # Crystal installing Crystal, from the operator's own checkout. A proxy
        # or certificate setting has to survive or this fails on a corporate
        # network, and no target code runs here.
        inherit_environment=True,
    )
    if install.returncode != 0:
        print(install.stderr.strip()[-2000:])
        return install.returncode or 1
    print("crystal updated.")
    return 0


def _campaign(args) -> int:
    from .campaigns import run_campaign as run_single_campaign

    registry = discover_packs()
    pack = getattr(args, "pack", None)
    if pack:
        registry.load_pack(pack)

    if args.campaign_command == "list":
        print(f"crystal {__version__} build {__build__}")
        print(f"registered packs: {', '.join(registry.list_packs())}")
        print()
        for campaign in registry.list_campaigns():
            status = "enabled" if campaign.enabled else "disabled"
            print(f"  [{status}] {campaign.campaign_id}")
            print(f"    {campaign.name} — {campaign.description}")
            print(f"    pack={campaign.pack}  priority={campaign.priority}")
        return 0

    if args.campaign_command == "run":
        campaign = registry.get(args.campaign_id)
        if campaign is None:
            print(f"unknown campaign: {args.campaign_id}", file=sys.stderr)
            print(f"available: {', '.join(c.campaign_id for c in registry.list_campaigns())}",
                  file=sys.stderr)
            return 2

        print(f"running campaign {campaign.name} against {args.project}...",
              file=sys.stderr)
        result = research(
            args.project,
            use_solc=not args.no_solc,
            use_foundry=not getattr(args, "no_foundry", False),
            include_tests=getattr(args, "include_tests", False),
        )
        cr = run_single_campaign(campaign, result)
        print(f"campaign: {cr.campaign_name}")
        print(f"  explored={cr.total_sequences_explored} "
              f"pruned={cr.total_sequences_pruned}")
        if cr.warning:
            print(f"  warning: {cr.warning}")
        if not cr.candidates:
            print("  no candidates produced.")
        for candidate in cr.top_candidates:
            print(f"  [{candidate.score:.3f}] {candidate.candidate_id}")
            print(f"    hypothesis: {candidate.hypothesis}")
            print(f"    sequence: {' -> '.join(candidate.state_sequence)}")
            print(f"    delta: {candidate.state_delta}")
            if candidate.evidence:
                for ev in candidate.evidence[:5]:
                    print(f"    evidence: {ev}")
        return 0

    return 2


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

    if args.command == "campaign":
        return _campaign(args)

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
