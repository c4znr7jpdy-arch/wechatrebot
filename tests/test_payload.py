import hashlib
import json
from pathlib import Path

import pytest

from wechatrebot_manager.payload import PayloadError, install_payload, verify_payload


def _payload(root: Path, *, status: str = "ready", path: str = "app/example.txt") -> Path:
    source = root / Path(*path.split("/"))
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("commercial payload\n", encoding="utf-8")
    digest = hashlib.sha256(source.read_bytes()).hexdigest().upper()
    (root / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_manifest_status": status,
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
    assert installed["source_manifest_status"] == "ready"


def test_tampered_payload_is_rejected_before_install(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    source = _payload(payload)
    source.write_text("tampered", encoding="utf-8")
    with pytest.raises(PayloadError, match="校验失败"):
        verify_payload(payload)


def test_incomplete_payload_requires_explicit_development_override(tmp_path: Path) -> None:
    payload = tmp_path / "payload"
    _payload(payload, status="phase-incomplete")
    with pytest.raises(PayloadError, match="尚未达到"):
        verify_payload(payload)
    assert verify_payload(payload, allow_incomplete=True).files


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
        ("verify", 1, 1),
        ("stage", 1, 1),
        ("install", 1, 1),
    ]
