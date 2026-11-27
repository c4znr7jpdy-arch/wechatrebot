"""Synthetic signed packages exercise environment reuse and durable transitions."""
import copy
import hashlib
import io
import json
from pathlib import Path
import shutil
import sys
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from build_runtime_packages import build_environment
from build_update_release import _sign_manifest
from test_payload import _payload_files
from wechatrebot_manager import launcher, payload, runtime_deployment, runtime_install, update_download
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.runtime_manifest import (
    ENGINE_PATH, ENVIRONMENT_MANIFEST, ENVIRONMENT_RECEIPT, REQUIRED_RUNTIME_FILES, parse_environment,
)
from wechatrebot_manager.update_manifest import UpdateChannel, UpdateError
from wechatrebot_manager.update_errors import UpdateFailure
from wechatrebot_manager.update_rollback import UpdateFileBackup, snapshot_update_targets


@pytest.fixture
def environment_factory(tmp_path, monkeypatch):
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    channel = UpdateChannel("https://example.test/stable.json", "stable", hashlib.sha256(public).hexdigest().upper()[:16], public)
    archive_map, requests = {}, []
    counter = 0

    class Response(io.BytesIO):
        def __init__(self, data, url):
            super().__init__(data)
            self.headers, self.url = {"Content-Length": str(len(data))}, url

        def geturl(self):
            return self.url

    def fetch(request, **kwargs):
        requests.append(request.full_url)
        return Response(archive_map[request.full_url].read_bytes(), request.full_url)

    monkeypatch.setattr(update_download.urllib.request, "urlopen", fetch)
    monkeypatch.setattr(launcher, "installation_services_stopped", lambda _root: True)

    def build(extra=None, previous=None):
        nonlocal counter
        counter += 1
        source = tmp_path / f"source-{counter}"
        contents = {"python/python.exe": b"python", "python/python313._pth": b"Lib/site-packages\nimport site\n",
                    "ffmpeg/bin/ffmpeg.exe": b"ffmpeg", "ffmpeg/bin/ffprobe.exe": b"ffprobe",
                    "python/Lib/site-packages/alpha/__init__.py": b"alpha"}
        contents.update(extra or {})
        for name, data in contents.items():
            if data is not None:
                target = source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
        env, assets = build_environment(source, tmp_path / f"output-{counter}", channel=channel, private_key=private,
            prefix=f"https://example.test/v{counter}", python_abi="cp313", metadata={}, previous=previous)
        for asset in assets:
            archive_map[f"https://example.test/v{counter}/{asset.name}"] = asset
        return env, assets, source

    build.channel, build.private, build.requests = channel, private, requests
    build.archive_map = archive_map
    return build


def _application(path, environment, version="1.4.0", extra=None):
    files = {"app/engine/python/astrbot/__init__.py": b"app core", "manager/WeChatReBot.exe": b"manager",
             ENVIRONMENT_MANIFEST.as_posix(): json.dumps(environment.manifest).encode()}
    files.update(extra or {})
    _payload_files(path, files)
    manifest = json.loads((path / payload.PAYLOAD_MANIFEST).read_text())
    manifest.update(application_package=1, release_version=version)
    write_json_atomic(path / payload.PAYLOAD_MANIFEST, manifest)
    return path


def test_first_install_then_reuse_downloads_nothing(environment_factory, tmp_path):
    env, assets, _ = environment_factory()
    root = tmp_path / "install"
    root.mkdir()
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    runtime_install.install_environment(root, environment_factory.channel)
    assert len(environment_factory.requests) == len(assets)
    assert runtime_deployment.missing_packages(env, root) == ()
    environment_factory.requests.clear()
    runtime_install.install_environment(root, environment_factory.channel)
    assert not environment_factory.requests
    assert not (root / runtime_install.ENVIRONMENT_JOURNAL).exists()


def test_new_dependency_reuses_old_assets_and_only_downloads_added_package(environment_factory, tmp_path):
    env, _, source = environment_factory()
    new, assets, _ = environment_factory({"python/Lib/site-packages/beta/__init__.py": b"beta"}, previous=env)
    assert len(assets) == 1
    assert {p.url for p in env.packages}.issubset({p.url for p in new.packages})
    root = tmp_path / "install"
    shutil.copytree(source, root / "runtime")
    write_json_atomic(root / ENVIRONMENT_MANIFEST, new.manifest)
    runtime_install.install_environment(root, environment_factory.channel)
    assert len(environment_factory.requests) == 1
    assert runtime_deployment.missing_packages(new, root) == ()
    installed = json.loads((root / "config/installed-payload.json").read_text())
    assert {item.path for item in new.files}.issubset({item["path"] for item in installed["files"]})


