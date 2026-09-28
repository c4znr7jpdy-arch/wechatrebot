from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from wechatrebot_manager import uninstaller, updates
from wechatrebot_manager.payload import PayloadError, sha256_file


PLUGINS = "app/astrbot/data/plugins"


def _script(name: str):
    path = Path(__file__).parents[1] / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_local_test", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(root: Path, contents: dict[str, str]) -> None:
    for relative, content in contents.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")


def _baseline(root: Path, contents: dict[str, str]) -> None:
    _files(root, contents)
    _files(root, {
        "config/installed-payload.json": json.dumps({
            "schema_version": 1,
            "release_version": "0.9.9",
            "files": [
                {"path": relative, "sha256": sha256_file(root / relative)}
                for relative in contents
            ],
        }),
        ".wechatrebot-install.json": json.dumps({"schema_version": 1, "state": "ready"}),
    })


def test_local_delta_applies_changed_new_and_retired_plugins_without_starting_apps(tmp_path, monkeypatch):
    local = _script("update_local_incremental")
    builder = _script("build_incremental_update")
    install, candidate = tmp_path / "install", tmp_path / "candidate"
    protected = {
        "config/manager.json": '{"my-setting": true}',
        "app/astrbot/data/cmd_config.json": '{"my-command": "custom"}',
        "app/astrbot/data/config/retired_config.json": '{"my-secret": "fixture-only"}',
        "app/astrbot/data/database.db": "user conversations",
        "runtime/python/python.exe": "unchanged Python",
        "manager/WeChatReBot.exe": "unchanged manager",
    }
    _baseline(install, {
        f"{PLUGINS}/existing/main.py": "old plugin",
        f"{PLUGINS}/existing/unused.png": "obsolete image",
        f"{PLUGINS}/retired/main.py": "removed plugin",
        "legal/runtime-requirements.lock": "httpx==0.28.1\n",
        "runtime/runtime-manifest.json": json.dumps({
            "schema_version": 1, "python": {"version": "3.13.9", "architecture": "x64"},
        }),
        **protected,
    })
    # The application's own edits after installation must neither invalidate the
    # code baseline nor be replaced by defaults during a plugin-only update.
    protected["app/astrbot/data/cmd_config.json"] = '{"my-command": "edited later"}'
    protected[f"{PLUGINS}/retired/local-cache.json"] = "unowned local data"
    _files(install, protected)
    _files(candidate, {
        f"{PLUGINS}/existing/main.py": "updated plugin",
        f"{PLUGINS}/new_plugin/main.py": "import httpx",
        f"{PLUGINS}/new_plugin/requirements.txt": "httpx>=0.28,<1\n",
    })
    (candidate / PLUGINS / "retired").mkdir(parents=True)
    archive = tmp_path / "incremental.zip"
    report = builder.build_incremental_update(
        candidate, install, archive,
        managed_roots=[f"{PLUGINS}/existing", f"{PLUGINS}/new_plugin", f"{PLUGINS}/retired"],
        version="0.9.9", build=19,
    )
    assert report["changed_count"] == 1
    assert report["added_count"] == 2
    assert report["removed_count"] == 2

    guard_calls = []
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *args: guard_calls.append(args))
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda: ())

    def forbidden_start(*_args, **_kwargs):
        pytest.fail("A local incremental update must not start any application")

    monkeypatch.setattr(updates, "_restart_manager", forbidden_start)
    local.apply_local(archive, install)

    assert guard_calls  # Retain real transactional application, only stub OS discovery.
    assert (install / PLUGINS / "existing/main.py").read_text() == "updated plugin"
    assert (install / PLUGINS / "new_plugin/main.py").read_text() == "import httpx"
    assert not (install / PLUGINS / "existing/unused.png").exists()
    assert not (install / PLUGINS / "retired/main.py").exists()
    for relative, content in protected.items():
        assert (install / relative).read_text(encoding="utf-8") == content
    manifest = json.loads((install / "config/installed-payload.json").read_text("utf-8"))
    managed = {item["path"]: item["sha256"] for item in manifest["files"]}
    assert managed[f"{PLUGINS}/existing/main.py"] == sha256_file(install / PLUGINS / "existing/main.py")
    assert f"{PLUGINS}/new_plugin/main.py" in managed
    assert f"{PLUGINS}/retired/main.py" not in managed
    assert f"{PLUGINS}/retired/local-cache.json" not in managed
    assert json.loads((install / "updates/update-result.json").read_text("utf-8"))["ok"] is True
    assert not (install / "updates/update-journal.json").exists()
    assert not list((install / "updates").glob(".rollback-*"))
    assert not list((install / "updates").glob("local-*"))


def test_local_apply_refuses_changed_baseline_before_extracting_or_writing(tmp_path, monkeypatch):
    local = _script("update_local_incremental")
    builder = _script("build_incremental_update")
    install, candidate = tmp_path / "install", tmp_path / "candidate"
    target = f"{PLUGINS}/existing/main.py"
    _baseline(install, {target: "old"})
    _files(candidate, {target: "new"})
    archive = tmp_path / "incremental.zip"
    builder.build_incremental_update(candidate, install, archive, managed_roots=[f"{PLUGINS}/existing"])
    _files(install, {target: "user edited after package creation"})
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda: ())

    with pytest.raises(PayloadError, match="现有文件"):
        local.apply_local(archive, install)

    assert (install / target).read_text() == "user edited after package creation"
    assert not (install / "updates").exists()


