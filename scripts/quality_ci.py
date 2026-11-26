"""Select and verify the Windows quality CI jobs without importing test modules."""

from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PLAYER_TESTS = frozenset({
    "tests/test_short_drama_player_controls.py",
    "tests/test_short_drama_player_continuity.py",
    "tests/test_short_drama_recommendation_ratio.py",
    "tests/test_short_drama_search_interactions.py",
})
JOB_FLAGS = {"manager": "manager", "browser": "browser", "player": "player", "frozen-manager": "frozen"}
FULL = tuple(JOB_FLAGS.values())
PLAYER_ONLY = ("player",)
DOCS_ONLY = ()
BROWSER_MODULES = {"playwright", "pytest_playwright", "selenium", "pyppeteer"}
BROWSER_FIXTURES = {"browser", "browser_context", "manager_browser_launch_options", "page"}

# Per-file elapsed seconds from the quality run 45738769265. New modules
# use a small default weight until timing data is refreshed.
BROWSER_SECONDS: dict[str, int] = {
    "test_activity_webui": 5,
    "test_admin_codes_webui": 2,
    "test_admin_release_editor_webui": 4,
    "test_admin_release_webui": 17,
    "test_admin_upload_webui": 9,
    "test_backup_restore_webui": 15,
    "test_browser_launch": 1,
    "test_chat_binding_readback_webui": 4,
    "test_chat_management_webui": 15,
    "test_chat_policy_validation_webui": 14,
    "test_chat_refresh_webui": 23,
    "test_contact_profile_webui": 42,
    "test_conversation_layout_webui": 16,
    "test_core_readback_webui": 10,
    "test_core_settings_webui": 3,
    "test_feature_numeric_webui": 4,
    "test_feature_readback_webui": 32,
    "test_feature_status_webui": 6,
    "test_group_policy_receipt_webui": 6,
    "test_group_qrcode_webui": 12,
    "test_history_cleanup_webui": 4,
    "test_history_connection_retention_webui": 26,
    "test_history_resume_webui": 7,
    "test_history_webui": 43,
    "test_maintenance_webui": 6,
    "test_manager_startup": 86,
    "test_message_image_preview_webui": 41,
    "test_modal_close_guard_webui": 8,
    "test_model_actions_webui": 7,
    "test_model_card_tests_webui": 12,
    "test_model_discovery_webui": 21,
    "test_model_layout_webui": 24,
    "test_model_motion_webui": 5,
    "test_model_settings_webui": 38,
    "test_native_relationship_actions_webui": 15,
    "test_native_selflearning_settings_webui": 28,
    "test_native_voice_editor_guard_webui": 33,
    "test_native_voice_settings_webui": 17,
    "test_onboarding_entry_webui": 9,
    "test_panel_layout": 8,
    "test_private_policy_webui": 22,
    "test_runtime_connection_webui": 6,
    "test_runtime_environment_webui": 52,
    "test_schedule_agent_ui": 7,
    "test_scheduled_analysis_ui": 17,
    "test_scheduled_receipt_webui": 9,
    "test_scheduled_runs_webui": 17,
    "test_settings_webui": 45,
    "test_skin_webui": 40,
    "test_subscription_webui": 41,
    "test_tab_motion_webui": 11,
    "test_tab_retention_webui": 9,
    "test_update_flow_webui": 73,
    "test_update_install_receipt_webui": 17,
    "test_web_search_read_failure_webui": 4,
    "test_web_search_settings_webui": 17,
}


def _module_usage(path: Path) -> tuple[set[str], bool]:
    imports: set[str] = set()
    fixtures: set[str] = set()
    for node in ast.walk(ast.parse(path.read_bytes(), filename=str(path))):
        if isinstance(node, ast.Import):
            imports.update(name.name for name in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module and module != "tests":
                imports.add(module)
            else:
                imports.update(name.name for name in node.names)
        elif (
            isinstance(node, ast.Call) and node.args
            and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)
            and isinstance(node.func, ast.Attribute) and node.func.attr in {"import_module", "importorskip"}
        ):
            imports.add(node.args[0].value)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
            fixtures.update(arg.arg for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs))
    imports = {name.removeprefix("tests.").split(".", 1)[0] for name in imports}
    return imports, bool(imports & BROWSER_MODULES or fixtures & BROWSER_FIXTURES)