def test_content_packages_deterministic_and_exclude_business_code(environment_factory):
    first, archives, _ = environment_factory({"python/Lib/site-packages/astrbot/__init__.py": b"old engine",
                                            "python/Lib/site-packages/wechatrebot_manager/__init__.py": b"old auth"})
    second, other, _ = environment_factory()
    assert first.identity == second.identity
    assert [payload.sha256_file(p) for p in archives] == [payload.sha256_file(p) for p in other]
    assert all("astrbot" not in f.path and "wechatrebot_manager" not in f.path for f in first.files)


def test_skip_versions_removes_only_owned_old_files_and_keeps_user_data(environment_factory, tmp_path):
    env, _, source = environment_factory()
    root = tmp_path / "install"
    shutil.copytree(source, root / "runtime")
    old = _payload_files(tmp_path / "old", {"app/obsolete.py": b"obsolete",
        "runtime/python/Lib/site-packages/astrbot/__init__.py": b"old core",
        "config/user.json": b"user config", "app/astrbot/data/database.db": b"user db"})
    payload.install_payload(old, root)
    app = _application(tmp_path / "app", env)
    staging = runtime_deployment.prepare_deployment(app, root, root / "updates", environment_factory.channel)
    assert not environment_factory.requests
    payload.install_payload(staging, root)
    assert not (root / "app/obsolete.py").exists()
    assert not (root / "runtime/python/Lib/site-packages/astrbot/__init__.py").exists()
    assert (root / "config/user.json").read_bytes() == b"user config"
    assert (root / "app/astrbot/data/database.db").read_bytes() == b"user db"
    assert json.loads((root / "config/installed-payload.json").read_text())["release_version"] == "1.4.0"


def test_reused_file_changed_after_preparation_blocks_install(environment_factory, tmp_path, monkeypatch):
    env, _, source = environment_factory()
    root = tmp_path / "install"
    shutil.copytree(source, root / "runtime")
    staging = runtime_deployment.prepare_deployment(_application(tmp_path / "app", env), root,
                                                   root / "updates", environment_factory.channel)
    verified = payload.verify_payload(staging)
    (root / "runtime/python/python.exe").write_bytes(b"changed")
    with pytest.raises(UpdateError, match="准备后改变"):
        runtime_deployment.verify_deployment_environment(staging, root, environment_factory.channel)

    def unexpected_hash(_path):
        pytest.fail("deployment metadata check reread file content")

    with monkeypatch.context() as checks:
        checks.setattr(payload, "sha256_file", unexpected_hash)
        checks.setattr(runtime_deployment, "sha256_file", unexpected_hash)
        runtime_deployment.verify_deployment_environment(
            staging, root, environment_factory.channel, verified_payload=verified, check_installed=False,
        )
    with pytest.raises(payload.PayloadError, match="复用的运行环境文件已改变"):
        payload.install_payload(staging, root, verified_payload=verified)
    assert not (root / "manager/WeChatReBot.exe").exists()


def test_staged_runtime_file_must_match_signed_environment(environment_factory, tmp_path):
    env, _, _ = environment_factory()
    root = tmp_path / "install"
    root.mkdir()
    staging = runtime_deployment.prepare_deployment(_application(tmp_path / "app", env), root,
                                                   root / "updates", environment_factory.channel)
    relative = "runtime/python/python.exe"
    (staging / relative).write_bytes(b"forged")
    manifest_path = staging / payload.PAYLOAD_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = next(item for item in manifest["files"] if item["path"] == relative)
    entry["sha256"] = payload.sha256_file(staging / relative)
    write_json_atomic(manifest_path, manifest)
    verified = payload.verify_payload(staging)

    with pytest.raises(UpdateError, match="准备后改变"):
        runtime_deployment.verify_deployment_environment(
            staging, root, environment_factory.channel, verified_payload=verified, check_installed=False,
        )


def test_install_failure_restores_environment_and_allows_cached_retry(environment_factory, tmp_path, monkeypatch):
    env, _, _ = environment_factory()
    root = tmp_path / "install"
    root.mkdir()
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    target = root / "runtime/python/python.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"previous python")
    original = payload._replace_file

    def fail(source, destination):
        if Path(destination) == root / "config/installed-payload.json":
            raise OSError("injected write failure")
        return original(source, destination)

    monkeypatch.setattr(payload, "_replace_file", fail)
    with pytest.raises((OSError, payload.PayloadError), match="injected write failure"):
        runtime_install.install_environment(root, environment_factory.channel)
    assert target.read_bytes() == b"previous python"
    assert not (root / "runtime/ffmpeg/bin/ffmpeg.exe").exists()
    monkeypatch.setattr(payload, "_replace_file", original)
    environment_factory.requests.clear()
    runtime_install.install_environment(root, environment_factory.channel)
    assert not environment_factory.requests
    assert target.read_bytes() == b"python"


