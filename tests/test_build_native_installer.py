from __future__ import annotations

import argparse
from pathlib import Path

import pytest

from scripts import build_native_installer


def _manager(path: Path) -> Path:
    path.write_bytes(b"MZ" + b"\0" * 32)
    return path


def test_online_installer_command_contains_pinned_https_payload(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "manager.exe")
    output = build_native_installer.ROOT / "artifacts" / "native-test-online"
    args = argparse.Namespace(
        manager_exe=manager,
        payload=None,
        payload_url="https://example.invalid/payload.wrb",
        payload_sha256="A" * 64,
        output=output,
        iscc=None,
    )

    command, result = build_native_installer.command_for(args, require_compiler=False)

    assert result["mode"] == "online"
    assert "/DPayloadSHA256=" + "A" * 64 in command
    assert "/DPayloadUrl=https://example.invalid/payload.wrb" in command


def test_offline_installer_records_payload_digest(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "manager.exe")
    payload = tmp_path / "payload.wrb"
    payload.write_bytes(b"payload")
    output = build_native_installer.ROOT / "artifacts" / "native-test-offline"
    args = argparse.Namespace(
        manager_exe=manager,
        payload=payload,
        payload_url=None,
        payload_sha256=None,
        output=output,
        iscc=None,
    )

    command, result = build_native_installer.command_for(args, require_compiler=False)

    assert result["mode"] == "offline"
    assert result["payload_size"] == 7
    assert any(item.startswith("/DPayloadArchive=") for item in command)


def test_online_installer_rejects_unpinned_or_non_https_payload(tmp_path: Path) -> None:
    manager = _manager(tmp_path / "manager.exe")
    output = build_native_installer.ROOT / "artifacts" / "native-test-invalid"
    args = argparse.Namespace(
        manager_exe=manager,
        payload=None,
        payload_url="http://example.invalid/payload.wrb",
        payload_sha256="bad",
        output=output,
        iscc=None,
    )

    with pytest.raises(ValueError):
        build_native_installer.command_for(args, require_compiler=False)
