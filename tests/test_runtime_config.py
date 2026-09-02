import json
from pathlib import Path

import pytest

from wechatrebot_manager.runtime_config import (
    RuntimePorts,
    normalize_admin_id,
    write_runtime_configs,
)
from wechatrebot_manager.brand import BrandProfile
from wechatrebot_manager.provider import OpenAIProviderProfile


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
    assert astrbot["dashboard"]["enable"] is False
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


def test_runtime_config_applies_brand_and_customer_provider(tmp_path: Path) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    brand = BrandProfile(
        display_name="群聊管家",
        short_name="管家",
        persona_name="小管家",
    ).normalized()
    provider = OpenAIProviderProfile(
        api_key="customer-secret",
        api_base="https://api.example.com/v1",
        model="chat-model",
    )

    write_runtime_configs(
        install,
        wechat,
        brand_profile=brand,
        provider_profile=provider,
        admin_id="wxid_customer_admin",
    )

    astrbot = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert astrbot["provider_settings"]["default_personality"] == "commercial_brand"
    assert astrbot["provider_settings"]["default_provider_id"] == (
        "commercial_openai/chat-model"
    )
    assert astrbot["persona"][0]["name"] == "commercial_brand"
    assert "群聊管家" in astrbot["persona"][0]["prompt"]
    assert astrbot["provider_sources"][0]["key"] == ["customer-secret"]
    assert astrbot["admins_id"] == ["wxid_customer_admin"]


def test_admin_id_validation_and_reconfiguration_preservation(tmp_path: Path) -> None:
    assert normalize_admin_id(" wxid_customer ") == "wxid_customer"
    with pytest.raises(ValueError, match="wxid"):
        normalize_admin_id("含 空格")

    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    write_runtime_configs(install, wechat, admin_id="wxid_customer")
    write_runtime_configs(install, wechat)
    astrbot = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert astrbot["admins_id"] == ["wxid_customer"]
