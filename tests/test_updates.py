from __future__ import annotations

import base64
import hashlib
import io
import json
from pathlib import Path
import urllib.error
import zipfile
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.updates import (
    PRODUCT_ID,
    UpdateChannel,
    UpdateError,
    UpdateRelease,
    PreparedUpdate,
    apply_update_plan,
    canonical_manifest_bytes,
    create_update_plan,
    cleanup_stale_update_workdirs,
    consume_update_result,
    extract_update_archive,
    load_update_channel,
    MandatoryUpdateUnavailable,
    parse_update_manifest,
    prepare_update,
    recover_update_plan,
)
from wechatrebot_manager.update_errors import UpdateFailure
from wechatrebot_manager import updates as updates_module


def _channel_and_key() -> tuple[UpdateChannel, Ed25519PrivateKey]:
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    return (
        UpdateChannel(
            manifest_url="https://updates.example.test/stable.json",
            channel="stable",
            key_id=key_id,
            public_key=public,
        ),
        private,
    )


def _signed_manifest(
    channel: UpdateChannel,
    private: Ed25519PrivateKey,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": 1,
        "product": PRODUCT_ID,
        "channel": "stable",
        "version": "0.8.0",
        "minimum_supported_version": "0.6.0",
        "published_at": "2026-09-02T00:00:00+00:00",
        "title": "插件与更新中心",
        "notes": ["支持安全远程更新", "修复双击启动反馈"],
        "package": {
            "url": "https://updates.example.test/wechatrebot-0.8.0-update.zip",
            "size": 1234,
            "sha256": "A" * 64,
        },
    }
    signature = private.sign(canonical_manifest_bytes(payload))
    payload["signature"] = {
        "algorithm": "ed25519",
        "key_id": channel.key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    return payload


def _ready_payload(root: Path, manager_content: bytes = b"new-manager") -> Path:
    manager = root / "manager" / "WeChatReBot.exe"
    app = root / "app" / "feature.txt"
    manager.parent.mkdir(parents=True)
    app.parent.mkdir(parents=True)
    manager.write_bytes(manager_content)
    app.write_text("updated", encoding="utf-8")
    files = []
    for path in (manager, app):
        files.append(
            {
                "path": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
            }
        )
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": files,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_embedded_update_channel_has_a_valid_pinned_public_key() -> None:
    channel = load_update_channel()
    assert len(channel.public_key) == 32
    assert len(channel.key_id) == 16
    assert channel.enabled
    assert channel.manifest_url == (
        "https://ss.xiuxianjyj.xin/wechatrebot-updates/stable.json"
    )


def test_signed_manifest_is_verified_before_update_is_offered() -> None:
    channel, private = _channel_and_key()
    payload = _signed_manifest(channel, private)

    release = parse_update_manifest(payload, channel, current_version="0.7.0")

    assert release is not None
    assert release.version == "0.8.0"
    assert not release.mandatory
    payload["title"] = "tampered"
    with pytest.raises(UpdateError, match="签名校验失败"):
        parse_update_manifest(payload, channel, current_version="0.7.0")


def test_signed_manifest_build_and_mandatory_fields_are_honored() -> None:
    channel, private = _channel_and_key()
    payload = _signed_manifest(channel, private)
    payload["version"] = "0.7.0"
    payload["build"] = 8
    payload["mandatory"] = True
    payload.pop("signature")
    payload["signature"] = {
        "algorithm": "ed25519",
        "key_id": channel.key_id,
        "value": base64.b64encode(private.sign(canonical_manifest_bytes(payload))).decode("ascii"),
    }

    release = parse_update_manifest(
        payload,
        channel,
        current_version="0.7.0",
        current_build=7,
    )

    assert release is not None
    assert release.build == 8
    assert release.mandatory is True
    assert parse_update_manifest(
        payload,
        channel,
        current_version="0.7.0",
        current_build=8,
    ) is None


def test_current_or_older_signed_version_is_not_offered() -> None:
    channel, private = _channel_and_key()
    payload = _signed_manifest(channel, private)
    assert parse_update_manifest(payload, channel, current_version="0.8.0") is None


def test_unsupported_client_is_blocked_when_manifest_has_no_newer_update() -> None:
    channel, private = _channel_and_key()
    payload = _signed_manifest(channel, private)
    payload["version"] = "0.8.0"
    payload["minimum_supported_version"] = "0.9.0"
    payload.pop("signature")
    payload["signature"] = {
        "algorithm": "ed25519",
        "key_id": channel.key_id,
        "value": base64.b64encode(private.sign(canonical_manifest_bytes(payload))).decode("ascii"),
    }

    with pytest.raises(MandatoryUpdateUnavailable, match="未提供可用的新版本"):
        parse_update_manifest(payload, channel, current_version="0.8.0")


def test_signed_current_version_can_be_published_without_a_package() -> None:
    channel, private = _channel_and_key()
    payload = _signed_manifest(channel, private)
    payload["package"] = None
    payload.pop("signature")
    signature = private.sign(canonical_manifest_bytes(payload))
    payload["signature"] = {
        "algorithm": "ed25519",
        "key_id": channel.key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }

    assert parse_update_manifest(payload, channel, current_version="0.8.0") is None
    with pytest.raises(UpdateError, match="缺少安装包"):
        parse_update_manifest(payload, channel, current_version="0.7.0")


def test_update_archive_must_keep_every_file_under_payload(tmp_path: Path) -> None:
    safe = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe, "w") as package:
        package.writestr("payload/release-manifest.json", "{}")
    assert extract_update_archive(safe, tmp_path / "safe-out") == (
        tmp_path / "safe-out" / "payload"
    ).resolve()

    unsafe = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(unsafe, "w") as package:
        package.writestr("../outside.txt", "bad")
    with pytest.raises(UpdateError, match="路径越界"):
        extract_update_archive(unsafe, tmp_path / "unsafe-out")
    assert not (tmp_path / "outside.txt").exists()


