from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil

import pytest

from wechatrebot_manager import uninstaller, updates
from wechatrebot_manager.payload import PayloadError, sha256_file
from wechatrebot_manager.licensing.bundle import install_runtime_bundle


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


@pytest.fixture
def office_staging(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("office-stage")
    source, install = tmp_path / "source", tmp_path / "install"
    plugin = f"{PLUGINS}/astrbot_plugin_office_assistant"
    plugins = {"plugins": [{"name": "astrbot_plugin_office_assistant", "ownership": "owner"}]}
    _files(source, {
        "office/main.py": "# updated plugin\n",
        "office/word_renderer_js/src/index.ts": "renderer source",
        "manager/main.py": "manager source",
        "core.patch": "runtime patch",
        "release/source-manifest.json": json.dumps({"entries": [
            {"origin": "commercial", "source": "office", "target": plugin},
            {"origin": "commercial", "source": "manager", "target": "manager/src/wechatrebot_manager"},
            {"origin": "commercial", "source": "core.patch", "target": "legal/corresponding-source/core.patch"},
        ]}),
        "release/plugin-manifest.json": json.dumps(plugins),
    })
    _baseline(install, {
        f"{plugin}/main.py": "# old plugin\n",
        f"{plugin}/word_renderer_js/src/index.ts": "renderer source",
        f"{plugin}/word_renderer_js/dist/cli.js": "compiled renderer",
        f"{plugin}/word_renderer_js/node_modules/docx/index.js": "production dependency",
        "legal/plugin-manifest.json": json.dumps(plugins),
        "legal/corresponding-source/core.patch": "runtime patch",
        "manager/src/wechatrebot_manager/main.py": "manager source",
    })
    return source, install, plugin


@pytest.mark.parametrize("compiled", ["compiled renderer", "updated compiled renderer"])
def test_staged_office_delta_preserves_dependencies_and_updates_compiled_code(
    office_staging, tmp_path, monkeypatch, compiled,
):
    local = _script("update_local_incremental")
    builder = _script("build_incremental_update")
    import build_office_renderer

    source, install, plugin = office_staging
    candidate = tmp_path / "candidate"

    def compile_renderer(staged_plugin):
        assert staged_plugin == candidate / plugin
        assert (staged_plugin / "word_renderer_js/src/index.ts").read_text() == "renderer source"
        _files(staged_plugin, {
            "word_renderer_js/dist/cli.js": compiled,
            "word_renderer_js/node_modules/docx/index.js": "production dependency",
        })

    monkeypatch.setattr(build_office_renderer, "build_office_renderer", compile_renderer)
    scopes = local.stage_sources(source, source, install, candidate)
    report = builder.build_incremental_update(
        candidate, install, tmp_path / "incremental.zip", managed_roots=scopes, version="0.9.9",
    )
    assert report["removed_count"] == 0
    assert report["added_count"] == 0
    assert report["changed_count"] == (1 if compiled == "compiled renderer" else 2)
    assert (install / plugin / "word_renderer_js/dist/cli.js").read_text() == "compiled renderer"


def test_office_build_failure_aborts_local_update_before_packaging(office_staging, monkeypatch, capsys):
    local = _script("update_local_incremental")
    import build_office_renderer
    import build_incremental_update

    source, install, _plugin = office_staging
    monkeypatch.setattr(local, "ROOT", source)
    monkeypatch.setattr(local, "verify_source_inputs", lambda *_args: None)
    monkeypatch.setattr(local, "require_master", lambda *_args: None)
    monkeypatch.setattr(local, "verify_base_inputs", lambda *_args: None)
    monkeypatch.setattr(local, "manager_changed", lambda *_args: False)

    def failed_build(_plugin):
        raise RuntimeError("renderer build failed")

    def forbidden_write(*_args, **_kwargs):
        pytest.fail("A failed renderer build must not package or apply an update")

    monkeypatch.setattr(build_office_renderer, "build_office_renderer", failed_build)
    monkeypatch.setattr(build_incremental_update, "build_incremental_update", forbidden_write)
    monkeypatch.setattr(local, "apply_local", forbidden_write)
    assert local.main(["--install-root", str(install)]) == 1
    assert "renderer build failed" in capsys.readouterr().err
    assert not (install / "updates").exists()


def test_manager_only_staging_does_not_build_office_renderer(office_staging, tmp_path, monkeypatch):
    local = _script("update_local_incremental")
    import build_office_renderer

    source, install, plugin = office_staging

    def forbidden_build(_plugin):
        pytest.fail("Manager-only updates must not build the Office renderer")

    monkeypatch.setattr(build_office_renderer, "build_office_renderer", forbidden_build)
    candidate = tmp_path / "candidate"
    scopes = local.stage_sources(source, source, install, candidate, manager_only=True)
    assert scopes == ["manager/src/wechatrebot_manager"]
    assert not (candidate / plugin).exists()
    assert (candidate / "manager/src/wechatrebot_manager/main.py").read_text() == "manager source"


@pytest.mark.parametrize("enabled_at,problem", [
    ("source", None), ("source", "runtime_guard"),
    ("installed_manager", "runtime_guard"), ("installed_runtime", "runtime_guard"),
    ("source", "core_patch"), ("source", "runtime_manifest"),
])
def test_commercial_manager_only_requires_matching_authorization_runtime(
    tmp_path, monkeypatch, capsys, enabled_at, problem,
):
    local = _script("update_local_incremental")
    repository = Path(__file__).parents[1]
    source, install = tmp_path / "source", tmp_path / "install"
    package = source / "src/wechatrebot_manager"
    for name in local.RUNTIME_FILES:
        target = package / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(repository / "src/wechatrebot_manager" / name, target)
    channel = json.loads((package / "licensing-channel.json").read_text("utf-8"))
    channel["enabled"] = enabled_at == "source"
    (package / "licensing-channel.json").write_text(json.dumps(channel), encoding="utf-8")
    site_packages = install / "runtime/python/Lib/site-packages"
    fingerprint = install_runtime_bundle(source, site_packages)
    (source / "patches").mkdir()
    patch = "patches/astrbot-4.26.6-license.patch"
    shutil.copy2(repository / patch, source / patch)
    manifest = {"licensing_bundle_sha256": fingerprint,
                "astrbot_license_patch_sha256": sha256_file(source / patch)}
    manifest_path = install / "runtime/runtime-manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    if enabled_at != "source":
        destination = (install / "manager/src/wechatrebot_manager" if enabled_at == "installed_manager"
                       else site_packages / "wechatrebot_manager") / "licensing-channel.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps({**channel, "enabled": True}), encoding="utf-8")
    if problem == "runtime_guard":
        (site_packages / "wechatrebot_manager/licensing/runtime_guard.py").unlink()
    elif problem == "core_patch":
        manifest.pop("astrbot_license_patch_sha256")
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    elif problem == "runtime_manifest":
        manifest_path.unlink()
    _files(source, {"release/source-manifest.json": json.dumps({"entries": [
        {"origin": "commercial", "source": "src/wechatrebot_manager", "target": "manager/src/wechatrebot_manager"},
    ]})})
    monkeypatch.setattr(local, "ROOT", source)
    monkeypatch.setattr(local, "require_master", lambda _root: None)
    monkeypatch.setattr(local, "manager_changed", lambda *_args: True)

    def forbidden_base_scan(*_args):
        pytest.fail("Authorization validation must not scan plugins, Python or FFmpeg")

    monkeypatch.setattr(local, "verify_base_inputs", forbidden_base_scan)
    assert local.main(["--install-root", str(install), "--manager-only", "--plan"]) == (1 if problem else 0)
    output = capsys.readouterr()
    if problem:
        assert output.err and not output.out
    else:
        assert json.loads(output.out)["runtime_build_required"] is False
    assert not (source / "artifacts").exists()


def test_cleanup_refuses_unrelated_directory(tmp_path):
    local = _script("update_local_incremental")
    working = tmp_path / "run"
    outside = tmp_path / "unrelated"
    _files(outside, {"user.txt": "keep"})
    with pytest.raises(RuntimeError, match="拒绝清理"):
        local.remove_working_directory(outside, working)
    assert (outside / "user.txt").read_text() == "keep"
