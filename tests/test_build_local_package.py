import importlib.util
import json
from pathlib import Path
import sys
import zipfile
from types import SimpleNamespace

import pytest


def builder():
    path = Path(__file__).parents[1] / "scripts" / "build_local_package.py"
    spec = importlib.util.spec_from_file_location("build_local_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_stage_stops_and_keeps_diagnostic_log(tmp_path):
    module = builder()
    with pytest.raises(RuntimeError, match="exit 7"):
        module.run_step("broken", [sys.executable, "-c", "print('diagnostic'); exit(7)"], tmp_path)
    assert "diagnostic" in (tmp_path / "broken.log").read_text()


def test_local_plan_has_no_publish_or_service_commands(tmp_path):
    steps = builder().build_steps(tmp_path / "source with spaces", tmp_path / "output")
    assert [Path(command[1]).name for _, command in steps] == [
        "build_manager.py", "build_runtime.py", "build_staging.py",
        "build_distribution.py", "build_native_installer.py",
    ]
    installer = steps[-1][1]
    assert "--payload" in installer
    assert "--payload-url" not in installer
    assert str(tmp_path / "source with spaces") in steps[2][1]


def test_customer_zip_contains_native_installer_and_checksum(tmp_path):
    module = builder()
    installer = tmp_path / "installer" / "WeChatReBot-Setup-0.9.9.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"test installer")
    archive = module.finish_package(tmp_path, "0.9.9")
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {installer.name, "安装说明.txt"}
        assert package.read(installer.name) == installer.read_bytes()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["uploaded"] is False
    assert result["services_started"] is False
    assert result["sha256"] in archive.with_suffix(".zip.sha256").read_text()


def test_uses_verified_linked_worktree_cache_and_passes_it_to_runtime(tmp_path, monkeypatch):
    module = builder()
    from build_runtime import _wheel_manifest, _wheelhouse_summary
    linked = tmp_path / "linked tree"
    suffix = "artifacts/runtime-cache/wheels-py313-win-amd64"
    good = linked / suffix
    good.mkdir(parents=True)
    (good / "dependency.whl").write_bytes(b"approved wheel")
    current = tmp_path / "current"
    bad = current / suffix
    bad.mkdir(parents=True)
    (bad / "dependency.whl").write_bytes(b"recompiled wheel")
    (current / "release").mkdir()
    (current / "release/runtime-wheelhouse.json").write_text(json.dumps(_wheelhouse_summary(_wheel_manifest(good))))
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=f"worktree {linked}\n"))
    assert module.select_wheelhouse(current) == good
    runtime = module.build_steps(tmp_path, tmp_path / "out", good)[1][1]
    assert runtime[runtime.index("--wheelhouse") + 1] == str(good)
    assert (bad / "dependency.whl").read_bytes() == b"recompiled wheel"


def test_missing_approved_cache_fails_before_building(tmp_path, monkeypatch):
    module = builder()
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=""))
    with pytest.raises(RuntimeError, match="No dependency cache matches"):
        module.select_wheelhouse(tmp_path)


def test_cached_plan_rebuilds_manager_and_plugins_but_skips_runtime(tmp_path):
    module = builder()
    runtime = tmp_path / "cached runtime"
    python = tmp_path / "cached build/python.exe"
    steps = module.build_steps(tmp_path / "source", tmp_path / "out", runtime=runtime, build_python=python)
    assert [name for name, _ in steps] == ["1-manager", "3-staging", "4-payload", "5-installer"]
    manager = steps[0][1]
    assert manager[manager.index("--build-python") + 1] == str(python)
    staging = steps[1][1]
    assert staging[staging.index("--runtime-root") + 1] == str(runtime)


def test_runtime_cache_invalidated_by_command_patch_change(tmp_path, monkeypatch):
    module = builder()
    import build_staging
    from build_runtime import sha256_file
    inputs = {
        "requirements_sha256": "release/runtime-requirements.lock",
        "astrbot_patch_sha256": "patches/astrbot-4.26.6-wechat.patch",
        "astrbot_outbound_policy_patch_sha256": "patches/astrbot-4.26.6-outbound-policy.patch",
    }
    manifest = {}
    for key, relative in inputs.items():
        path = tmp_path / relative
        path.parent.mkdir(exist_ok=True)
        path.write_text(relative)
        manifest[key] = sha256_file(path)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "runtime-manifest.json").write_text(json.dumps(manifest))
    monkeypatch.setattr(build_staging, "_validate_runtime_root", lambda *_: {})
    assert module.runtime_matches(tmp_path, runtime)
    (tmp_path / inputs["astrbot_outbound_policy_patch_sha256"]).write_text("new command permission fix")
    assert not module.runtime_matches(tmp_path, runtime)


def test_partial_runtime_is_never_reused(tmp_path):
    assert not builder().runtime_matches(tmp_path, tmp_path / "unfinished")
