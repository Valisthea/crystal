"""Subprocess helper with predictable decoding.

Windows consoles hand back bytes that are not valid in the ANSI code page, and
`subprocess.run(text=True)` then returns `None` for the stream instead of a
string. Every external tool Crystal shells out to goes through here so callers
always get strings.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass

from .isolation import child_environment


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    stdout: str
    stderr: str
    ok: bool
    error: str = ""

    @property
    def output(self) -> str:
        return self.stdout + self.stderr

    def first_line(self) -> str:
        for line in self.output.splitlines():
            if line.strip():
                return line.strip()
        return ""


def run(command, cwd=None, timeout: int = 120, stdin: str | None = None,
        inherit_environment: bool = False, env_extra=None) -> ProcessResult:
    """Run an external tool with the environment Crystal chose to give it.

    The default withholds everything not on `isolation.ALWAYS | TOOLCHAIN`.
    That matters most for the property backends: Foundry hands the target's own
    Solidity harness `vm.envUint("PRIVATE_KEY")`, so inheriting the operator's
    environment put a deploy key inside code Crystal did not write.

    `inherit_environment=True` is for commands acting on the operator's own
    assets rather than on a target — installing Crystal into its own checkout,
    where a proxy or certificate setting has to survive. It is not a
    convenience for a backend that will not start; that is an allowlist gap,
    and the fix is to widen the allowlist or set `CRYSTAL_PASS_ENV`.
    """
    environment = None if inherit_environment else child_environment(extra=env_extra)
    try:
        completed = subprocess.run(
            command, cwd=cwd, input=stdin, timeout=timeout,
            capture_output=True, check=False, env=environment,
            encoding="utf-8", errors="replace",
        )
    except subprocess.TimeoutExpired as exc:
        return ProcessResult(
            None, _text(exc.stdout), _text(exc.stderr), False, "timeout",
        )
    except (OSError, ValueError) as exc:
        return ProcessResult(None, "", str(exc), False, str(exc))
    return ProcessResult(
        completed.returncode, _text(completed.stdout), _text(completed.stderr), True,
    )


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)