def test_update_archive_supports_long_windows_staging_paths(tmp_path: Path) -> None:
    archive = tmp_path / "long.zip"
    relative = (
        "payload/runtime/python/Lib/site-packages/lark_oapi/api/"
        "security_and_compliance/v2/model/"
        "p2_security_and_compliance_device_apply_record_device_apply_event_v2.py"
    )
    destination = tmp_path / ("staging-" + "x" * 80)
    assert len(str(destination / Path(*relative.split("/")))) >= 248
    with zipfile.ZipFile(archive, "w") as package:
        package.writestr("payload/release-manifest.json", "{}")
        package.writestr(relative, b"long-path-ok")

    payload = extract_update_archive(archive, destination)

    assert (payload / Path(*relative.split("/")[1:])).read_bytes() == b"long-path-ok"


def test_update_plan_uses_detached_manager_copy_and_applies_transactionally(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    (install / "updates").mkdir(parents=True)
    (install / "manager").mkdir()
    (install / "manager" / "WeChatReBot.exe").write_bytes(b"old-manager")
    (install / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    working = install / "updates" / ".pending-0.8.0-test"
    payload = _ready_payload(working / "extracted" / "payload")
    archive = working / "update.zip"
    archive.write_bytes(b"archive")
    release = UpdateRelease(
        "0.8.0", "update", (), "", "https://updates.example.test/update.zip", 7, "A" * 64, False
    )
    prepared = PreparedUpdate(release, working, payload, archive)
    current = tmp_path / "current.exe"
    current.write_bytes(b"helper")

    plan = create_update_plan(
        prepared, install, executable=current, parent_pid=0
    )

    assert plan.helper_executable.read_bytes() == b"new-manager"
    plan_payload = json.loads(plan.plan_path.read_text(encoding="utf-8"))
    assert plan_payload["wait_pids"] == [0]
    assert apply_update_plan(plan.plan_path, restart=False) == 0
    assert (install / "manager" / "WeChatReBot.exe").read_bytes() == b"new-manager"
    assert (install / "卸载机器人.exe").read_bytes() == b"new-manager"
    assert (install / "app" / "feature.txt").read_text(encoding="utf-8") == "updated"
    result = json.loads(
        (install / "updates" / "update-result.json").read_text(encoding="utf-8")
    )
    assert result == {
        "schema_version": 1,
        "ok": True,
        "version": "0.8.0",
        "message": "更新已完成。",
    }


def test_startup_failure_restores_previous_manager_and_payload(
    monkeypatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    (install / "updates").mkdir(parents=True)
    (install / "manager").mkdir()
    (install / "manager" / "WeChatReBot.exe").write_bytes(b"old-manager")
    (install / "app").mkdir()
    (install / "app" / "feature.txt").write_text("old-feature", encoding="utf-8")
    (install / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    working = install / "updates" / ".pending-0.8.0-test"
    payload = _ready_payload(working / "extracted" / "payload")
    archive = working / "update.zip"
    archive.write_bytes(b"archive")
    release = UpdateRelease(
        "0.8.0", "update", (), "", "https://updates.example.test/update.zip", 7, "A" * 64, False
    )
    current = tmp_path / "current.exe"
    current.write_bytes(b"old-helper")
    plan = create_update_plan(
        PreparedUpdate(release, working, payload, archive),
        install,
        executable=current,
        parent_pid=0,
        current_version="0.7.9",
    )
    launches = []

    class Process:
        def poll(self):
            return None

    monkeypatch.setattr(
        updates_module,
        "_restart_manager",
        lambda *_args, **_kwargs: launches.append("started") or Process(),
    )
    monkeypatch.setattr(updates_module, "_stop_restarted_manager", lambda _process: None)
    monkeypatch.setattr(
        updates_module,
        "_wait_for_startup_confirmation",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            UpdateFailure("UPD-1009", "test startup timeout")
        ),
    )

    assert apply_update_plan(plan.plan_path) == 1
    assert (install / "manager" / "WeChatReBot.exe").read_bytes() == b"old-manager"
    assert (install / "app" / "feature.txt").read_text(encoding="utf-8") == "old-feature"
    result = json.loads(
        (install / "updates" / "update-result.json").read_text(encoding="utf-8")
    )
    assert result["error_code"] == "UPD-1009"
    assert result["status"] == "failed"
    assert launches == ["started", "started"]


class _DownloadResponse(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.headers = {"Content-Length": str(len(content))}

    def geturl(self) -> str:
        return "https://updates.example.test/package.zip"

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.close()


def _release_for_archive(content: bytes) -> UpdateRelease:
    return UpdateRelease(
        "0.8.0",
        "update",
        (),
        "",
        "https://updates.example.test/package.zip",
        len(content),
        hashlib.sha256(content).hexdigest().upper(),
        False,
    )


def test_download_hash_mismatch_is_rejected_and_partial_directory_removed(
    monkeypatch, tmp_path: Path
) -> None:
    content = b"not-the-signed-package"
    release = _release_for_archive(content)
    release = UpdateRelease(
        release.version,
        release.title,
        release.notes,
        release.published_at,
        release.package_url,
        release.package_size,
        "0" * 64,
        release.mandatory,
    )
    channel, _private = _channel_and_key()
    monkeypatch.setattr(
        updates_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _DownloadResponse(content),
    )

    with pytest.raises(UpdateFailure) as error:
        prepare_update(release, channel, tmp_path / "install")

    assert error.value.definition.code == "UPD-1003"
    assert not tuple((tmp_path / "install" / "updates").glob(".pending-*"))


def test_network_failure_is_customer_safe_and_leaves_no_partial_files(
    monkeypatch, tmp_path: Path
) -> None:
    release = _release_for_archive(b"some-package")
    channel, _private = _channel_and_key()
    monkeypatch.setattr(
        updates_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            urllib.error.URLError("offline")
        ),
    )

    with pytest.raises(UpdateFailure) as error:
        prepare_update(release, channel, tmp_path / "install")

    assert error.value.definition.code == "UPD-1002"
    assert not tuple((tmp_path / "install" / "updates").glob(".pending-*"))


def test_insufficient_disk_space_fails_before_network_request(
    monkeypatch, tmp_path: Path
) -> None:
    release = _release_for_archive(b"package")
    channel, _private = _channel_and_key()
    called = False

    def forbidden_network(*_args, **_kwargs):
        nonlocal called
        called = True
        raise AssertionError("download should not start")

    monkeypatch.setattr(
        updates_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=0, total=0, used=0),
    )
    monkeypatch.setattr(updates_module.urllib.request, "urlopen", forbidden_network)

    with pytest.raises(UpdateFailure) as error:
        prepare_update(release, channel, tmp_path / "install")

    assert error.value.definition.code == "UPD-1004"
    assert "MB" in error.value.to_public_dict()["message"]
    assert called is False


def test_disk_space_preflight_accounts_for_install_staging_and_both_rollbacks(
    monkeypatch, tmp_path: Path
) -> None:
    release = UpdateRelease(
        "0.8.0",
        "update",
        (),
        "",
        "https://updates.example.test/package.zip",
        10,
        "A" * 64,
        False,
        uncompressed_size=100,
    )
    monkeypatch.setattr(updates_module, "_estimated_rollback_bytes", lambda _root: 50)
    reserve = 64 * 1024**2
    monkeypatch.setattr(
        updates_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=10 + 2 * 100 + 2 * 50 + reserve, total=0, used=0),
    )

    updates_module._ensure_update_disk_space(tmp_path, release)

    monkeypatch.setattr(
        updates_module.shutil,
        "disk_usage",
        lambda _path: SimpleNamespace(free=100 + 2 * 50 + reserve - 1, total=0, used=0),
    )
    with pytest.raises(UpdateFailure) as error:
        updates_module._ensure_update_disk_space(tmp_path, release, staged=True)
    assert error.value.definition.code == "UPD-1004"


def test_next_start_recovery_restores_an_interrupted_install(
    monkeypatch, tmp_path: Path
) -> None:
    install = tmp_path / "install"
    updates = install / "updates"
    rollback = updates / ".rollback-0123456789abcdef0123456789abcdef"
    backup_files = rollback / "files"
    backup_files.mkdir(parents=True)
    (install / "manager").mkdir(parents=True)
    (install / "app").mkdir()
    (install / "manager" / "WeChatReBot.exe").write_bytes(b"new-manager")
    (install / "app" / "feature.txt").write_text("new-feature", encoding="utf-8")
    (install / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    (backup_files / "00000000").write_bytes(b"old-manager")
    (backup_files / "00000001").write_text("old-feature", encoding="utf-8")
    update_id = rollback.name.removeprefix(".rollback-")
    journal_path = updates / "update-journal.json"
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "update_id": update_id,
                "version": "0.8.0",
                "state": "awaiting_startup",
                "rollback_root": rollback.relative_to(install).as_posix(),
                "files": [
                    {"path": "manager/WeChatReBot.exe", "existed": True, "backup": "files/00000000"},
                    {"path": "app/feature.txt", "existed": True, "backup": "files/00000001"},
                ],
            }
        ),
        encoding="utf-8",
    )
    plan_path = updates / f".recovery-plan-{update_id}.json"
    plan_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "action": "recover-interrupted-update",
                "update_id": update_id,
                "install_root": str(install),
                "target_executable": str(install / "manager" / "WeChatReBot.exe"),
                "parent_pid": 0,
                "wait_pids": [0],
            }
        ),
        encoding="utf-8",
    )
    restarted = []
    monkeypatch.setattr(
        updates_module,
        "_restart_manager",
        lambda target, _root, **_kwargs: restarted.append(target),
    )

    assert recover_update_plan(plan_path) == 0
    assert (install / "manager" / "WeChatReBot.exe").read_bytes() == b"old-manager"
    assert (install / "app" / "feature.txt").read_text(encoding="utf-8") == "old-feature"
    result = json.loads((updates / "update-result.json").read_text(encoding="utf-8"))
    assert result["error_code"] == "UPD-1009"
    assert journal_path.exists() is False
    assert rollback.exists() is False
    assert restarted == [install / "manager" / "WeChatReBot.exe"]


