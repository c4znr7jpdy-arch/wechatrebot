import json
from pathlib import Path

import pytest

from wechatrebot_manager.runtime_config import RuntimePorts, write_runtime_configs


def test_runtime_config_uses_only_new_bridge_ports_and_selected_wechat(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")

    write_runtime_configs(install, wechat, wechat_version="4.1.8.27")

    bridge = json.loads(
        (install / "app" / "bridge" / "config.json").read_text(encoding="utf-8")
    )
    manager = json.loads(
        (install / "config" / "manager.json").read_text(encoding="utf-8")
    )
    assert bridge["wechat_exe_path"] == str(wechat.resolve())
    assert bridge["astrbot_ws_url"] == "ws://127.0.0.1:6200/ws"
    assert bridge["injector_path"] == "vendor/hook/x64 inject.exe"
    assert manager["ports"]["hook_api"] == 19088


def test_runtime_ports_must_be_unique() -> None:
    ports = RuntimePorts(dashboard=6200, astrbot_ws=6200)
    with pytest.raises(ValueError, match="不能重复"):
        write_runtime_configs(Path.cwd(), Path.cwd() / "Weixin.exe", ports=ports)