def test_groups(root: Path) -> dict[str, list[str]]:
    """Partition pytest's test modules using their browser imports and fixtures."""
    test_root = root / "tests"
    modules = sorted(
        path for path in test_root.rglob("*.py")
        if not any(part.startswith(".") or part == "__pycache__" for part in path.relative_to(test_root).parts)
    )
    paths = [path for path in modules if path.name.startswith("test_") or path.name.endswith("_test.py")]
    by_name: dict[str, Path] = {}
    for path in modules:
        if path.name in {"__init__.py", "conftest.py"}:
            continue
        if path.stem in by_name:
            raise ValueError(f"Cannot classify duplicate local test module {path.stem}")
        by_name[path.stem] = path

    browser: set[Path] = set()
    imports: dict[Path, set[Path]] = {}
    for path in by_name.values():
        names, uses_browser = _module_usage(path)
        imports[path] = {by_name[name] for name in names if name in by_name}
        if uses_browser:
            browser.add(path)

    player = {root / name for name in PLAYER_TESTS}
    if not player.issubset(paths) or not (player & browser):
        raise ValueError("Cannot classify the required player browser tests")
    while True:
        added_browser = {path for path, dependencies in imports.items() if dependencies & browser}
        added_player = {path for path, dependencies in imports.items() if dependencies & player}
        if added_browser <= browser and added_player <= player:
            break
        browser |= added_browser
        player |= added_player

    group = {name: [] for name in ("manager", "browser-1", "browser-2", "player")}
    totals = {"browser-1": 0, "browser-2": 0}
    for path in sorted(paths, key=lambda item: (-BROWSER_SECONDS.get(item.stem, 8), item)):
        name = path.relative_to(root).as_posix()
        if path in player:
            group["player"].append(name)
        elif path in browser:
            shard = min(totals, key=totals.__getitem__)
            group[shard].append(name)
            totals[shard] += BROWSER_SECONDS.get(path.stem, 8)
        else:
            group["manager"].append(name)

    if any(not paths for paths in group.values()):
        raise ValueError("A quality CI test group is empty")
    return {name: sorted(files) for name, files in group.items()}


def _docs_path(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return len(parts) >= 2 and parts[0] == "docs" and path.endswith(".md") and parts[-1] != "AGENTS.md"


def classify(event: str, changed: list[str]) -> tuple[str, ...]:
    if event != "pull_request" or not changed:
        return FULL
    if all(_docs_path(path) for path in changed):
        return DOCS_ONLY
    # These browser tests share manager fixtures, but no manager tests consume
    # them. Player code itself has other consumers and keeps all checks enabled.
    if all(path in PLAYER_TESTS or _docs_path(path) for path in changed):
        return PLAYER_ONLY
    return FULL


def _changed_files(root: Path, base: str, head: str) -> list[str]:
    sha = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})")
    if not sha.fullmatch(base) or not sha.fullmatch(head):
        return []
    try:
        result = subprocess.run(
            ["git", "diff", "--no-renames", "--name-only", "-z", f"{base}...{head}", "--"],
            cwd=root, check=True, capture_output=True,
        )
        return [path for path in result.stdout.decode("utf-8").split("\0") if path]
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError):
        return []


def _plan(root: Path, environment: dict[str, str]) -> None:
    groups = test_groups(root)
    event = environment.get("QUALITY_EVENT", "")
    changed = _changed_files(root, environment.get("QUALITY_BASE", ""), environment.get("QUALITY_HEAD", "")) if event == "pull_request" else []
    enabled = classify(event, changed)
    if enabled == PLAYER_ONLY and any(path not in groups["player"] for path in changed if path in PLAYER_TESTS):
        raise ValueError("A changed player test is missing from the player quality job")
    print("Quality jobs:", ", ".join(enabled) or "documentation only")
    with Path(environment["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for flag in JOB_FLAGS.values():
            print(f"{flag}={str(flag in enabled).lower()}", file=output)


def gate_errors(results: dict) -> list[str]:
    if set(results) != {"plan", *JOB_FLAGS} or results["plan"].get("result") != "success":
        return ["Quality plan failed or the workflow job results are incomplete."]
    outputs = results["plan"].get("outputs", {})
    if any(outputs.get(flag) not in {"true", "false"} for flag in JOB_FLAGS.values()):
        return ["Quality plan did not set every job flag."]
    enabled = tuple(flag for flag in JOB_FLAGS.values() if outputs[flag] == "true")
    if enabled not in {FULL, PLAYER_ONLY, DOCS_ONLY}:
        return ["Quality plan produced an unsupported job selection."]

    errors = []
    for job, flag in JOB_FLAGS.items():
        expected = "success" if flag in enabled else "skipped"
        actual = results[job].get("result")
        if actual != expected:
            errors.append(f"{job}: expected {expected}, got {actual}")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subcommands = parser.add_subparsers(dest="command", required=True)
    subcommands.add_parser("plan")
    subcommands.add_parser("gate")
    run = subcommands.add_parser("run")
    run.add_argument("group", choices=("manager", "browser-1", "browser-2", "player"))
    run.add_argument("pytest_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.command == "plan":
        _plan(ROOT, os.environ)
        return 0
    if args.command == "gate":
        errors = gate_errors(json.loads(os.environ["QUALITY_RESULTS"]))
        for error in errors:
            print(error, file=sys.stderr)
        return int(bool(errors))
    paths = test_groups(ROOT)[args.group]
    print(f"Running {len(paths)} files in {args.group}.", flush=True)
    return subprocess.run([sys.executable, "-m", "pytest", *args.pytest_args, *paths], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main())