def test_stale_update_workdirs_are_cleaned_without_touching_recent_data(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    updates = install / "updates"
    old = updates / ".pending-old"
    recent = updates / ".pending-recent"
    old.mkdir(parents=True)
    recent.mkdir()
    (old / "large.bin").write_bytes(b"old")
    import os
    import time

    old_time = time.time() - 7200
    os.utime(old, (old_time, old_time))

    assert cleanup_stale_update_workdirs(install, minimum_age_seconds=3600) == 1
    assert not old.exists()
    assert recent.exists()


def test_update_plan_waits_for_onefile_bootloader(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import updates

    executable = tmp_path / "WeChatReBot.exe"
    executable.write_bytes(b"manager")
    monkeypatch.setattr(updates.os, "name", "nt")
    monkeypatch.setattr(updates.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates.os, "getppid", lambda: 41)
    monkeypatch.setattr(updates, "_process_executable", lambda _pid: executable)

    assert updates._update_wait_pids(
        executable,
        42,
        include_bootloader=True,
    ) == (42, 41)


def test_legacy_update_result_gets_status_for_webui(tmp_path: Path) -> None:
    updates = tmp_path / "updates"
    updates.mkdir()
    result_path = updates / "update-result.json"
    result_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "ok": False,
                "version": "0.8.7",
                "message": "文件仍被占用。",
            }
        ),
        encoding="utf-8",
    )

    assert consume_update_result(tmp_path) == {
        "schema_version": 1,
        "ok": False,
        "status": "failed",
        "version": "0.8.7",
        "message": "文件仍被占用。",
    }
    assert not result_path.exists()
