import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager.payload import (
    PayloadError,
    install_payload,
    payload_relative_paths,
    verify_payload,
)


def _payload(root: Path, *, path: str = "app/example.txt") -> Path:
    source = root / Path(*path.split("/"))
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("commercial payload\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [{"path": path, "sha256": digest}],
            }
        ),
        encoding="utf-8",
    )
    return source


def test_verified_payload_installs_and_records_hashes(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    install = tmp_path / "install"
    install.mkdir()

    result = install_payload(payload, install)

    assert result.files[0].sha256 == hashlib.sha256(source.read_bytes()).hexdigest().upper()
    assert (install / "app" / "example.txt").read_text(encoding="utf-8") == "commercial payload\n"
    installed = json.loads(
        (install / "config" / "installed-payload.json").read_text(encoding="utf-8")
    )
    assert installed["schema_version"] == 1
    assert "source_manifest_status" not in installed


def test_tampered_payload_is_rejected_before_install(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    source.write_text("tampered", encoding="utf-8")
    with pytest.raises(PayloadError, match="校验失败"):
        verify_payload(payload)


def test_streaming_install_rejects_tamper_before_replacing_target(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    source.write_text("tampered", encoding="utf-8")
    install = tmp_path / "install"
    target = install / "app" / "example.txt"
    target.parent.mkdir(parents=True)
    target.write_text("existing installation\n", encoding="utf-8")

    with pytest.raises(PayloadError, match="校验失败"):
        install_payload(payload, install)

    assert target.read_text(encoding="utf-8") == "existing installation\n"


def test_legacy_payload_status_does_not_block_verification(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    manifest_path = payload / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_manifest_status"] = "phase-incomplete"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert verify_payload(payload).files


def test_path_preflight_reads_manifest_without_walking_payload(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = tmp_path / "payload"
    _payload(payload)

    def fail_if_walked(_path: Path, _pattern: str):
        raise AssertionError("path preflight must not traverse the payload")

    monkeypatch.setattr(Path, "rglob", fail_if_walked)

    assert payload_relative_paths(payload) == (Path("app/example.txt"),)


def test_manifest_cannot_escape_or_overwrite_user_data(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    manifest_path = payload / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../outside.txt"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(PayloadError, match="不安全"):
        verify_payload(payload)

    payload = tmp_path / "payload-user-data"
    _payload(payload, path="data/private.json")
    with pytest.raises(PayloadError, match="用户数据"):
        verify_payload(payload)


def test_install_payload_reports_each_phase(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload)
    events: list[tuple[str, int, int]] = []

    install_payload(
        payload,
        tmp_path / "install",
        progress_callback=lambda phase, current, total: events.append(
            (phase, current, total)
        ),
    )

    assert events == [
        ("prepare", 1, 1),
        ("install", 1, 1),
    ]


def test_windows_file_replace_retries_transient_lock(monkeypatch, tmp_path: Path) -> None:
    from wechatrebot_manager import payload as payload_module

    source = tmp_path / "source.exe"
    target = tmp_path / "target.exe"
    source.write_bytes(b"new")
    target.write_bytes(b"old")
    real_replace = payload_module.os.replace
    attempts = 0

    def flaky_replace(current_source: Path, current_target: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise PermissionError("temporarily locked")
        real_replace(current_source, current_target)

    monkeypatch.setattr(payload_module.os, "name", "nt")
    monkeypatch.setattr(payload_module.os, "replace", flaky_replace)
    monkeypatch.setattr(payload_module.time, "sleep", lambda _seconds: None)

    payload_module._replace_file(source, target, timeout_seconds=1)

    assert attempts == 3
    assert target.read_bytes() == b"new"


def test_packed_payload_installs_and_verifies_in_one_stream(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    archive_path = tmp_path / "payload.wrb"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(payload / "release-manifest.json", "release-manifest.json")
        archive.write(source, "app/example.txt")
    events: list[str] = []

    result = install_payload(
        archive_path,
        tmp_path / "install",
        progress_callback=lambda phase, _current, _total: events.append(phase),
    )

    assert result.root == archive_path.resolve()
    assert events == ["prepare", "install"]
    assert (tmp_path / "install" / "app" / "example.txt").read_text(
        encoding="utf-8"
    ) == "commercial payload\n"


def test_packed_payload_rejects_unlisted_file(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    archive_path = tmp_path / "payload.wrb"
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.write(payload / "release-manifest.json", "release-manifest.json")
        archive.write(source, "app/example.txt")
        archive.writestr("app/unlisted.txt", "unexpected")

    with pytest.raises(PayloadError, match="未登记"):
        verify_payload(archive_path)


def test_install_uses_flat_transaction_paths_for_deep_payload(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "payload.wrb"
    relative = "runtime/" + "deep/" * 17 + "module.py"
    content = b"payload"
    digest = hashlib.sha256(content).hexdigest().upper()
    manifest = {
        "schema_version": 1,
        "files": [{"path": relative, "sha256": digest}],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr(relative, content)
    install_root = tmp_path / "install"
    legacy_transaction_chars = (
        len(str(install_root)) + len("\\updates\\.rollback-") + 32 + 1 + len(relative)
    )
    assert len(str(install_root / Path(*relative.split("/")))) <= 259
    assert legacy_transaction_chars > 259

    install_payload(archive_path, install_root)

    assert (install_root / Path(*relative.split("/"))).read_bytes() == content
    assert not tuple((install_root / "updates").glob(".*-*"))


def test_install_rejects_final_target_path_overflow_before_creating_root(
    tmp_path: Path,
) -> None:
    archive_path = tmp_path / "payload-too-deep.wrb"
    relative = "runtime/" + "deep/" * 45 + "module.py"
    content = b"payload"
    manifest = {
        "schema_version": 1,
        "files": [
            {
                "path": relative,
                "sha256": hashlib.sha256(content).hexdigest().upper(),
            }
        ],
    }
    with zipfile.ZipFile(archive_path, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps(manifest))
        archive.writestr(relative, content)
    install_root = tmp_path / "rejected-install"

    with pytest.raises(PayloadError, match="路径过长"):
        install_payload(archive_path, install_root)

    assert not install_root.exists()
