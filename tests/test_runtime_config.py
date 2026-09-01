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
    astrbot = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert bridge["wechat_exe_path"] == str(wechat.resolve())
    assert bridge["astrbot_ws_url"] == "ws://127.0.0.1:6200/ws"
    assert bridge["injector_path"] == "vendor/hook/x64 inject.exe"
    assert manager["ports"]["hook_api"] == 19088
    assert len(astrbot["platform"]) == 1
    assert astrbot["platform"][0]["ws_reverse_port"] == 6200
    assert astrbot["dashboard"]["host"] == "127.0.0.1"
    assert (install / "app" / "astrbot" / ".astrbot").is_file()


def test_runtime_ports_must_be_unique() -> None:
    ports = RuntimePorts(dashboard=6200, astrbot_ws=6200)
    with pytest.raises(ValueError, match="不能重复"):
        write_runtime_configs(Path.cwd(), Path.cwd() / "Weixin.exe", ports=ports)


def test_runtime_reconfiguration_preserves_customer_provider_and_dashboard_hash(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    config_path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "platform": [{"id": "obsolete"}],
                "provider": [{"id": "customer-provider", "api_key": "private"}],
                "dashboard": {"pbkdf2_password": "stored-hash", "port": 1},
                "plugin_set": ["customer-plugin"],
            }
        ),
        encoding="utf-8",
    )
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")

    write_runtime_configs(install, wechat)

    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["provider"][0]["id"] == "customer-provider"
    assert updated["dashboard"]["pbkdf2_password"] == "stored-hash"
    assert updated["plugin_set"] == ["customer-plugin"]
    assert updated["platform"] == [
        {
            "id": "aiocqhttp_hook41827",
            "type": "aiocqhttp",
            "enable": True,
            "ws_reverse_host": "127.0.0.1",
            "ws_reverse_port": 6200,
            "ws_reverse_token": "",
        }
    ]