@pytest.mark.parametrize("missing", [False, True])
def test_base_contract_changes_are_rejected_before_staging(tmp_path, missing):
    local = _script("update_local_incremental")
    runtime = _script("build_runtime")
    source, install = tmp_path / "source", tmp_path / "install"
    for source_name, installed_name in local.BASE_INPUTS.items():
        _files(source, {source_name: "same base"})
        _files(install, {installed_name: "same base"})
    _files(install, {"runtime/runtime-manifest.json": json.dumps({"python": {
        "version": runtime.PYTHON_VERSION, "sha256": runtime.PYTHON_SHA256,
    }})})
    local.verify_base_inputs(source, install)
    source_name, installed_name = next(iter(local.BASE_INPUTS.items()))
    if missing:
        (install / installed_name).unlink()
    else:
        _files(source, {source_name: "different base"})

    with pytest.raises(RuntimeError, match="底层组件基线不兼容"):
        local.verify_base_inputs(source, install)

    assert not (source / "artifacts").exists()
    assert not (install / "updates").exists()


def test_source_scopes_preserve_user_configuration_and_retire_only_owned_plugins(tmp_path):
    local = _script("update_local_incremental")
    source, install = tmp_path / "source", tmp_path / "install"
    targets = [
        "manager/src/wechatrebot_manager",
        f"{PLUGINS}/active",
        f"{PLUGINS}/active/main.py",
        "app/astrbot/data/config/active_config.json",
        "app/astrbot/data/cmd_config.json",
        "config/manager.json",
        "config/templates/manager.example.json",
    ]
    entries = [{"origin": "commercial", "source": f"fixture/{index}", "target": target}
               for index, target in enumerate(targets)]
    _files(source, {
        "release/source-manifest.json": json.dumps({"entries": entries}),
        "release/plugin-manifest.json": json.dumps({"plugins": [
            {"name": "active", "ownership": "owner"},
            {"name": "newly_excluded", "ownership": "excluded"},
            {"name": "never_owned", "ownership": "excluded"},
        ]}),
    })
    _files(install, {"legal/plugin-manifest.json": json.dumps({"plugins": [
        {"name": "active", "ownership": "owner"},
        {"name": "removed", "ownership": "third-party"},
        {"name": "newly_excluded", "ownership": "owner"},
        {"name": "never_owned", "ownership": "excluded"},
    ]})})

    selected, scopes = local.source_scopes(source, install)

    selected_targets = {entry["target"] for entry in selected}
    assert "app/astrbot/data/config/active_config.json" not in selected_targets
    assert "app/astrbot/data/cmd_config.json" not in selected_targets
    assert "config/manager.json" not in selected_targets
    assert "config/templates/manager.example.json" in scopes
    assert f"{PLUGINS}/active" in scopes
    assert f"{PLUGINS}/active/main.py" not in scopes
    assert f"{PLUGINS}/removed" in scopes
    assert f"{PLUGINS}/newly_excluded" in scopes
    assert f"{PLUGINS}/never_owned" not in scopes


def test_manager_only_plan_does_not_access_plugin_or_base_inputs(tmp_path, monkeypatch, capsys):
    local = _script("update_local_incremental")
    source, install = tmp_path / "source", tmp_path / "install"
    _files(source, {"release/source-manifest.json": json.dumps({"entries": [
        {"origin": "commercial", "source": "src/manager", "target": "manager/src/wechatrebot_manager"},
        {"origin": "commercial", "source": "build.lock", "target": "legal/build-requirements.lock"},
        {"origin": "project", "source": "not-present/plugin", "target": f"{PLUGINS}/not_present"},
        {"origin": "project", "source": "not-present/hook.dll", "target": "app/bridge/vendor/hook.dll"},
    ]})})
    # Neither plugin manifests nor runtime baseline files exist: reading them is
    # a failure, even though a manager-only preview is valid.
    monkeypatch.setattr(local, "ROOT", source)
    monkeypatch.setattr(local, "require_master", lambda _root: None)
    monkeypatch.setattr(local, "manager_changed", lambda *_args: True)

    def forbidden_base_scan(*_args):
        pytest.fail("Manager-only mode must not scan the plugin/runtime base")

    monkeypatch.setattr(local, "verify_base_inputs", forbidden_base_scan)
    assert local.main(["--install-root", str(install), "--manager-only", "--plan"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["runtime_build_required"] is False
    assert "manager/src/wechatrebot_manager" in result["components"]
    assert not any(scope.startswith("app/") for scope in result["components"])
    assert not (source / "artifacts").exists()
    assert not install.exists()


def test_cleanup_refuses_unrelated_directory(tmp_path):
    local = _script("update_local_incremental")
    working = tmp_path / "run"
    outside = tmp_path / "unrelated"
    _files(outside, {"user.txt": "keep"})
    with pytest.raises(RuntimeError, match="拒绝清理"):
        local.remove_working_directory(outside, working)
    assert (outside / "user.txt").read_text() == "keep"
