from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest

from scripts import publish_native_download as release


def test_zip_contains_installer_and_instructions_only(tmp_path: Path):
    setup = tmp_path / "WeChatReBot-Setup-0.9.2.exe"
    setup.write_bytes(b"MZ" + b"installer" * 50)
    payload = tmp_path / "payload.wrb"
    payload.write_bytes(b"runtime")
    archive, checksum = release.wrap_installer(setup, tmp_path, "0.9.2", payload)
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {setup.name, "安装说明.txt"}
        assert package.read(setup.name) == setup.read_bytes()
        assert "联网" in package.read("安装说明.txt").decode("utf-8-sig")
    assert checksum.read_text().strip() == f"{release.sha256_file(archive)}  {archive.name}"


def test_prepared_artifact_change_is_rejected(tmp_path: Path):
    file = tmp_path / "release.zip"
    file.write_bytes(b"original")
    record = release.record(file)
    file.write_bytes(b"modified")
    with pytest.raises(ValueError, match="已改变"):
        release.verify_record(record)


def test_smoke_payload_cannot_be_published(monkeypatch, tmp_path: Path):
    payload = tmp_path / "payload.wrb"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps({"release_version": "0.9.2", "test_only_native_installer_overlay": True}))
    monkeypatch.setattr(release, "verify_payload", lambda _: SimpleNamespace(files=[1]))
    with pytest.raises(ValueError, match="烟测"):
        release.distribution_payload(payload, tmp_path / "manager.exe", "0.9.2")


def test_upload_only_sets_specific_release_objects_public(monkeypatch, tmp_path: Path):
    package = tmp_path / "release.zip"
    package.write_bytes(b"test")
    commands, checked = [], []
    monkeypatch.setattr(release.subprocess, "run", lambda command, **kwargs: commands.append(command))
    monkeypatch.setattr(release, "public_verify", lambda url, path: checked.append((url, path)))
    release.upload(release.storage_config(), [("downloads", package, "application/zip")], "ossutil", None)
    upload_command, acl_command = commands
    assert upload_command[1] == "cp"
    assert upload_command[3] == "oss://jiangxiaomei-releases-bj/wechatrebot-updates/downloads/release.zip"
    assert "--acl" not in upload_command
    assert upload_command[upload_command.index("--region") + 1] == "cn-beijing"
    assert upload_command[upload_command.index("--content-type") + 1] == "application/zip"
    assert upload_command[upload_command.index("--cache-control") + 1] == "public,max-age=31536000,immutable"
    assert upload_command[upload_command.index("--metadata") + 1].startswith("sha256=")
    assert upload_command[upload_command.index("--content-disposition") + 1].startswith("attachment;")
    assert acl_command[1:3] == ["api", "put-object-acl"]
    assert acl_command[acl_command.index("--object-acl") + 1] == "public-read"
    assert checked[0][0].startswith("https://jiangxiaomei-releases-bj.oss-cn-beijing.aliyuncs.com/")


def test_public_verification_hashes_actual_download(monkeypatch, tmp_path: Path):
    artifact = tmp_path / "release.zip"
    artifact.write_bytes(b"good")
    response = io.BytesIO(b"bad!")
    response.status = 200
    monkeypatch.setattr(release.urllib.request, "urlopen", lambda *args, **kwargs: response)
    with pytest.raises(ValueError, match="不一致"):
        release.public_verify("https://example.test/release.zip", artifact)


def test_page_restores_backups_if_metadata_upload_fails(monkeypatch, tmp_path: Path):
    commands = []
    metadata = tmp_path / "download.json"
    metadata.write_text("{}")
    args = SimpleNamespace(host="root@47.242.208.64", remote_root="/srv/wechatrebot-updates", base_url="https://example.test/wechatrebot-updates")
    monkeypatch.setattr(release.publisher, "_ssh", lambda host, command: commands.append(command))
    def copy(path, host, target):
        if path == metadata:
            raise RuntimeError("upload interrupted")
    monkeypatch.setattr(release.publisher, "_scp", copy)
    with pytest.raises(RuntimeError, match="interrupted"):
        release.deploy_page(metadata, args)
    assert "/manifests/native-download-" in commands[-1]
    assert "cp " in commands[-1]
    assert "stable.json" not in "\n".join(commands)


def test_stale_runtime_is_rejected_before_packaging(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(release.build_staging, "_validate_runtime_root", lambda *args: {"requirements_sha256": "outdated"})
    with pytest.raises(ValueError, match="缓存运行时不是当前源码版本"):
        release.validate_runtime_for_release(tmp_path)
