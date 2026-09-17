from __future__ import annotations

import json
import hashlib
from pathlib import Path
import zipfile

from scripts import prepare_native_test_payload


def test_overlay_keeps_base_untouched_and_verifies_new_manager(
    monkeypatch, tmp_path: Path
) -> None:
    base = tmp_path / "base.wrb"
    manager = tmp_path / "manager.exe"
    manager.write_bytes(b"MZnew")
    source = tmp_path / "cli.py"
    source.write_bytes(b"new source")
    monkeypatch.setattr(prepare_native_test_payload, "ROOT", tmp_path)
    monkeypatch.setattr(
        prepare_native_test_payload,
        "_current_source_files",
        lambda: {"manager/src/wechatrebot_manager/cli.py": source},
    )
    with zipfile.ZipFile(base, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("manager/WeChatReBot.exe", b"MZold")
        archive.writestr("manager/src/wechatrebot_manager/cli.py", b"old")
        archive.writestr("config/example.txt", b"unchanged")
        archive.writestr(
            "release-manifest.json",
            json.dumps({
                "schema_version": 1,
                "files": [
                    {"path": "manager/WeChatReBot.exe", "sha256": "0" * 64},
                    {"path": "manager/src/wechatrebot_manager/cli.py", "sha256": "0" * 64},
                    {"path": "config/example.txt", "sha256": hashlib.sha256(b"unchanged").hexdigest().upper()},
                ],
            }),
        )
    output = tmp_path / "artifacts" / "unit-overlay.wrb"
    result = prepare_native_test_payload.prepare(base, manager, output)
    with zipfile.ZipFile(output) as archive:
        assert archive.read("manager/WeChatReBot.exe") == b"MZnew"
        assert archive.read("config/example.txt") == b"unchanged"
        manifest = json.loads(archive.read("release-manifest.json"))
        assert manifest["test_only_native_installer_overlay"] is True
    with zipfile.ZipFile(base) as archive:
        assert archive.read("manager/WeChatReBot.exe") == b"MZold"
    assert result["test_only"] is True
