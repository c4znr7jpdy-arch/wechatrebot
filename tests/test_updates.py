from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
import zipfile

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
    extract_update_archive,
    load_update_channel,
    parse_update_manifest,
)


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
                "source_manifest_status": "ready",
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


def test_current_or_older_signed_version_is_not_offered() -> None:
    channel, private = _channel_and_key()
    payload = _signed_manifest(channel, private)
    assert parse_update_manifest(payload, channel, current_version="0.8.0") is None


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

    assert plan.helper_executable.read_bytes() == b"helper"
    assert apply_update_plan(plan.plan_path, restart=False) == 0
    assert (install / "manager" / "WeChatReBot.exe").read_bytes() == b"new-manager"
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
