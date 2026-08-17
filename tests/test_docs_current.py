"""The README and CHANGELOG must describe the build that is actually shipping.

Documentation drifted four builds behind before this existed. A release note
nobody checks is the same defect class the version string had: it looks
maintained right up until someone relies on it.
"""

import re
import subprocess
import sys
from pathlib import Path

from crystal import __build__, __release__, __version__
from crystal.detectors import DETECTORS

ROOT = Path(__file__).resolve().parent.parent
README = ROOT / "README.md"
CHANGELOG = ROOT / "CHANGELOG.md"


def readme() -> str:
    return README.read_text(encoding="utf-8")


def changelog() -> str:
    return CHANGELOG.read_text(encoding="utf-8")


def test_readme_states_the_current_release():
    assert __release__ in readme(), (
        f"README does not mention {__release__}; bump it with the build"
    )


def test_changelog_has_an_entry_for_the_current_build():
    heading = f"## Crystal V{__version__[:3]}0 Build {__build__}"
    text = changelog()
    assert heading in text, f"CHANGELOG is missing a section for build {__build__}"
    assert text.index(heading) < 400, "the newest build must be the first entry"


def test_every_detector_is_documented_in_the_readme():
    text = readme()
    undocumented = sorted(
        module.DETECTOR for module in DETECTORS.values()
        if module.DETECTOR not in text
    )
    assert not undocumented, f"detectors missing from the README: {undocumented}"


def test_readme_test_count_matches_the_suite():
    """The badge is a claim about this repository; keep it true.

    Counted by asking pytest, not by grepping for `def test_`: the two disagree
    (class-based and parametrized tests), and a check that compares the badge
    against the wrong number passes while saying nothing.
    """
    match = re.search(r"tests-(\d+)%20passing", readme())
    assert match, "README has no test-count badge"
    claimed = int(match.group(1))

    collected = subprocess.run(
        [sys.executable, "-m", "pytest", str(ROOT / "tests"), "--collect-only", "-q"],
        capture_output=True, text=True, timeout=300, cwd=str(ROOT),
        encoding="utf-8", errors="replace",
    )
    found = re.search(r"(\d+)\s+tests?\s+collected", collected.stdout)
    assert found, f"could not read pytest's collection count:\n{collected.stdout[-500:]}"
    assert claimed == int(found.group(1)), (
        f"README badge claims {claimed} tests, pytest collects {found.group(1)}"
    )


def test_evidence_only_contract_is_stated_up_front():
    head = readme()[:1200]
    assert "never produces a confirmed finding" in head.lower() or \
           "never produce a confirmed finding" in head.lower()
