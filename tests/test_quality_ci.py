from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

import quality_ci as quality


ROOT = Path(__file__).resolve().parents[1]


def test_every_test_file_is_in_exactly_one_quality_job():
    groups = quality.test_groups(ROOT)
    expected = {
        path.relative_to(ROOT).as_posix()
        for pattern in ("test_*.py", "*_test.py")
        for path in (ROOT / "tests").rglob(pattern)
    }
    assigned = [path for files in groups.values() for path in files]

    assert set(assigned) == expected
    assert len(assigned) == len(expected)
    assert quality.PLAYER_TESTS <= set(groups["player"])
    assert "tests/test_history_webui.py" not in groups["manager"]
    assert "tests/test_runtime_builder.py" in groups["manager"]


def test_browser_groups_follow_shared_fixture_imports(tmp_path):
    tests = tmp_path / "tests"
    tests.mkdir()
    for name in quality.PLAYER_TESTS:
        (tmp_path / name).write_text("from playwright.sync_api import sync_playwright\n", encoding="utf-8")
    for name, source in {
        "browser_fixture.py": "from playwright.sync_api import sync_playwright",
        "fixture_helper.py": "from tests import browser_fixture",
        "test_uses_helper.py": "from fixture_helper import browser_fixture",
        "test_dynamic_browser.py": 'import pytest\npytest.importorskip("playwright.sync_api")',
        "test_pure.py": "def test_example(): pass",
        "test_uses_player.py": "from test_short_drama_player_controls import player_assets",
    }.items():
        (tests / name).write_text(source, encoding="utf-8")

    groups = quality.test_groups(tmp_path)

    assert groups["manager"] == ["tests/test_pure.py"]
    assert set(groups["browser-1"] + groups["browser-2"]) == {
        "tests/test_uses_helper.py", "tests/test_dynamic_browser.py",
    }
    assert "tests/test_uses_player.py" in groups["player"]


@pytest.mark.parametrize(("event", "changed", "enabled"), [
    ("pull_request", ["docs/changes.md"], quality.DOCS_ONLY),
    ("pull_request", ["tests/test_short_drama_player_controls.py", "docs/player.md"], quality.PLAYER_ONLY),
    ("pull_request", ["tests/test_short_drama_player_controls.py", "tests/conftest.py"], quality.FULL),
    ("pull_request", ["runtime/jiang_short_drama/title_search.py"], quality.FULL),
    ("pull_request", ["vendor/deploy/short_drama_player/index.html"], quality.FULL),
    ("pull_request", ["patches/short-drama-player-controls.patch"], quality.FULL),
    ("pull_request", [".github/workflows/quality.yml"], quality.FULL),
    ("pull_request", ["scripts/quality_ci.py"], quality.FULL),
    ("pull_request", ["docs/AGENTS.md"], quality.FULL),
    ("pull_request", ["docs/fixture.json"], quality.FULL),
    ("pull_request", [], quality.FULL),
    ("push", ["docs/player.md"], quality.FULL),
])
def test_quality_plan_selects_only_dependency_safe_skips(event, changed, enabled):
    assert quality.classify(event, changed) == enabled


def test_git_diff_uses_pr_merge_base_and_preserves_utf8_paths(monkeypatch):
    def run(command, **kwargs):
        assert command == [
            "git", "diff", "--no-renames", "--name-only", "-z", f"{'a' * 40}...{'b' * 40}", "--",
        ]
        assert kwargs["cwd"] == ROOT
        return SimpleNamespace(stdout="docs/:调整说明.md\0tests/test_short_drama_player_controls.py\0".encode())

    monkeypatch.setattr(subprocess, "run", run)

    assert quality._changed_files(ROOT, "a" * 40, "b" * 40) == [
        "docs/:调整说明.md", "tests/test_short_drama_player_controls.py",
    ]


def test_missing_git_diff_falls_back_to_full_coverage(monkeypatch):
    def run(command, **_kwargs):
        raise subprocess.CalledProcessError(128, command)

    monkeypatch.setattr(subprocess, "run", run)

    changed = quality._changed_files(ROOT, "a" * 40, "b" * 40)
    assert quality.classify("pull_request", changed) == quality.FULL


def _results(enabled):
    return {
        "plan": {
            "result": "success",
            "outputs": {flag: str(flag in enabled).lower() for flag in quality.JOB_FLAGS.values()},
        },
        **{
            job: {"result": "success" if flag in enabled else "skipped"}
            for job, flag in quality.JOB_FLAGS.items()
        },
    }


@pytest.mark.parametrize("enabled", [quality.FULL, quality.DOCS_ONLY, quality.PLAYER_ONLY])
def test_quality_gate_accepts_matching_outcomes(enabled):
    assert quality.gate_errors(_results(enabled)) == []


@pytest.mark.parametrize(("enabled", "job", "outcome"), [
    (quality.FULL, "browser", "failure"),
    (quality.FULL, "manager", "cancelled"),
    (quality.FULL, "frozen-manager", "skipped"),
    (quality.DOCS_ONLY, "manager", "failure"),
    (quality.PLAYER_ONLY, "player", "skipped"),
    (quality.PLAYER_ONLY, "browser", "success"),
])
def test_quality_gate_rejects_unexpected_outcomes(enabled, job, outcome):
    results = _results(enabled)
    results[job]["result"] = outcome

    assert any(job in error for error in quality.gate_errors(results))


def test_quality_gate_requires_a_valid_successful_plan():
    results = _results(quality.DOCS_ONLY)
    results["plan"]["result"] = "failure"
    assert quality.gate_errors(results)

    results = _results(quality.DOCS_ONLY)
    del results["browser"]
    assert quality.gate_errors(results)

    results = _results(quality.DOCS_ONLY)
    del results["plan"]["outputs"]["frozen"]
    assert quality.gate_errors(results)

    assert quality.gate_errors(_results(("frozen",)))
