import subprocess
import sys


def test_cli_capabilities_runs():
    p = subprocess.run(
        [sys.executable, "-m", "crystal.cli", "capabilities"],
        capture_output=True, text=True, check=True
    )
    assert "crystal 1.0.0" in p.stdout
    assert "compiler-ast-model" in p.stdout
