"""The environment a child process is given, built by allowlist.

An allowlist, not a denylist, and the choice is not stylistic. A denylist of
secret-looking names — `PRIVATE_KEY`, `*_TOKEN`, `AWS_*` — is a list of the
secrets somebody thought of. The one that leaks is the one nobody named. An
allowlist fails the other way: a toolchain that needs an unusual variable stops
working loudly, and the operator opts it back in by name.

What is here is what the tools genuinely need to run. Everything else is
withheld, and `isolation_report()` says how many names were dropped without
printing any value — a report that echoes a secret to prove it withheld it has
achieved nothing.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

# Names the operator opts back in, comma-separated. Needed when a toolchain
# wants something unforeseen — a private registry, a proxy, a custom cache.
# Opting a secret back in is the operator's decision to make, and it is
# recorded in the isolation report so the decision stays visible.
OPT_IN_VARIABLE = "CRYSTAL_PASS_ENV"

# Without these a subprocess does not start, or starts and cannot find itself.
# `SystemRoot` in particular: omit it on Windows and the loader fails before
# the tool's first instruction.
ALWAYS = frozenset({
    # POSIX
    "PATH", "HOME", "TMPDIR", "SHELL", "USER", "LOGNAME",
    "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TERM",
    # Windows
    "SystemRoot", "SystemDrive", "windir", "COMSPEC", "PATHEXT",
    "TEMP", "TMP", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles",
    "ProgramFiles(x86)", "ProgramW6432", "CommonProgramFiles",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
})

# Where the analysis toolchains keep their own installs and caches. Foundry
# resolves solc versions under `~/.svm`, Medusa is a Go binary with a module
# cache, Halmos is a Python console script.
TOOLCHAIN = frozenset({
    "FOUNDRY_HOME", "SVM_HOME",
    "GOPATH", "GOCACHE", "GOMODCACHE", "GOROOT",
    "VIRTUAL_ENV",
    "SOLC_VERSION", "SOLCX_BINARY_PATH",
    "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
})

# Deliberately absent, and worth naming because a reader will look for them:
#
#   FOUNDRY_PROFILE  — selects a profile in the operator's global config
#   ETH_RPC_URL      — Foundry reads it natively; usually carries an API key
#   PYTHONPATH       — an import path is a way to run code of your choosing
#
# `HOME` is present because no toolchain works without it, and that is a real
# concession: `~/.foundry/foundry.toml` is reachable through it, and an
# operator who keeps an RPC URL there has not hidden it from the child. Stated
# rather than glossed — see `isolation_report()`.


@dataclass(frozen=True)
class EnvironmentDecision:
    """What was passed to a child, and what was kept back."""

    passed: tuple[str, ...]
    withheld: tuple[str, ...]
    opted_in: tuple[str, ...]

    @property
    def withheld_count(self) -> int:
        return len(self.withheld)


def _opted_in(source) -> frozenset[str]:
    raw = source.get(OPT_IN_VARIABLE, "")
    return frozenset(name.strip() for name in raw.split(",") if name.strip())


def _case_insensitive() -> bool:
    """Windows environment names are case-insensitive; POSIX names are not.

    `os.environ` on Windows stores `SYSTEMROOT` and looks up `SystemRoot`, so
    matching the allowlist exactly withheld the one variable without which a
    Windows child fails before its first instruction. On POSIX `path` and
    `PATH` are different variables and folding them would be wrong.
    """
    return os.name == "nt"


def _allows(allowed: frozenset[str]):
    if not _case_insensitive():
        return allowed.__contains__
    folded = {name.upper() for name in allowed}
    return lambda name: name.upper() in folded


def decide(source=None) -> EnvironmentDecision:
    """Split an environment into what a child may see and what it may not."""
    source = os.environ if source is None else source
    opted = _opted_in(source)
    permits = _allows(ALWAYS | TOOLCHAIN | opted)

    passed = sorted(name for name in source if permits(name))
    withheld = sorted(name for name in source if not permits(name))
    return EnvironmentDecision(
        tuple(passed), tuple(withheld),
        tuple(sorted(name for name in opted if name in source)),
    )


def child_environment(source=None, extra=None) -> dict[str, str]:
    """The environment for a process running code Crystal did not write.

    `extra` is for values Crystal itself sets on a run — a config path, a
    working directory — never for forwarding something out of the parent.
    """
    source = os.environ if source is None else source
    decision = decide(source)
    env = {name: source[name] for name in decision.passed}
    env.update(extra or {})
    return env


def withheld_names(source=None) -> tuple[str, ...]:
    """Names kept back. Names only — a value printed is a value leaked."""
    return decide(source).withheld


def isolation_report(source=None) -> dict:
    """What Crystal actually isolates, and what it does not.

    The third line is the one that matters. Crystal confines the filesystem and
    the environment; it does not run the tool under a different user, a
    container or a VM, so a fuzzer executing target bytecode has the operator's
    rights. Saying so is the difference between a claim and a fabrication, and
    an operator analysing something genuinely hostile needs to know which one
    this is before they start.
    """
    decision = decide(source)
    return {
        "environment": {
            "policy": "allowlist",
            "passed": list(decision.passed),
            "withheld_count": decision.withheld_count,
            "opted_in": list(decision.opted_in),
            "note": (
                "HOME is passed because no toolchain resolves without it; "
                "configuration stored under it is therefore reachable by the "
                "analysed target's tooling."
            ),
        },
        "filesystem": {
            "policy": "disposable working directory per execution",
            "note": (
                "each backend copies the contract under analysis into a "
                "temporary directory and runs there; the target tree is read, "
                "never written."
            ),
        },
        "process": {
            "policy": "not sandboxed",
            "note": (
                "external tools run as the operator, on the host. Crystal does "
                "not provide container or VM isolation, and does not claim to. "
                "Analyse a genuinely hostile target inside a disposable machine."
            ),
        },
        "executed_operator_code": {
            "policy": "explicit only",
            "note": (
                "packs and fixtures are Python and are executed in-process. "
                "They load only from a path passed on the command line — never "
                "discovered inside the analysed target."
            ),
        },
    }
