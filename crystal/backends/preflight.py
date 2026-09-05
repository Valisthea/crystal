"""Pre-flight: the traps that make a green run meaningless, found before launch.

Three of them were measured on one protocol on 2026-09-05, each of which
returned green from a backend that had decided nothing:

* **Homonym artifacts.** The repository declares `library Quotes` twice, at
  `src/libraries/Quotes.sol` and `src/legacy/Quotes.sol`. Foundry keeps both
  by disambiguating the artifact *path*; a backend that resolves the artifact
  *name* links whichever it meets first. Measured on Medusa: 5 sound runs out
  of 10. Crystal already knows names are not identities (`crystal/naming.py`);
  here that rule is applied to the artifact tree as it stands, with no
  hand-filtered directory list.
* **Zero-balance actors.** A harness whose actors are never funded cannot
  deposit. 1,049,043 calls, zero successful deposits, 35 tests green.
* **Frozen block.** Halmos pins `block.number` to 1, so any branch behind a
  block delay is unreachable and the property behind it returns PASS.

Each check reads the target, the generated harness or the backend config —
never the tool's output — so the report exists before the tool exists.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ..discovery import excluded_dir_reason
from .actions import (
    block_dependence,
    entry_points,
    mutating_actions,
    needs_value,
    property_name,
    property_subject,
    pulls_tokens,
)

REFUSE = "refuse"
UNSUPPORTED = "unsupported"
WARN = "warn"
INFO = "info"

HOMONYM = "homonym"
ZERO_BALANCE = "zero-balance-actors"
FROZEN_BLOCK = "frozen-block"
NO_MUTATION = "no-mutating-entry"
UNFUNDED_TOKENS = "unfunded-tokens"

# Build output and vendored code. `legacy/`, `test-contracts/`, `mocks/` are
# deliberately *not* here: the whole point is to see the tree as the compiler
# sees it. Vendored trees are covered by the compiler cache pass below, which
# lists exactly the files the compiler saw, wherever they came from.
VENDORED = frozenset({
    ".git", "node_modules", "lib", "out", "cache", "artifacts", "broadcast",
    "target", "dist", ".venv", "venv", "site-packages", "__pycache__",
    "typechain", "typechain-types", "forge-cache", ".build",
})

DECLARATION_RE = re.compile(
    r"^\s*(abstract\s+contract|contract|library|interface)\s+([A-Za-z_$][\w$]*)",
    re.MULTILINE,
)
COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
VALUE_FORWARD_RE = re.compile(r"\{\s*value\s*:")
DEAL_RE = re.compile(r"\b(?:vm|hevm|cheats?)\.deal\s*\(|\bdeal\s*\(")
BALANCE_ADDR_RE = re.compile(r"^\s*balanceAddr\s*:\s*(\S+)", re.MULTILINE)
BALANCE_CONTRACT_RE = re.compile(r"^\s*balanceContract\s*:\s*(\S+)", re.MULTILINE)
ALL_CONTRACTS_RE = re.compile(r"^\s*(?:allContracts|multi-abi)\s*:\s*true", re.MULTILINE)
MUTATING_FUNCTION_RE = re.compile(
    r"function\s+([A-Za-z_]\w*)\s*\(([^)]*)\)\s*((?:[a-z]+\s*)*)\{"
)
NON_ACTION_PREFIXES = ("property_", "optimize_", "check_", "invariant_", "echidna_", "test")


@dataclass(frozen=True)
class Homonym:
    """One name, declared at more than one path."""

    name: str
    kinds: tuple[str, ...]
    paths: tuple[str, ...]
    identical: bool
    linked: str = ""
    artifact: str = ""

    def describe(self) -> str:
        kind = "/".join(self.kinds) or "unit"
        text = (
            f"`{self.name}` ({kind}) is declared at {len(self.paths)} paths: "
            f"{', '.join(self.paths)} — "
            + ("byte-identical copies" if self.identical else "the definitions differ")
        )
        if self.linked:
            text += f". The artifact tree resolved the name to {self.linked}"
            if self.artifact:
                text += f" ({self.artifact})"
        return text


@dataclass(frozen=True)
class PreflightIssue:
    kind: str
    severity: str
    message: str
    name: str = ""
    paths: tuple[str, ...] = ()
    backend: str = ""
    contract: str = ""
    properties: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    def line(self) -> str:
        scope = f" [{self.contract}]" if self.contract else ""
        return f"[{self.severity.upper()}] {self.kind}{scope}: {self.message}"


@dataclass(frozen=True)
class PreflightReport:
    project: str
    backend: str
    issues: tuple[PreflightIssue, ...] = ()
    homonyms: tuple[Homonym, ...] = ()

    def for_contract(self, name: str) -> tuple[PreflightIssue, ...]:
        return tuple(
            issue for issue in self.issues if issue.contract in {"", name}
        )

    def blocking(self, name: str | None = None) -> tuple[PreflightIssue, ...]:
        issues = self.for_contract(name) if name else self.issues
        return tuple(issue for issue in issues if issue.severity == REFUSE)

    def undecidable(self, name: str, prop: str) -> str:
        """The reason a property cannot be decided on this backend, or ""."""
        for issue in self.for_contract(name):
            if issue.severity == UNSUPPORTED and prop in issue.properties:
                return issue.message
        return ""

    def excluded_actions(self, name: str) -> dict[str, tuple[str, ...]]:
        """Property -> actions the backend cannot exercise, from frozen-block
        warnings; the harness drops them so a PASS says nothing about them."""
        excluded: dict[str, tuple[str, ...]] = {}
        for issue in self.for_contract(name):
            if issue.kind == FROZEN_BLOCK and issue.severity in {WARN, UNSUPPORTED}:
                for prop in issue.properties:
                    excluded[prop] = tuple(dict.fromkeys(excluded.get(prop, ()) + issue.actions))
        return excluded

    def lines(self) -> list[str]:
        return [issue.line() for issue in self.issues]

    def to_dict(self) -> dict:
        return {
            "project": self.project,
            "backend": self.backend,
            "issues": [asdict(issue) for issue in self.issues],
            "homonyms": [asdict(item) for item in self.homonyms],
        }


# -- Homonym artifacts ------------------------------------------------------------

def _relative(path, root: Path) -> str:
    """Project-relative posix path by string arithmetic: `Path.resolve()` hits
    the filesystem once per component and a target with `node_modules` has
    thousands of import specifiers to relativise."""
    absolute = os.path.normpath(os.path.join(str(root), str(path)))
    base = os.path.normpath(str(root))
    if os.path.normcase(absolute).startswith(os.path.normcase(base) + os.sep):
        return Path(absolute[len(base) + 1:]).as_posix()
    return Path(path).as_posix()


def _walk_sources(root: Path, suffixes: tuple[str, ...]):
    """Files under `root` with one of `suffixes`, pruning vendored and build
    directories at the walk rather than after it."""
    for directory, subdirectories, files in os.walk(root):
        # The same directories the rest of the pipeline drops from research.
        # A collision between two deployment scripts, or inside node_modules,
        # cannot affect a property run against production code — and reporting
        # it teaches an operator to skip the gate.
        subdirectories[:] = [
            name for name in subdirectories
            if name not in VENDORED and excluded_dir_reason(name) is None
        ]
        for name in files:
            if name.endswith(suffixes):
                yield Path(directory) / name


def _declared_in_sources(root: Path) -> dict[str, list[tuple[str, str, str]]]:
    """name -> [(relative path, kind, content hash)] over the target's own tree."""
    found: dict[str, list[tuple[str, str, str]]] = {}
    for path in _walk_sources(root, (".sol",)):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        digest = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
        stripped = COMMENT_RE.sub("", text)
        for kind, name in DECLARATION_RE.findall(stripped):
            found.setdefault(name, []).append(
                (_relative(path, root), " ".join(kind.split()), digest)
            )
    for path in _walk_sources(root, (".vy",)):
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        found.setdefault(path.stem, []).append((_relative(path, root), "vyper", digest))
    return found


