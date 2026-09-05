"""Build drift in `crystal doctor`.

Four times in one day the editable install resolved to an extracted ZIP
snapshot several builds behind the repo, scans ran stale code for hours, and
nothing reported it. Doctor now says where the imported package lives, whether
that place is a git checkout, and whether the running build matches HEAD.
Drift is a warning, never a failure: the exit code still reflects readiness.

Every situation is faked here (a throwaway git repository, a bare directory,
a monkeypatched `shutil.which`, a monkeypatched `process.run`, a monkeypatched
`importlib.metadata`), so these tests pass on a fresh clone with no snapshots
present, and the git-absent path is exercised even on a machine that has git.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from crystal import __build__, cli, process

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git is not installed")
SHORT_SHA = re.compile(r"[0-9a-f]{7,40}")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        [GIT, "-c", "user.name=crystal", "-c", "user.email=crystal@test",
         "-c", "commit.gpgsign=false", *args],
        cwd=cwd, check=True, capture_output=True,
    )


def _checkout(root: Path, build: str) -> Path:
    """A throwaway repository shaped like Crystal: `<root>/crystal/__init__.py`."""
    package = root / "crystal"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__build__ = "{build}"\n', encoding="utf-8")
    _git("init", "-q", cwd=root)
    _git("add", ".", cwd=root)
    _git("commit", "-q", "-m", f"Build {build}", cwd=root)
    return package


def _snapshot(root: Path, build: str) -> Path:
    """An extracted archive: the same layout, no git anywhere in it."""
    package = root / "crystal-main" / "crystal"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(f'__build__ = "{build}"\n', encoding="utf-8")
    return package


def _crystal(build: str, source: dict, install: dict | None = None) -> dict:
    return {
        "version": "1.0.0", "build": build, "release": f"Build {build}",
        "location": "C:/somewhere/crystal", "source": source,
        "install": install or {
            "distribution": "crystal-security-engine", "editable": None,
            "target": None, "target_build": None, "covers_location": None,
            "reason": "not pip-installed",
        },
    }


CHECKOUT = {
    "kind": "checkout", "git_root": "C:/somewhere", "head": "abc1234",
    "dirty": False, "modified_files": 0, "head_build": __build__,
    "build_matches": True, "reason": None,
}
SNAPSHOT = {
    "kind": "snapshot", "git_root": None, "head": None, "dirty": None,
    "modified_files": None, "head_build": None, "build_matches": None,
    "reason": "no git working tree above the package",
}


# -- source_report against real directories ----------------------------------

@needs_git
def test_checkout_reports_head_and_matching_build(tmp_path):
    package = _checkout(tmp_path / "repo", "042")
    source = cli.source_report(package, build="042")
    assert source["kind"] == "checkout"
    assert Path(source["git_root"]).resolve() == (tmp_path / "repo").resolve()
    assert SHORT_SHA.fullmatch(source["head"])
    assert source["dirty"] is False
    assert source["modified_files"] == 0
    assert source["head_build"] == "042"
    assert source["build_matches"] is True
    assert source["reason"] is None
    assert cli.drift_warnings(_crystal("042", source)) == []


@needs_git
def test_checkout_with_uncommitted_bump_reports_dirty_and_mismatch(tmp_path):
    package = _checkout(tmp_path / "repo", "042")
    (package / "__init__.py").write_text('__build__ = "043"\n', encoding="utf-8")
    source = cli.source_report(package, build="043")
    assert source["kind"] == "checkout"
    assert source["dirty"] is True
    assert source["modified_files"] == 1
    assert source["head_build"] == "042"
    assert source["build_matches"] is False
    warnings = cli.drift_warnings(_crystal("043", source))
    assert len(warnings) == 1
    assert "043" in warnings[0] and "042" in warnings[0]
    assert source["head"] in warnings[0]
    assert "uncommitted" in warnings[0]


def test_snapshot_outside_any_git_tree(tmp_path):
    package = _snapshot(tmp_path, "009")
    source = cli.source_report(package, build="009")
    assert source["kind"] == "snapshot"
    assert source["head"] is None
    assert source["dirty"] is None
    assert source["build_matches"] is None
    assert source["reason"]
    warnings = cli.drift_warnings(_crystal("009", source))
    assert len(warnings) == 1
    assert "snapshot, not a checkout" in warnings[0]
    assert "build 009" in warnings[0]
    assert "cannot self-update" in warnings[0]


@needs_git
def test_snapshot_dropped_inside_a_foreign_repository_is_still_a_snapshot(tmp_path):
    _checkout(tmp_path / "repo", "042")
    stray = tmp_path / "repo" / "vendor" / "crystal"
    stray.mkdir(parents=True)
    (stray / "__init__.py").write_text('__build__ = "009"\n', encoding="utf-8")
    source = cli.source_report(stray, build="009")
    assert source["kind"] == "snapshot"
    assert Path(source["git_root"]).resolve() == (tmp_path / "repo").resolve()
    assert "not tracked" in source["reason"]
    assert source["head"] is None


# -- degradation: git absent or failing --------------------------------------

def test_git_absent_degrades_to_unknown_fields_without_crash(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda *args, **kwargs: None)

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    package = repo / "crystal"
    package.mkdir()
    source = cli.source_report(package, build="042")
    assert source["kind"] == "checkout"
    assert Path(source["git_root"]) == repo
    assert source["head"] is None
    assert source["dirty"] is None
    assert source["build_matches"] is None
    assert "git is not installed" in source["reason"]
    assert cli.drift_warnings(_crystal("042", source)) == []

    if cli._nearest_git_entry(tmp_path) is not None:
        pytest.skip("temporary directory sits inside a git repository")
    source = cli.source_report(_snapshot(tmp_path, "009"), build="009")
    assert source["kind"] == "snapshot"
    assert "git is not installed" in source["reason"]


def test_git_absent_doctor_still_completes(monkeypatch, capsys):
    monkeypatch.setattr(cli.shutil, "which", lambda *args, **kwargs: None)
    code = cli.main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    source = report["crystal"]["source"]
    assert source["kind"] in {"checkout", "snapshot"}
    assert source["head"] is None
    assert source["build_matches"] is None
    assert "git is not installed" in source["reason"]
    assert report["tools"]["git"]["available"] is False
    assert code == (0 if report["ready"] else 1)


def test_git_failing_to_run_degrades_to_unknown(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda *args, **kwargs: "git")
    monkeypatch.setattr(
        cli.process, "run",
        lambda *args, **kwargs: process.ProcessResult(None, "", "boom", False, "boom"),
    )
    source = cli.source_report(tmp_path, build="042")
    assert source["kind"] == "unknown"
    assert "boom" in source["reason"]
    assert cli.drift_warnings(_crystal("042", source)) == []


def test_git_refusing_the_repository_degrades_to_unknown(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    monkeypatch.setattr(cli.shutil, "which", lambda *args, **kwargs: "git")
    monkeypatch.setattr(
        cli.process, "run",
        lambda *args, **kwargs: process.ProcessResult(
            128, "", "fatal: detected dubious ownership in repository", True),
    )
    source = cli.source_report(repo / "crystal", build="042")
    assert source["kind"] == "unknown"
    assert "dubious ownership" in source["reason"]


def test_git_denying_a_repository_with_no_dot_git_means_snapshot(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.shutil, "which", lambda *args, **kwargs: "git")
    monkeypatch.setattr(
        cli.process, "run",
        lambda *args, **kwargs: process.ProcessResult(
            128, "", "fatal: not a git repository", True),
    )
    if cli._nearest_git_entry(tmp_path) is not None:
        pytest.skip("temporary directory sits inside a git repository")
    source = cli.source_report(_snapshot(tmp_path, "009"), build="009")
    assert source["kind"] == "snapshot"


# -- install_report: where pip believes the package lives --------------------

class _Distribution:
    def __init__(self, raw):
        self.raw = raw

    def read_text(self, name):
        assert name == "direct_url.json"
        return self.raw


def test_install_report_flags_an_editable_target_elsewhere(monkeypatch, tmp_path):
    target = tmp_path / "crystal-main"
    _snapshot(tmp_path, "009")
    raw = json.dumps({"dir_info": {"editable": True}, "url": target.as_uri()})
    monkeypatch.setattr(cli.importlib_metadata, "distribution",
                        lambda name: _Distribution(raw))

    elsewhere = tmp_path / "checkout" / "crystal"
    elsewhere.mkdir(parents=True)
    install = cli.install_report(elsewhere)
    assert install["editable"] is True
    assert Path(install["target"]).resolve() == target.resolve()
    assert install["target_build"] == "009"
    assert install["covers_location"] is False
    warnings = cli.drift_warnings(_crystal("013", CHECKOUT, install))
    assert len(warnings) == 1
    assert "pip" in warnings[0] and "009" in warnings[0]

    install = cli.install_report(target / "crystal")
    assert install["covers_location"] is True
    assert cli.drift_warnings(_crystal("013", CHECKOUT, install)) == []


def test_install_report_when_not_pip_installed(monkeypatch, tmp_path):
    def missing(name):
        raise cli.importlib_metadata.PackageNotFoundError(name)

    monkeypatch.setattr(cli.importlib_metadata, "distribution", missing)
    install = cli.install_report(tmp_path)
    assert install["target"] is None
    assert install["covers_location"] is None
    assert "not pip-installed" in install["reason"]
    assert cli.drift_warnings(_crystal("013", CHECKOUT, install)) == []


def test_install_report_without_direct_url_claims_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(cli.importlib_metadata, "distribution",
                        lambda name: _Distribution(None))
    install = cli.install_report(tmp_path)
    assert install["editable"] is None
    assert install["target"] is None
    assert "direct_url.json" in install["reason"]


def test_non_editable_local_install_does_not_warn(monkeypatch, tmp_path):
    """`pip install .` records a file: target too, but its code runs from
    site-packages; only an editable target can shadow this run."""
    target = tmp_path / "crystal-main"
    _snapshot(tmp_path, "009")
    raw = json.dumps({"dir_info": {}, "url": target.as_uri()})
    monkeypatch.setattr(cli.importlib_metadata, "distribution",
                        lambda name: _Distribution(raw))
    install = cli.install_report(tmp_path / "site-packages" / "crystal")
    assert install["editable"] is False
    assert install["covers_location"] is False
    assert cli.drift_warnings(_crystal("013", CHECKOUT, install)) == []


# -- doctor output: text and --json carry the same facts ---------------------

def test_doctor_text_reports_checkout_sha_and_matches(monkeypatch, capsys):
    monkeypatch.setattr(cli, "source_report", lambda *a, **k: dict(CHECKOUT))
    monkeypatch.setattr(cli, "install_report",
                        lambda *a, **k: _crystal("x", CHECKOUT)["install"])
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert f"build {__build__}" in out
    assert "git checkout at C:/somewhere" in out
    assert "abc1234 (clean)" in out
    assert f"matches (HEAD declares build {__build__})" in out
    assert "WARNING" not in out
    assert code == 0


def test_doctor_text_reports_snapshot_with_a_warning_and_same_exit_code(
        monkeypatch, capsys):
    monkeypatch.setattr(cli, "source_report", lambda *a, **k: dict(SNAPSHOT))
    monkeypatch.setattr(cli, "install_report",
                        lambda *a, **k: _crystal("x", SNAPSHOT)["install"])
    code = cli.main(["doctor"])
    out = capsys.readouterr().out
    assert "snapshot, not a checkout" in out
    assert re.search(r"^WARNING: build \d+ at .* is a snapshot, not a checkout",
                     out, re.MULTILINE)
    assert "cannot self-update and will not track the repo" in out
    assert "environment ready" in out
    assert code == 0, "drift is a warning, not a failure"


def test_doctor_json_carries_the_same_facts(monkeypatch, capsys):
    monkeypatch.setattr(cli, "source_report", lambda *a, **k: dict(SNAPSHOT))
    monkeypatch.setattr(cli, "install_report",
                        lambda *a, **k: _crystal("x", SNAPSHOT)["install"])
    code = cli.main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["crystal"]["build"] == __build__
    assert report["crystal"]["source"] == SNAPSHOT
    assert report["crystal"]["install"]["target"] is None
    assert len(report["warnings"]) == 1
    assert "snapshot, not a checkout" in report["warnings"][0]
    assert report["blocking"] == []
    assert report["ready"] is True
    assert code == 0


def test_doctor_json_checkout_mismatch_is_structured(monkeypatch, capsys):
    drifted = dict(CHECKOUT, head_build="011", build_matches=False, dirty=True,
                   modified_files=2)
    monkeypatch.setattr(cli, "source_report", lambda *a, **k: drifted)
    monkeypatch.setattr(cli, "install_report",
                        lambda *a, **k: _crystal("x", drifted)["install"])
    cli.main(["doctor", "--json"])
    report = json.loads(capsys.readouterr().out)
    assert report["crystal"]["source"]["build_matches"] is False
    assert report["crystal"]["source"]["head_build"] == "011"
    assert report["crystal"]["source"]["dirty"] is True
    assert any("011" in w and __build__ in w for w in report["warnings"])
    assert report["ready"] is True


def test_doctor_on_this_checkout_is_internally_consistent():
    """On a clone, the imported package is a tracked checkout and a clean tree
    means the running build equals HEAD's. Never asserts cleanliness itself."""
    report = cli.environment_report()
    source = report["crystal"]["source"]
    assert source["kind"] in {"checkout", "snapshot", "unknown"}
    assert isinstance(report["warnings"], list)
    if source["kind"] == "checkout" and source["head"]:
        assert SHORT_SHA.fullmatch(source["head"])
        if source["dirty"] is False and source["head_build"] is not None:
            assert source["build_matches"] is True
            assert source["head_build"] == __build__
