"""Subprocess helper with predictable decoding.

Windows consoles hand back bytes that are not valid in the ANSI code page, and
`subprocess.run(text=True)` then returns `None` for the stream instead of a
string. Every external tool Crystal shells out to goes through here so callers
always get strings.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass


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


def run(command, cwd=None, timeout: int = 120, stdin: str | None = None) -> ProcessResult:
    try:
        completed = subprocess.run(
            command, cwd=cwd, input=stdin, timeout=timeout,
            capture_output=True, check=False,
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