def test_crash_recovery_validates_paths_before_mutation(environment_factory, tmp_path):
    env, _, _ = environment_factory()
    root = tmp_path / "install"
    root.mkdir()
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    staging = runtime_deployment.prepare_deployment(None, root, root / "updates", environment_factory.channel)
    verified = payload.verify_payload(staging)
    journal = snapshot_update_targets(staging, root, root / "updates/.environment-rollback-test",
                                     update_id="test", version="environment", verified_payload=verified)
    journal["environment_transaction"] = 1
    write_json_atomic(root / runtime_install.ENVIRONMENT_JOURNAL, journal)
    owner = UpdateFileBackup(journal, root)
    first = verified.files[0]
    target = root / first.relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    owner.retain(target, first.source_path)
    shutil.copy2(first.source_path, target)
    broken = {**journal, "rollback_root": "updates/other"}
    write_json_atomic(root / runtime_install.ENVIRONMENT_JOURNAL, broken)
    with pytest.raises(UpdateError, match="回滚路径"):
        runtime_install.recover_environment(root)
    assert target.exists()
    write_json_atomic(root / runtime_install.ENVIRONMENT_JOURNAL, journal)
    runtime_install.recover_environment(root)
    assert not target.exists()
    runtime_install.recover_environment(root)


@pytest.mark.parametrize("change", ["signature", "platform", "abi", "traversal", "duplicate", "business", "size", "missing"])
def test_invalid_environment_is_rejected(environment_factory, change):
    env, _, _ = environment_factory()
    manifest = copy.deepcopy(env.manifest)
    if change == "platform":
        manifest["platform"] = "windows-arm64"
    elif change == "abi":
        manifest["python_abi"] = "unknown"
    elif change == "duplicate":
        manifest["packages"].append(manifest["packages"][0])
    elif change == "missing":
        manifest["packages"] = manifest["packages"][:1]
    elif change == "size":
        manifest["packages"][0]["files"][0]["size"] = -1
    else:
        manifest["packages"][0]["files"][0]["path"] = (
            "runtime/python/Lib/site-packages/astrbot/bad.py" if change == "business" else "runtime/../../bad")
    if change != "signature":
        _sign_manifest(manifest, environment_factory.private)
    with pytest.raises((UpdateError, payload.PayloadError)):
        parse_environment(manifest, environment_factory.channel)


def test_corrupt_download_keeps_existing_installation(environment_factory, tmp_path):
    env, _, _ = environment_factory()
    next(iter(environment_factory.archive_map.values())).write_bytes(b"corrupt archive")
    root = tmp_path / "install"
    root.mkdir()
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    with pytest.raises(UpdateError):
        runtime_install.install_environment(root, environment_factory.channel)
    assert not (root / "runtime/python/python.exe").exists()


def test_deleted_dependency_files_are_removed_on_next_application(environment_factory, tmp_path):
    old, _, _ = environment_factory({"python/Lib/site-packages/alpha/obsolete.py": b"old"})
    root = tmp_path / "install"
    root.mkdir()
    write_json_atomic(root / ENVIRONMENT_MANIFEST, old.manifest)
    runtime_install.install_environment(root, environment_factory.channel)
    new, assets, _ = environment_factory(previous=old)
    assert len(assets) == 1
    staging = runtime_deployment.prepare_deployment(_application(tmp_path / "app", new), root,
                                                   root / "updates", environment_factory.channel)
    payload.install_payload(staging, root)
    assert not (root / "runtime/python/Lib/site-packages/alpha/obsolete.py").exists()


def test_runtime_api_blocks_start_and_license_connection_during_install(tmp_path):
    from test_app_shell import _api
    api = _api(tmp_path)
    api._runtime_status = {"state": "missing", "ready": False}
    for action in ("start", "connect_wechat", "resume_pending_start"):
        assert api._queue_robot_action(action, "starting")["data"]["accepted"] is False
    assert not api._action_lock.locked()