def _artifact_source(path: Path) -> str:
    """The source a Foundry artifact was built from, per its own metadata."""
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    ast = document.get("ast") if isinstance(document, dict) else None
    if isinstance(ast, dict) and ast.get("absolutePath"):
        return str(ast["absolutePath"])
    metadata = document.get("metadata") if isinstance(document, dict) else None
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            metadata = None
    if isinstance(metadata, dict):
        target = (metadata.get("settings") or {}).get("compilationTarget") or {}
        for source in target:
            return str(source)
    return ""


def _declared_in_foundry_cache(root: Path) -> tuple[dict[str, list[str]], dict[str, tuple[str, str]]]:
    """name -> source paths the compiler actually saw, and name -> (artifact,
    source) for the by-name artifact a name lookup lands on."""
    cache_file = root / "cache" / "solidity-files-cache.json"
    if not cache_file.is_file():
        return {}, {}
    try:
        cache = json.loads(cache_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    by_name: dict[str, list[str]] = {}
    for source, entry in (cache.get("files") or {}).items():
        if not isinstance(entry, dict):
            continue
        for name in (entry.get("artifacts") or {}):
            by_name.setdefault(name, []).append(Path(source).as_posix())
    linked: dict[str, tuple[str, str]] = {}
    for name, sources in by_name.items():
        if len(sources) < 2:
            continue
        for source in sources:
            candidate = root / "out" / Path(source).name / f"{name}.json"
            if candidate.is_file():
                origin = _artifact_source(candidate)
                if origin:
                    linked[name] = (_relative(candidate, root), Path(origin).as_posix())
                    break
    return by_name, linked


def _declared_in_hardhat_artifacts(root: Path) -> dict[str, list[str]]:
    base = root / "artifacts"
    if not base.is_dir():
        return {}
    by_name: dict[str, list[str]] = {}
    for path in base.rglob("*.json"):
        if not path.is_file() or path.name.endswith(".dbg.json"):
            continue
        try:
            if path.stat().st_size > 4_000_000:
                continue
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(document, dict):
            continue
        name, source = document.get("contractName"), document.get("sourceName")
        if name and source:
            by_name.setdefault(str(name), []).append(Path(str(source)).as_posix())
    return by_name


def find_homonyms(project) -> tuple[Homonym, ...]:
    """Every contract, library or interface name declared at more than one path.

    Sources are scanned as they stand; the compiler cache (Foundry) or artifact
    tree (Hardhat) is read when present, because it lists exactly what the
    compiler linked, vendored dependencies included.
    """
    root = Path(project)
    if not root.is_dir():
        return ()
    declared = _declared_in_sources(root)
    cached, linked = _declared_in_foundry_cache(root)
    hardhat = _declared_in_hardhat_artifacts(root)

    names = set(declared) | set(cached) | set(hardhat)
    homonyms = []
    for name in sorted(names):
        paths: dict[str, None] = {}
        kinds: dict[str, None] = {}
        digests: list[str] = []
        for path, kind, digest in declared.get(name, ()):
            paths[path] = None
            kinds[kind] = None
            digests.append(digest)
        for path in [*cached.get(name, ()), *hardhat.get(name, ())]:
            paths[path] = None
        if len(paths) < 2:
            continue
        # Compare the declarations themselves. A file digest answers a
        # different question and answers it wrongly here.
        declaration_digests = [
            _declaration_digest(root / path, name) for path in paths
        ]
        identical = (
            all(declaration_digests)
            and len(set(declaration_digests)) == 1
        )
        artifact, origin = linked.get(name, ("", ""))
        homonyms.append(Homonym(
            name, tuple(kinds), tuple(sorted(paths)), identical, origin, artifact
        ))
    return tuple(homonyms)


def _digest(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _declaration_digest(path: Path, name: str) -> str:
    """Digest the named declaration's own text, not the file that holds it.

    Two files can differ while the declaration they share is byte-identical —
    a helper interface repeated across generated harnesses, a vendored type
    copied verbatim beside other code. Comparing whole files calls those
    divergent and refuses to launch on a difference that is not there. The
    question the refusal turns on is whether the *definitions* differ.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return ""
    start = re.search(
        r"^[ \t]*(?:abstract\s+)?(?:contract|interface|library)\s+"
        + re.escape(name) + r"(?![A-Za-z0-9_])",
        text, re.MULTILINE,
    )
    if start is None:
        return ""
    opening = text.find("{", start.start())
    if opening < 0:
        return ""
    depth, index = 0, opening
    while index < len(text):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                index += 1
                break
        index += 1
    body = " ".join(text[start.start():index].split())
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _resolve_import(spec: str, importer: Path, root: Path) -> str:
    text = (spec or "").strip().strip('"\'')
    if not text:
        return ""
    if text.startswith("."):
        candidate = os.path.join(str(importer.parent), text)
    else:
        candidate = os.path.join(str(root), text)
    return _relative(os.path.normpath(candidate), root)


IMPORT_RE = re.compile(r'\bimport\s+(?:[^;"\']*?\bfrom\s+)?["\']([^"\']+)["\']')


def _references(contract, root: Path) -> tuple[str, tuple[str, ...]]:
    """The contract's own source text and the project-relative paths it imports.

    Import specifiers come from the parsed model and from the text itself, so
    the `import {Quotes} from "./libraries/Quotes.sol"` form is seen whichever
    front-end parsed the file.
    """
    path = Path(contract.path) if contract.path else None
    text = ""
    if path and path.is_file():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            text = ""
    stripped = COMMENT_RE.sub("", text)
    specs = list(contract.imports or ()) + IMPORT_RE.findall(stripped)
    imports = tuple(dict.fromkeys(
        resolved for resolved in (
            _resolve_import(spec, path, root) for spec in specs
        ) if resolved
    )) if path else ()
    return stripped, imports


def homonym_issues(homonyms, contracts, backend: str, project) -> list[PreflightIssue]:
    """Refuse a contract whose linkage touches a divergent homonym; warn on the rest."""
    root = Path(project)
    issues: list[PreflightIssue] = []
    if not homonyms:
        return issues
    # One alternation over every homonym name, run once per contract text:
    # the per-name search was 6,000 regex passes on the reference target.
    mention = re.compile(
        r"\b(" + "|".join(re.escape(item.name) for item in homonyms) + r")\b"
    )
    # A contract under test may itself be a homonym, so references are kept
    # per parsed unit rather than per name, and duplicate verdicts collapse.
    references = []
    for contract in contracts:
        text, imports = _references(contract, root)
        references.append((contract, set(mention.findall(text)), imports))
    for homonym in homonyms:
        affected: dict[tuple[str, tuple[str, ...]], None] = {}
        for contract, mentioned, imports in references:
            imported = tuple(path for path in imports if path in homonym.paths)
            if contract.name == homonym.name or imported or homonym.name in mentioned:
                affected[(contract.name, imported)] = None
        base = homonym.describe()
        if not affected or homonym.identical:
            issues.append(PreflightIssue(
                HOMONYM, WARN, base + ". Not linked by the contracts under test."
                if not affected else base + ". Copies are identical, so either link is the same code.",
                homonym.name, homonym.paths, backend,
            ))
            continue
        for name, imported in affected:
            detail = base
            if imported:
                detail += f". {name} imports {', '.join(imported)}"
                if homonym.linked and homonym.linked not in imported:
                    detail += f", not the {homonym.linked} the artifact tree linked"
            detail += (
                f". A backend that links by name may bind the wrong one; "
                f"refusing to launch {backend} for {name}."
            )
            issues.append(PreflightIssue(
                HOMONYM, REFUSE, detail, homonym.name, homonym.paths, backend, name,
            ))
    return issues


# -- Zero-balance actors ------------------------------------------------------------

def _subjects(contract, properties) -> tuple[str, ...]:
    subjects: dict[str, None] = {}
    for prop in properties or ():
        for name in property_subject(prop, contract):
            subjects[name] = None
    return tuple(subjects)


def _relevant_actions(contract, properties, contracts=()):
    subjects = _subjects(contract, properties)
    if subjects:
        return mutating_actions(contract, subjects, contracts), subjects
    return entry_points(contract), ()


def _config_text(config) -> str:
    if config is None:
        return ""
    if isinstance(config, str):
        return config
    try:
        return json.dumps(config)
    except (TypeError, ValueError):
        return str(config)


def _config_dict(config) -> dict:
    if isinstance(config, dict):
        return config
    if isinstance(config, str):
        try:
            loaded = json.loads(config)
        except ValueError:
            return {}
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _as_int(text: str) -> int | None:
    token = (text or "").strip().strip('"\'')
    try:
        return int(token, 0)
    except ValueError:
        return None


def forwarded_actions(harness_source: str) -> tuple[str, ...]:
    """Entry points a generated harness reaches: `h_<name>` fuzz handlers and
    `check_<property>_after_<name>` symbolic tests. Empty for a harness that
    follows neither convention, in which case every entry point is assumed."""
    names = []
    for name, _, _ in MUTATING_FUNCTION_RE.findall(harness_source or ""):
        if name.startswith("h_"):
            names.append(re.sub(r"_\d+$", "", name[2:]))
        elif name.startswith("check_") and "_after_" in name:
            names.append(name.rsplit("_after_", 1)[1])
    return tuple(dict.fromkeys(names))


def funding_issues(backend: str, traits, contract, properties, harness_source: str,
                   config, contracts=()) -> list[PreflightIssue]:
    """Can value ever reach the transitions the properties depend on?"""
    issues: list[PreflightIssue] = []
    actions, subjects = _relevant_actions(contract, properties, contracts)
    forwarded = forwarded_actions(harness_source)
    if forwarded:
        # An action the harness cannot forward is reported by the mutation
        # check as missing, not here as unfunded.
        actions = [function for function in actions if function.name in forwarded]
    value_actions = [
        function for function in actions if needs_value(contract, function, contracts)
    ]
    token_actions = [
        function for function in actions if pulls_tokens(contract, function, contracts)
    ]
    qualified = lambda function: f"{contract.name}.{function.name}"  # noqa: E731
    about = f" about {', '.join(subjects)}" if subjects else ""

    text = _config_text(config)
    if backend == "echidna":
        match = BALANCE_ADDR_RE.search(text)
        if match and _as_int(match.group(1)) == 0 and value_actions:
            issues.append(PreflightIssue(
                ZERO_BALANCE, REFUSE,
                f"echidna.yaml sets balanceAddr: 0 — every actor starts with zero "
                f"balance, so {', '.join(map(qualified, value_actions))} can never "
                f"receive value; every property{about} can only be VACUOUS",
                contract=contract.name, backend=backend,
                actions=tuple(map(qualified, value_actions)),
            ))
            return issues
    if backend == "medusa":
        fuzzing = _config_dict(config).get("fuzzing") or {}
        if "senderAddresses" in fuzzing and not fuzzing.get("senderAddresses"):
            issues.append(PreflightIssue(
                ZERO_BALANCE, REFUSE,
                "medusa.json sets senderAddresses: [] — there is no actor to call "
                "from, so no transition can happen",
                contract=contract.name, backend=backend,
            ))
            return issues

    if value_actions:
        forwards = bool(VALUE_FORWARD_RE.search(harness_source or ""))
        deals = bool(DEAL_RE.search(harness_source or ""))
        names = tuple(map(qualified, value_actions))
        if not forwards and not deals:
            everything = len(value_actions) == len(actions)
            issues.append(PreflightIssue(
                ZERO_BALANCE, REFUSE if everything else WARN,
                f"zero-balance path: {', '.join(names)} "
                f"{'is' if len(names) == 1 else 'are'} payable but the harness never "
                f"forwards msg.value nor funds an actor (no `{{value: ...}}`, no "
                f"`deal`); " + (
                    f"every property{about} can only be VACUOUS"
                    if everything else
                    f"those transitions cannot be exercised; properties{about} "
                    f"are decided only through the non-payable ones"
                ),
                contract=contract.name, backend=backend, actions=names,
            ))
        elif not getattr(traits, "funds_senders", True) and not deals:
            issues.append(PreflightIssue(
                ZERO_BALANCE, WARN,
                f"{backend} gives the caller no native balance by default and the "
                f"harness never deals any; {', '.join(names)} can only be reached "
                f"with msg.value == 0",
                contract=contract.name, backend=backend, actions=names,
            ))
    if token_actions:
        names = tuple(map(qualified, token_actions))
        issues.append(PreflightIssue(
            UNFUNDED_TOKENS, INFO,
            f"{', '.join(names)} pull ERC-20 balances the generated harness does "
            f"not mint; the witness will show whether they ever succeeded",
            contract=contract.name, backend=backend, actions=names,
        ))
    return issues


# -- Frozen block -----------------------------------------------------------------

def precompile_issues(backend: str, traits, contract, properties,
                      contracts=()) -> list[PreflightIssue]:
    """Properties whose reachable actions cross a precompile this tool cannot run.

    A symbolic engine that cannot model `sha256` does not report an error on a
    path that calls it — it explores until something kills it. Declaring the
    property UNSUPPORTED is a result an operator can act on; a run that never
    terminates teaches them to stop using the backend.
    """
    unmodelled = tuple(getattr(traits, "unmodelled_precompiles", ()) or ())
    if not unmodelled:
        return []
    issues: list[PreflightIssue] = []
    for prop in properties or ():
        name = property_name(prop)
        subject = property_subject(prop, contract)
        # An unresolved subject must widen the check, not cancel it: skipping
        # here would let the very case we cannot reason about through as
        # decidable, which is the failure mode this whole gate exists to stop.
        actions = (mutating_actions(contract, subject, contracts) if subject
                   else [f for f in contract.functions if f.is_entry_point])
        # `mutating_actions` yields names; the fallback yields functions.
        actions = [getattr(a, "name", a) for a in actions]
        crossed: dict[str, list[str]] = {}
        for function in actions:
            for called in _called_names(contract, function, contracts):
                if called in unmodelled:
                    crossed.setdefault(called, []).append(function)
        if not crossed:
            continue
        detail = "; ".join(
            f"`{name_}` on {', '.join(sorted(set(fns))[:3])}"
            for name_, fns in sorted(crossed.items())
        )
        issues.append(PreflightIssue(
            kind="unmodelled-precompile", severity=UNSUPPORTED,
            name=name, backend=backend, contract=contract.name,
            properties=(name,),
            message=(f"{backend} cannot execute {detail}: a path through it "
                     f"produces no verdict rather than an answer, so this "
                     f"property is undecidable on this backend"),
        ))
    return issues


def _called_names(contract, function_name: str, contracts=()) -> set[str]:
    """Bare callee names a function reaches, one level through internal calls."""
    found: set[str] = set()
    by_name = {f.name: f for f in contract.functions}
    pending, seen = [function_name], set()
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        function = by_name.get(current)
        if function is None or function.ir is None:
            continue
        for call in function.ir.calls():
            leaf = (call.callee or "").rsplit(".", 1)[-1]
            found.add(leaf)
            if leaf in by_name and len(seen) < 24:
                pending.append(leaf)
    return found


def block_issues(backend: str, traits, contract, properties, contracts=()) -> list[PreflightIssue]:
    """Which properties hide behind a block or timestamp delta this backend
    cannot advance past."""
    issues: list[PreflightIssue] = []
    advances = getattr(traits, "advances_block", True)
    for prop in properties or ():
        name = property_name(prop)
        subject = property_subject(prop, contract)
        actions = mutating_actions(contract, subject, contracts) if subject else []
        dependent = [
            (function, block_dependence(contract, function, contracts))
            for function in actions
        ]
        dependent = [(function, clauses) for function, clauses in dependent if clauses]
        if not dependent:
            continue
        described = "; ".join(
            f"`{contract.name}.{function.name}` ({' / '.join(clauses[:2])})"
            for function, clauses in dependent
        )
        action_names = tuple(f"{contract.name}.{function.name}" for function, _ in dependent)
        if advances:
            issues.append(PreflightIssue(
                FROZEN_BLOCK, INFO,
                f"{name}: {described} read the block environment; {backend} advances "
                f"block.number and block.timestamp during a run, so the branch is reachable",
                name, backend=backend, contract=contract.name, properties=(name,),
                actions=action_names,
            ))
            continue
        if len(dependent) == len(actions):
            issues.append(PreflightIssue(
                FROZEN_BLOCK, UNSUPPORTED,
                f"{name} can only be reached through {described}, whose reachability "
                f"depends on a block or timestamp delta; {backend} pins block.number "
                f"to 1 and cannot decide it",
                name, backend=backend, contract=contract.name, properties=(name,),
                actions=action_names,
            ))
        else:
            blocked = {id(function) for function, _ in dependent}
            remaining = ", ".join(
                f"{contract.name}.{function.name}" for function in actions
                if id(function) not in blocked
            )
            issues.append(PreflightIssue(
                FROZEN_BLOCK, WARN,
                f"{name}: {backend} pins block.number to 1 and cannot exercise "
                f"{described}; the property is decided only through {remaining}",
                name, backend=backend, contract=contract.name, properties=(name,),
                actions=action_names,
            ))
    return issues


# -- No mutating entry point ------------------------------------------------------

def harness_actions(harness_source: str) -> tuple[str, ...]:
    """Public/external, non-view functions of a harness that are not tests."""
    found = []
    for name, _, qualifiers in MUTATING_FUNCTION_RE.findall(harness_source or ""):
        words = set(qualifiers.split())
        if not words & {"public", "external"} or words & {"view", "pure"}:
            continue
        if name.startswith(NON_ACTION_PREFIXES) or name in {"setUp", "constructor"}:
            continue
        found.append(name)
    return tuple(dict.fromkeys(found))


def mutation_issues(backend: str, traits, contract, harness_source: str, config,
                    properties=(), contracts=()) -> list[PreflightIssue]:
    """Can the harness move the state each property is about at all?

    A fuzzer picks its calls from the harness; with nothing to pick, nothing
    moves (REFUSE). A symbolic backend's tests call the target themselves, so
    that part does not apply to it. Either way, a property whose every
    mutating action the harness could not forward — a struct argument, an enum
    — cannot be witnessed on this harness and is reported UNSUPPORTED before
    the tool is asked to confirm it with a green.
    """
    issues: list[PreflightIssue] = []
    fuzzer = getattr(traits, "kind", "fuzzer") == "fuzzer"
    if fuzzer and not getattr(traits, "fuzzes_nested_deployments", False) \
            and not (backend == "echidna" and ALL_CONTRACTS_RE.search(_config_text(config))) \
            and not harness_actions(harness_source):
        return [PreflightIssue(
            NO_MUTATION, REFUSE,
            f"the harness exposes no state-mutating entry point and {backend} only "
            f"calls the harness's own functions, so no transition can ever occur; "
            f"every property would be green by construction",
            contract=contract.name, backend=backend,
        )]
    forwarded = forwarded_actions(harness_source)
    if not forwarded:
        return issues
    for prop in properties or ():
        name = property_name(prop)
        subject = property_subject(prop, contract)
        if not subject:
            continue
        writers = mutating_actions(contract, subject, contracts)
        if not writers or any(function.name in forwarded for function in writers):
            continue
        missing = ", ".join(
            f"{contract.name}.{function.name}"
            f"({', '.join(parameter.type_name for parameter in function.params)})"
            for function in writers
        )
        issues.append(PreflightIssue(
            NO_MUTATION, UNSUPPORTED,
            f"{name} can only be moved through {missing}, which the harness could "
            f"not forward (argument types Crystal cannot fabricate); no transition "
            f"in this run can touch {', '.join(subject)}, so the property cannot "
            f"be witnessed on this harness",
            name, backend=backend, contract=contract.name, properties=(name,),
            actions=tuple(f"{contract.name}.{function.name}" for function in writers),
        ))
    return issues


# -- Orchestration ------------------------------------------------------------------

def run(project, contracts, backend: str, traits=None, properties_by_contract=None,
        harness_by_contract=None, config_by_contract=None, all_contracts=None) -> PreflightReport:
    """Every check, on the target as it stands, before any backend is launched."""
    properties_by_contract = properties_by_contract or {}
    harness_by_contract = harness_by_contract or {}
    config_by_contract = config_by_contract or {}
    all_contracts = list(all_contracts) if all_contracts is not None else list(contracts)

    homonyms = find_homonyms(project)
    issues = homonym_issues(homonyms, list(contracts), backend, project)
    for contract in contracts:
        properties = properties_by_contract.get(contract.name, ())
        harness = harness_by_contract.get(contract.name, "")
        config = config_by_contract.get(contract.name)
        issues += funding_issues(backend, traits, contract, properties, harness, config, all_contracts)
        issues += block_issues(backend, traits, contract, properties, all_contracts)
        issues += precompile_issues(backend, traits, contract, properties,
                                    all_contracts)
        if harness:
            issues += mutation_issues(
                backend, traits, contract, harness, config, properties, all_contracts,
            )
    return PreflightReport(str(project), backend, tuple(issues), homonyms)
