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
    assert astrbot["dashboard"]["enable"] is True
    assert astrbot["dashboard"]["username"] == "local-console"
    assert len(astrbot["dashboard"]["jwt_secret"]) == 64
    assert astrbot["dashboard"]["pbkdf2_password"].startswith("pbkdf2_sha256$600000$")
    assert (install / "app" / "astrbot" / ".astrbot").is_file()


def test_runtime_config_can_be_created_before_wechat_is_installed(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"

    write_runtime_configs(install)

    bridge = json.loads(
        (install / "app" / "bridge" / "config.json").read_text(encoding="utf-8")
    )
    manager = json.loads(
        (install / "config" / "manager.json").read_text(encoding="utf-8")
    )
    assert bridge["wechat_exe_path"] == ""
    assert manager["wechat_exe_path"] == ""
    assert manager["wechat_version"] is None


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
    assert updated["platform_settings"]["enable_id_white_list"] is False
    assert (install / "app" / "astrbot" / "data" / "config").is_dir()
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
    assert astrbot["provider_settings"]["request_max_retries"] == 1
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


def test_delivery_uses_native_context_and_seeds_angel_heart_model(tmp_path: Path) -> None:
    install = tmp_path / "install"
    legal = install / "legal"
    plugin = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "plugins"
        / "astrbot_plugin_angel_heart"
    )
    legal.mkdir(parents=True)
    plugin.mkdir(parents=True)
    (legal / "feature-set.json").write_text(
        json.dumps(
            {
                "features": [
                    {
                        "module": "astrbot_plugin_angel_heart",
                        "display_name": "长期记忆",
                        "configuration": "page",
                    },
                    {
                        "module": "astrbot_plugin_music",
                        "display_name": "点歌",
                        "configuration": "none",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    provider = OpenAIProviderProfile(
        api_key="customer-secret",
        api_base="https://api.example.com/v1",
        model="chat-model",
    )

    write_runtime_configs(install, provider_profile=provider)

    astrbot = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(
            encoding="utf-8"
        )
    )
    angel_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "astrbot_plugin_angel_heart_config.json"
    )
    angel = json.loads(angel_path.read_text(encoding="utf-8"))
    assert astrbot["plugin_set"] == [
        "astrbot_plugin_angel_heart",
        "astrbot_plugin_music",
    ]
    assert astrbot["provider_settings"]["context_limit_reached_strategy"] == "llm_compress"
    assert astrbot["provider_settings"]["llm_compress_provider_id"] == (
        "commercial_openai/chat-model"
    )
    assert angel["analyzer_model"] == "commercial_openai/chat-model"
    assert angel["wake_interaction"]["alias"] == "小妹"
    assert angel["personality"]["ai_self_identity"].startswith("你是小妹")

    angel_path.write_text(json.dumps({"analyzer_model": "custom/model"}), encoding="utf-8")
    astrbot["plugin_set"] = ["astrbot_plugin_angel_heart", "legacy-plugin"]
    (install / "app" / "astrbot" / "data" / "cmd_config.json").write_text(
        json.dumps(astrbot), encoding="utf-8"
    )
    write_runtime_configs(install, provider_profile=provider)
    preserved = json.loads(angel_path.read_text(encoding="utf-8"))
    updated_astrbot = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert preserved["analyzer_model"] == "custom/model"
    assert updated_astrbot["plugin_set"] == ["astrbot_plugin_angel_heart"]


def test_existing_global_features_migrate_once_to_group_scoping(tmp_path: Path) -> None:
    install = tmp_path / "install"
    legal = install / "legal"
    legal.mkdir(parents=True)
    (legal / "feature-set.json").write_text(json.dumps({"features": [
        {"module": "jiang_commands", "display_name": "指令", "configuration": "none", "default_enabled": True},
        {"module": "jiang_schedule", "display_name": "定时任务", "configuration": "none", "default_enabled": True},
        {"module": "astrbot_plugin_self_iterative_core", "display_name": "实验", "configuration": "none", "default_enabled": False},
    ]}), encoding="utf-8")
    path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plugin_set": ["jiang_commands"]}), encoding="utf-8")
    write_runtime_configs(install)
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["plugin_set"] == ["jiang_commands", "jiang_schedule"]
    assert updated["wechatrebot_feature_scope_version"] == 1
    updated["plugin_set"] = ["jiang_commands"]
    path.write_text(json.dumps(updated), encoding="utf-8")
    write_runtime_configs(install)
    assert json.loads(path.read_text(encoding="utf-8"))["plugin_set"] == ["jiang_commands"]


def test_delivery_seeds_fast_single_memory_and_private_emotion_defaults(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    data = install / "app" / "astrbot" / "data"
    for module in (
        "astrbot_plugin_livingmemory",
        "astrbot_plugin_mnemosyne",
        "astrbot_plugin_self_learning",
    ):
        (data / "plugins" / module).mkdir(parents=True)
    (install / "legal").mkdir(parents=True)
    (install / "legal" / "feature-set.json").write_text(
        json.dumps(
            {
                "features": [
                        {
                            "module": "astrbot_plugin_livingmemory",
                            "display_name": "长期记忆",
                            "configuration": "schema-and-page",
                            "description": "长期记忆",
                            "default_enabled": True,
                        },
                        {
                            "module": "astrbot_plugin_mnemosyne",
                            "display_name": "外部向量记忆",
                            "configuration": "schema",
                            "description": "进阶替代",
                            "default_enabled": True,
                        },
                        {
                            "module": "astrbot_plugin_self_learning",
                            "display_name": "情感与关系",
                            "configuration": "page",
                            "description": "精简情感上下文",
                            "default_enabled": True,
                        },
                ]
            }
        ),
        encoding="utf-8",
    )
    provider = OpenAIProviderProfile(
        api_key="customer-secret",
        api_base="https://api.example.com/v1",
        model="chat-model",
    )

    write_runtime_configs(install, provider_profile=provider)

    engine = json.loads((data / "cmd_config.json").read_text(encoding="utf-8"))
    living = json.loads(
        (data / "config" / "astrbot_plugin_livingmemory_config.json").read_text(
            encoding="utf-8"
        )
    )
    emotion = json.loads(
        (data / "config" / "astrbot_plugin_self_learning_config.json").read_text(
            encoding="utf-8"
        )
    )

    assert "astrbot_plugin_livingmemory" in engine["plugin_set"]
    assert "astrbot_plugin_mnemosyne" not in engine["plugin_set"]
    assert living["provider_settings"]["llm_provider_id"] == provider.model_id
    assert living["recall_engine"]["auto_recall_mode"] == "smart"
    assert living["recall_engine"]["vector_search_timeout_seconds"] == 0.8
    assert living["graph_memory"]["enabled"] is False
    assert "上次说了什么" in living["recall_engine"]["explicit_recall_phrases"]
    assert emotion["Self_Learning_Basic"]["enable_message_capture"] is False
    assert emotion["Advanced_Settings"]["save_raw_messages"] is False
    assert emotion["Runtime_Internal_Settings"]["llm_hook_profile"] == "compact"
    assert emotion["Integration_Settings"]["delegate_memory_to_livingmemory"] is True

    living["recall_engine"]["auto_recall_mode"] = "always"
    (data / "config" / "astrbot_plugin_livingmemory_config.json").write_text(
        json.dumps(living), encoding="utf-8"
    )
    write_runtime_configs(install, provider_profile=provider)
    preserved = json.loads(
        (data / "config" / "astrbot_plugin_livingmemory_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert preserved["recall_engine"]["auto_recall_mode"] == "always"


def test_provider_update_preserves_extensions_but_enforces_reply_timeout(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "provider_sources": [
                    {
                        "id": "jyjapi",
                        "key": ["old-secret"],
                        "api_base": "https://old.example/v1",
                        "custom_headers": {"X-Customer": "preserved"},
                        "timeout": 240,
                    }
                ],
                "provider": [
                    {
                        "id": "jyjapi/grok-4.6",
                        "provider_source_id": "jyjapi",
                        "model": "grok-4.6",
                        "modalities": ["text", "image", "audio", "tool_use"],
                        "max_context_tokens": 262144,
                        "custom_extra_body": {"reasoning_effort": "high"},
                    }
                ],
                "provider_settings": {
                    "default_provider_id": "jyjapi/grok-4.6",
                    "fallback_chat_models": ["legacy/slow-a", "legacy/slow-b"],
                },
            }
        ),
        encoding="utf-8",
    )
    profile = OpenAIProviderProfile(
        api_key="new-secret",
        api_base="https://api.xiuxianjyj.xin/v1",
        model="grok-4.6",
        source_id="jyjapi",
    )

    write_runtime_configs(install, provider_profile=profile)

    payload = json.loads(path.read_text(encoding="utf-8"))
    source = payload["provider_sources"][0]
    model = payload["provider"][0]
    assert source["key"] == ["new-secret"]
    assert source["api_base"] == "https://api.xiuxianjyj.xin/v1"
    assert source["custom_headers"] == {"X-Customer": "preserved"}
    assert source["timeout"] == 35
    assert model["max_context_tokens"] == 262144
    assert model["custom_extra_body"] == {"reasoning_effort": "high"}
    assert payload["provider_settings"]["default_provider_id"] == (
        "jyjapi/grok-4.6"
    )
    assert payload["provider_settings"]["request_max_retries"] == 1
    assert payload["provider_settings"]["fallback_chat_models"] == []