def test_completed_environment_is_ready_on_reopen_and_license_refresh_without_file_scan(
        environment_factory, tmp_path, monkeypatch):
    from test_app_shell import _api
    from wechatrebot_manager import runtime_api

    env, _, _ = environment_factory()
    root = tmp_path / "install"
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    runtime_install.install_environment(root, environment_factory.channel)
    environment_factory.requests.clear()

    def unexpected(*_args, **_kwargs):
        pytest.fail("opening the manager or refreshing a license must not rescan runtime files")

    monkeypatch.setattr(runtime_deployment, "sha256_file", unexpected)
    monkeypatch.setattr(payload, "sha256_file", unexpected)
    monkeypatch.setattr(runtime_api.threading.Thread, "start", unexpected)
    for _ in range(2):
        api = _api(root)
        api._update_channel = environment_factory.channel
        api._authorized_history_account = lambda account: account
        api._license_account = lambda: ""
        api._licenses = SimpleNamespace(refresh=lambda: None, snapshot=lambda _account: {"authorized": True})
        api._lifecycle.refresh = lambda: SimpleNamespace(to_dict=lambda: {"can_start": True})
        for refresh_license in (False, True):
            if refresh_license:
                assert api.refresh_license()["ok"] is True
            status = api.get_status()["data"]
            assert status["runtime_environment"]["state"] == "ready"
            assert status["runtime_environment"]["ready"] is True
            assert status["can_start"] is True
    assert not environment_factory.requests


def test_runtime_first_install_uses_existing_transaction_then_reopens_ready(environment_factory, tmp_path, monkeypatch):
    from test_app_shell import _api
    from wechatrebot_manager import runtime_api

    env, assets, _ = environment_factory()
    root = tmp_path / "install"
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    api = _api(root)
    api._update_channel = environment_factory.channel
    api._lifecycle.maintenance_stopped = lambda: True
    assert api._runtime_environment_status()["state"] == "missing"
    assert not environment_factory.requests
    monkeypatch.setattr(runtime_api.threading, "Thread", lambda *, target, daemon: SimpleNamespace(start=target))
    assert api.install_runtime_environment()["data"]["accepted"] is True
    assert api._runtime_environment_status()["ready"] is True
    assert not api._action_lock.locked() and not api._update_lock.locked()
    assert len(environment_factory.requests) == len(assets)
    reopened = _api(root)
    reopened._update_channel = environment_factory.channel
    assert reopened._runtime_environment_status()["ready"] is True


@pytest.mark.parametrize("change", ["missing", "invalid_json", "invalid_utf8", "list", "schema", "boolean_schema", "id",
                                  "journal", "oversized", "missing_manifest", *sorted(REQUIRED_RUNTIME_FILES)])
def test_unfinished_or_changed_environment_is_not_marked_ready(environment_factory, tmp_path, change):
    from test_app_shell import _api

    env, _, source = environment_factory()
    root = tmp_path / "install"
    shutil.copytree(source, root / "runtime")
    (root / ENGINE_PATH).mkdir(parents=True)
    write_json_atomic(root / ENVIRONMENT_MANIFEST, env.manifest)
    receipt_path = root / ENVIRONMENT_RECEIPT
    write_json_atomic(receipt_path, {"schema_version": 1, "id": env.identity})
    if change == "missing":
        receipt_path.unlink()
    elif change == "invalid_json":
        receipt_path.write_text("{")
    elif change == "invalid_utf8":
        receipt_path.write_bytes(b"\xff")
    elif change == "list":
        write_json_atomic(receipt_path, [])
    elif change in {"schema", "boolean_schema", "id"}:
        write_json_atomic(receipt_path, {"schema_version": {"schema": 2, "boolean_schema": True}.get(change, 1),
                                        "id": "old environment" if change == "id" else env.identity})
    elif change == "journal":
        write_json_atomic(root / runtime_install.ENVIRONMENT_JOURNAL, {"environment_transaction": 1})
    elif change == "oversized":
        receipt_path.write_bytes(b" " * 4097)
    elif change == "missing_manifest":
        (root / ENVIRONMENT_MANIFEST).unlink()
    else:
        (root / change).unlink()
    api = _api(root)
    api._update_channel = environment_factory.channel
    status = api._runtime_environment_status()
    assert status["state"] in {"missing", "failed"}
    assert status["ready"] is False
    assert not environment_factory.requests


def test_legacy_runtime_status_does_not_require_receipt_but_blocks_interrupted_install(tmp_path):
    from test_app_shell import _api

    assert _api(tmp_path)._runtime_environment_status() == {"state": "legacy", "ready": True}
    write_json_atomic(tmp_path / runtime_install.ENVIRONMENT_JOURNAL, {"environment_transaction": 1})
    status = _api(tmp_path)._runtime_environment_status()
    assert status["state"] == "failed" and status["ready"] is False


