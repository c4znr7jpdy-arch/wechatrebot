import json
from pathlib import Path
import zipfile

from wechatrebot_manager.desktop import (
    find_bundled_payload,
    install_root_from_executable,
    is_development_preview,
)
from wechatrebot_manager.payload import sha256_file


def test_find_bundled_payload_next_to_executable(tmp_path: Path) -> None:
    executable = tmp_path / "WeChatReBot-Setup.exe"
    executable.write_bytes(b"exe")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "release-manifest.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    assert find_bundled_payload(executable=executable) == payload.resolve()


def test_find_bundled_archive_next_to_executable(tmp_path: Path) -> None:
    executable = tmp_path / "WeChatReBot.exe"
    executable.write_bytes(b"exe")
    payload = tmp_path / "payload.wrb"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("release-manifest.json", json.dumps({"schema_version": 1}))

    assert find_bundled_payload(executable=executable) == payload.resolve()


def test_installed_manager_discovers_its_install_root(tmp_path: Path) -> None:
    manager = tmp_path / "install" / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"exe")
    (tmp_path / "install" / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    assert install_root_from_executable(manager) == (tmp_path / "install").resolve()


def test_development_preview_requires_matching_payload_manifest_hash(
    tmp_path: Path,
) -> None:
    executable = tmp_path / "WeChatReBot.exe"
    executable.write_bytes(b"exe")
    payload = tmp_path / "payload.wrb"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr(
            "release-manifest.json",
            json.dumps({"schema_version": 1, "source_manifest_status": "phase-incomplete"}),
        )
    distribution = {
        "schema_version": 1,
        "entrypoint": executable.name,
        "payload_status": "phase-incomplete",
        "development_preview": True,
        "files": [
            {
                "path": "payload.wrb",
                "sha256": sha256_file(payload),
            }
        ],
    }
    (tmp_path / "DISTRIBUTION-MANIFEST.json").write_text(
        json.dumps(distribution), encoding="utf-8"
    )

    assert is_development_preview(executable)
    payload.write_bytes(b"tampered")
    assert not is_development_preview(executable)
