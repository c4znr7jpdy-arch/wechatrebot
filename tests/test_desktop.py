import json
from pathlib import Path

from wechatrebot_manager.desktop import (
    find_bundled_payload,
    install_root_from_executable,
)


def test_find_bundled_payload_next_to_executable(tmp_path: Path) -> None:
    executable = tmp_path / "WeChatReBot-Setup.exe"
    executable.write_bytes(b"exe")
    payload = tmp_path / "payload"
    payload.mkdir()
    (payload / "release-manifest.json").write_text(
        json.dumps({"schema_version": 1}), encoding="utf-8"
    )
    assert find_bundled_payload(executable=executable) == payload.resolve()


def test_installed_manager_discovers_its_install_root(tmp_path: Path) -> None:
    manager = tmp_path / "install" / "manager" / "WeChatReBot.exe"
    manager.parent.mkdir(parents=True)
    manager.write_bytes(b"exe")
    (tmp_path / "install" / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    assert install_root_from_executable(manager) == (tmp_path / "install").resolve()