def test_runtime_install_worker_start_failure_releases_locks_and_can_retry(tmp_path, monkeypatch):
    from test_app_shell import _api
    from wechatrebot_manager import runtime_api
    api = _api(tmp_path)
    api._runtime_status = {"state": "missing", "ready": False}
    api._lifecycle.maintenance_stopped = lambda: True

    def fail(_thread):
        raise RuntimeError("cannot start worker")

    monkeypatch.setattr(runtime_api.threading.Thread, "start", fail)
    assert api.install_runtime_environment()["ok"] is False
    assert api._runtime_status["state"] == "failed"
    assert not api._action_lock.locked() and not api._update_lock.locked()


def test_application_and_environment_share_process_lock(environment_factory, tmp_path):
    from wechatrebot_manager.single_instance import SingleInstanceLock
    root = tmp_path / "install"
    root.mkdir()
    with SingleInstanceLock(f"update-helper:{root}"):
        with pytest.raises(UpdateError, match="正在更新"):
            runtime_install.install_environment(root, environment_factory.channel)


@pytest.mark.parametrize("fail", [False, True])
def test_application_prepare_apply_switches_environment_in_same_transaction(environment_factory, tmp_path, monkeypatch, fail):
    from build_update_release import _write_payload_archive
    from wechatrebot_manager import updates
    env, _, _ = environment_factory()
    app = _application(tmp_path / "app", env, extra={"app/astrbot/data/config/example.json": b'{"default":true}'})
    verified = payload.verify_payload(app)
    archive = tmp_path / "update.zip"
    _write_payload_archive(verified, verified.files, archive, version="1.4.0", build=None)
    environment_factory.archive_map["https://example.test/application.zip"] = archive
    root = tmp_path / "install"
    (root / "manager").mkdir(parents=True)
    (root / "manager/WeChatReBot.exe").write_bytes(b"old manager")
    write_json_atomic(root / ".wechatrebot-install.json", {"schema_version": 1, "state": "ready"})
    release = updates.UpdateRelease("1.4.0", "app", (), "", "https://example.test/application.zip",
                                    archive.stat().st_size, payload.sha256_file(archive), False)
    prepared = updates.prepare_update(release, environment_factory.channel, root)
    assert (root / "manager/WeChatReBot.exe").read_bytes() == b"old manager"
    assert not (root / "runtime/python/python.exe").exists()
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    monkeypatch.setattr(updates, "load_update_channel", lambda *_: environment_factory.channel)
    plan = updates.create_update_plan(prepared, root, executable=root / "manager/WeChatReBot.exe",
                                      parent_pid=0, automatic_install=True)
    if fail:
        original = payload._replace_file

        def failing_replace(source, target):
            if Path(target) == root / "runtime/python/python.exe":
                raise OSError("injected runtime write failure")
            return original(source, target)

        monkeypatch.setattr(payload, "_replace_file", failing_replace)
    result = updates.apply_update_plan(plan.plan_path, restart=False, before_wait=lambda: None)
    assert result == (1 if fail else 0)
    assert (root / "manager/WeChatReBot.exe").read_bytes() == (b"old manager" if fail else b"manager")
    assert (root / "runtime/python/python.exe").exists() is not fail
    if fail:
        assert not (root / "app/engine/python/astrbot/__init__.py").exists()


def test_new_engine_and_bridge_import_application_code_with_isolated_python(tmp_path):
    import subprocess
    from test_launcher import _layout
    _layout(tmp_path)
    engine = tmp_path / "app/engine/python"
    (engine / "astrbot/cli").mkdir(parents=True)
    for name in ("astrbot/__init__.py", "astrbot/cli/__init__.py"):
        (engine / name).write_text("")
    (engine / "shared_auth.py").write_text("VALUE = 'application auth'\n")
    (engine / "astrbot/cli/__main__.py").write_text("from shared_auth import VALUE\nprint(VALUE)\n")
    (tmp_path / "app/bridge/src/hook_bridge/__main__.py").write_text("from shared_auth import VALUE\nprint(VALUE)\n")
    for spec in launcher.build_service_specs(tmp_path):
        result = subprocess.run([sys.executable, "-I", *spec.argv[1:]], cwd=spec.cwd,
                                capture_output=True, text=True, check=True)
        assert result.stdout.strip() == "application auth"
