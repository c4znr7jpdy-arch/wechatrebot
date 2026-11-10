import ast
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechatrebot_manager.runtime_config import (
    RuntimePorts,
    normalize_admin_id,
    write_runtime_configs,
)
from wechatrebot_manager.brand import BrandProfile
from wechatrebot_manager.provider import OpenAIProviderProfile
from wechatrebot_manager.customer_config import global_plugin_states, set_plugin_enabled
from wechatrebot_manager.feature_policy import (
    GLOBAL_DISABLED_PLUGINS_KEY, global_plugin_state_path, load_global_disabled_plugins,
)
from test_hot_config_runtime import _runtime_function


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


def test_saving_customer_settings_preserves_custom_runtime_ports(tmp_path: Path) -> None:
    from wechatrebot_manager.customer_config import (
        RobotSettings, save_provider_profile, save_robot_settings,
    )

    ports = RuntimePorts(dashboard=16185, astrbot_ws=16200, bridge_callback=15000, hook_api=29088)
    write_runtime_configs(tmp_path, ports=ports)
    save_provider_profile(tmp_path, OpenAIProviderProfile(
        api_key="test-secret", api_base="https://example.com/v1", model="chat-model",
    ))
    save_robot_settings(tmp_path, RobotSettings(
        profile=BrandProfile(display_name="测试助手", short_name="助手", persona_name="测试助手"),
        admin_id="wxid_test",
    ))

    manager = json.loads((tmp_path / "config/manager.json").read_text("utf-8"))
    bridge = json.loads((tmp_path / "app/bridge/config.json").read_text("utf-8"))
    engine = json.loads((tmp_path / "app/astrbot/data/cmd_config.json").read_text("utf-8"))
    assert manager["ports"] == {"dashboard": 16185, "astrbot_ws": 16200, "bridge_callback": 15000, "hook_api": 29088}
    assert bridge["hook_api_port"] == ports.hook_api
    assert bridge["callback_port"] == ports.bridge_callback
    assert bridge["astrbot_ws_url"] == "ws://127.0.0.1:16200/ws"
    assert engine["dashboard"]["port"] == ports.dashboard
    assert engine["platform"][0]["ws_reverse_port"] == ports.astrbot_ws


@pytest.mark.parametrize("invalid", ["6200", True, [], 0, 65536])
def test_invalid_saved_ports_fail_before_writing_config(tmp_path: Path, invalid) -> None:
    write_runtime_configs(tmp_path)
    path = tmp_path / "config/manager.json"
    manager = json.loads(path.read_text("utf-8"))
    manager["ports"]["astrbot_ws"] = invalid
    path.write_text(json.dumps(manager), encoding="utf-8")
    bridge = tmp_path / "app/bridge/config.json"
    before = bridge.read_bytes()
    with pytest.raises(ValueError, match="端口"):
        write_runtime_configs(tmp_path)
    assert bridge.read_bytes() == before


@pytest.mark.parametrize("service_kind", ["embedding", "image", "voice"])
def test_memory_only_seeds_chat_model_and_repairs_non_chat_binding(tmp_path: Path, service_kind: str) -> None:
    data = tmp_path / "app/astrbot/data"
    (data / "plugins/astrbot_plugin_livingmemory").mkdir(parents=True)
    non_chat = OpenAIProviderProfile(
        api_key="test-secret", api_base="https://example.com/v1", model="non-chat-model",
        service_kind=service_kind,
    )
    write_runtime_configs(tmp_path, provider_profile=non_chat, provider_make_default=False)
    path = data / "config/astrbot_plugin_livingmemory_config.json"
    living = json.loads(path.read_text("utf-8"))
    assert not living.get("provider_settings", {}).get("llm_provider_id")
    living["provider_settings"] = {"llm_provider_id": non_chat.model_id}
    path.write_text(json.dumps(living), encoding="utf-8")
    chat = OpenAIProviderProfile(
        api_key="test-secret", api_base="https://example.com/v1", model="chat-model",
    )
    write_runtime_configs(tmp_path, provider_profile=chat)
    assert json.loads(path.read_text("utf-8"))["provider_settings"]["llm_provider_id"] == chat.model_id
    living["provider_settings"]["llm_provider_id"] = "customer-chat-model"
    path.write_text(json.dumps(living), encoding="utf-8")
    write_runtime_configs(tmp_path, provider_profile=chat)
    assert json.loads(path.read_text("utf-8"))["provider_settings"]["llm_provider_id"] == "customer-chat-model"


@pytest.mark.parametrize("current_id", ["", None, "custom/unknown"])
def test_memory_repairs_empty_binding_but_preserves_custom_adapter(tmp_path: Path, current_id) -> None:
    data = tmp_path / "app/astrbot/data"
    (data / "plugins/astrbot_plugin_livingmemory").mkdir(parents=True)
    write_runtime_configs(tmp_path)
    engine_path = data / "cmd_config.json"
    engine = json.loads(engine_path.read_text("utf-8"))
    engine["provider"] = [{"id": "custom/unknown", "type": "custom_chat_adapter"}]
    engine_path.write_text(json.dumps(engine), encoding="utf-8")
    path = data / "config/astrbot_plugin_livingmemory_config.json"
    path.write_text(json.dumps({"provider_settings": {"llm_provider_id": current_id}}), encoding="utf-8")
    chat = OpenAIProviderProfile(
        api_key="test-secret", api_base="https://example.com/v1", model="chat-model",
    )
    write_runtime_configs(tmp_path, provider_profile=chat)
    assert json.loads(path.read_text("utf-8"))["provider_settings"]["llm_provider_id"] == (current_id or chat.model_id)


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
    assert updated_astrbot["plugin_set"] == [
        "astrbot_plugin_angel_heart",
        "astrbot_plugin_music",
    ]


@pytest.mark.parametrize("kind,make_default", [
    ("image", False), ("embedding", False), ("voice", False), ("chat", False),
])
def test_extra_models_do_not_seed_group_analysis(
    tmp_path: Path, kind: str, make_default: bool,
) -> None:
    (tmp_path / "app/astrbot/data/plugins/astrbot_plugin_angel_heart").mkdir(parents=True)
    write_runtime_configs(tmp_path, provider_profile=OpenAIProviderProfile(
        api_key="test-key", api_base="https://example.test/v1", model="extra-model",
        service_kind=kind,
    ), provider_make_default=make_default)
    path = tmp_path / "app/astrbot/data/config/astrbot_plugin_angel_heart_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert not config.get("analyzer_model")


def test_customer_features_stay_loaded_for_per_chat_scoping(tmp_path: Path) -> None:
    install = tmp_path / "install"
    legal = install / "legal"
    legal.mkdir(parents=True)
    (legal / "feature-set.json").write_text(json.dumps({"features": [
        {"module": "jiang_commands", "display_name": "指令", "configuration": "none", "default_enabled": True},
        {"module": "jiang_schedule", "display_name": "定时任务", "configuration": "none", "default_enabled": True},
        {"module": "astrbot_plugin_self_iterative_core", "display_name": "实验", "configuration": "none", "default_enabled": False, "visible": False},
    ]}), encoding="utf-8")
    path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"plugin_set": ["jiang_commands"]}), encoding="utf-8")
    write_runtime_configs(install)
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["plugin_set"] == ["jiang_commands", "jiang_schedule"]
    assert updated["wechatrebot_feature_scope_version"] == 2
    updated["plugin_set"] = ["jiang_commands"]
    path.write_text(json.dumps(updated), encoding="utf-8")
    write_runtime_configs(install)
    assert json.loads(path.read_text(encoding="utf-8"))["plugin_set"] == [
        "jiang_commands",
        "jiang_schedule",
    ]


def test_runtime_config_preserves_explicit_global_disables(tmp_path: Path) -> None:
    install = tmp_path / "install"
    legal = install / "legal"
    legal.mkdir(parents=True)
    (legal / "feature-set.json").write_text(json.dumps({"features": [
        {"module": "jiang_commands", "display_name": "指令", "configuration": "none"},
        {"module": "jiang_schedule", "display_name": "定时任务", "configuration": "none"},
    ]}), encoding="utf-8")
    path = install / "app/astrbot/data/cmd_config.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "plugin_set": ["*"], "wechatrebot_disabled_plugins": ["jiang_schedule"],
    }), encoding="utf-8")
    write_runtime_configs(install)
    write_runtime_configs(install, admin_id="wxid_admin")
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["plugin_set"] == ["jiang_commands"]
    assert updated["wechatrebot_disabled_plugins"] == ["jiang_schedule"]
    assert updated["admins_id"] == ["wxid_admin"]


def test_global_disable_survives_actual_engine_integrity_cleanup_and_manager_save(tmp_path: Path) -> None:
    install = tmp_path / "install"
    legal = install / "legal"
    legal.mkdir(parents=True)
    modules = ("jiang_commands", "jiang_schedule")
    (legal / "feature-set.json").write_text(json.dumps({"features": [
        {"module": module, "display_name": module, "configuration": "none"}
        for module in modules
    ]}), encoding="utf-8")
    for module in modules:
        (install / "app/astrbot/data/plugins" / module).mkdir(parents=True)
    write_runtime_configs(install)
    assert not global_plugin_state_path(install).exists()
    set_plugin_enabled(install, "jiang_schedule", False)
    state_bytes = global_plugin_state_path(install).read_bytes()
    path = install / "app/astrbot/data/cmd_config.json"
    config = json.loads(path.read_text(encoding="utf-8"))
    assert config[GLOBAL_DISABLED_PLUGINS_KEY] == ["jiang_schedule"]

    # Execute the pinned engine's actual startup integrity method, without
    # importing AstrBot or starting its process. Its accepted plugin_set field
    # remains while the manager's unknown marker is removed.
    engine_config = Path(__file__).parents[1] / "vendor/engine/astrbot/core/config"
    defaults_tree = ast.parse((engine_config / "default.py").read_text("utf-8-sig"))
    defaults = next(node.value for node in defaults_tree.body if isinstance(node, ast.Assign)
                    and any(isinstance(target, ast.Name) and target.id == "DEFAULT_CONFIG" for target in node.targets))
    default_keys = {ast.literal_eval(key) for key in defaults.keys}
    assert GLOBAL_DISABLED_PLUGINS_KEY not in default_keys and "plugin_set" in default_keys
    source = (engine_config / "astrbot_config.py").read_text("utf-8-sig")
    check = _runtime_function(source, "check_config_integrity", {"logger": logging.getLogger(__name__)})
    owner = SimpleNamespace()
    owner.check_config_integrity = lambda reference, current, key="": check(owner, reference, current, key)
    assert check(owner, {"plugin_set": ["*"]}, config) is True
    assert GLOBAL_DISABLED_PLUGINS_KEY not in config
    assert config["plugin_set"] == ["jiang_commands"]
    path.write_text(json.dumps(config), encoding="utf-8")
    assert global_plugin_states(install)["jiang_schedule"] is False
    write_runtime_configs(install, admin_id="wxid_updated_admin")
    updated = json.loads(path.read_text(encoding="utf-8"))
    assert updated["plugin_set"] == ["jiang_commands"]
    assert global_plugin_state_path(install).read_bytes() == state_bytes
    assert load_global_disabled_plugins(install) == frozenset({"jiang_schedule"})


def test_legacy_disable_migrates_once_and_stale_engine_marker_cannot_override_it(tmp_path: Path) -> None:
    legacy = {GLOBAL_DISABLED_PLUGINS_KEY: ["jiang_schedule"]}
    assert load_global_disabled_plugins(tmp_path, {}) == frozenset()
    assert not global_plugin_state_path(tmp_path).exists()
    assert load_global_disabled_plugins(tmp_path, legacy) == frozenset({"jiang_schedule"})
    assert global_plugin_state_path(tmp_path).is_file()
    assert load_global_disabled_plugins(tmp_path, {GLOBAL_DISABLED_PLUGINS_KEY: []}) == frozenset({"jiang_schedule"})
    assert json.loads(global_plugin_state_path(tmp_path).read_text(encoding="utf-8")) == {
        "disabled_plugins": ["jiang_schedule"],
    }


@pytest.mark.parametrize("payload", ["not JSON", "[]", "{}", '{"disabled_plugins":"jiang_schedule"}'])
def test_corrupt_durable_global_policy_never_falls_back_to_live_marker(tmp_path: Path, payload: str) -> None:
    path = global_plugin_state_path(tmp_path)
    path.parent.mkdir(parents=True)
    path.write_text(payload, encoding="utf-8")
    with pytest.raises(ValueError, match="全局功能开关"):
        load_global_disabled_plugins(tmp_path, {GLOBAL_DISABLED_PLUGINS_KEY: []})


@pytest.mark.parametrize("retired_module", ["astrbot_plugin_mnemosyne", "jiang_yanyun_checkin"])
def test_delivery_retires_legacy_plugins_and_loads_private_emotion_defaults(
    tmp_path: Path, retired_module: str,
) -> None:
    install = tmp_path / "install"
    data = install / "app" / "astrbot" / "data"
    for module in (
        "astrbot_plugin_livingmemory",
        retired_module,
        "astrbot_plugin_self_learning",
    ):
        (data / "plugins" / module).mkdir(parents=True)
    retired_config = data / "config" / f"{retired_module}_config.json"
    retired_config.parent.mkdir(parents=True)
    retired_config.write_text("{}", encoding="utf-8")
    retired_data = data / "plugin_data" / retired_module
    retired_data.mkdir(parents=True)
    retired_source = (
        install
        / "legal"
        / "corresponding-source"
        / f"astrbot-plugin-{retired_module}.patch"
    )
    retired_source.parent.mkdir(parents=True)
    retired_source.write_text("legacy", encoding="utf-8")
    (install / "legal").mkdir(parents=True, exist_ok=True)
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
                            "module": retired_module,
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
    assert retired_module not in engine["plugin_set"]
    assert not (data / "plugins" / retired_module).exists()
    assert not retired_config.exists()
    assert not retired_data.exists()
    assert not retired_source.exists()
    assert living["provider_settings"]["llm_provider_id"] == provider.model_id
    assert living["recall_engine"]["auto_recall_mode"] == "smart"
    assert living["recall_engine"]["vector_search_timeout_seconds"] == 0.8
    assert living["graph_memory"]["enabled"] is True
    assert "explicit_recall_phrases" not in living["recall_engine"]
    assert emotion["Self_Learning_Basic"]["enable_message_capture"] is False
    assert emotion["Advanced_Settings"]["save_raw_messages"] is False
    assert emotion["Runtime_Internal_Settings"]["llm_hook_profile"] == "compact"
    assert emotion["Integration_Settings"]["delegate_memory_to_livingmemory"] is True

    living["recall_engine"]["auto_recall_mode"] = "always"
    living["graph_memory"]["enabled"] = False
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
    assert preserved["graph_memory"]["enabled"] is False


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


def test_runtime_preparation_repairs_existing_embedding_without_replacing_bindings(tmp_path):
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    write_runtime_configs(tmp_path, wechat)
    path = tmp_path / "app/astrbot/data/cmd_config.json"
    config = json.loads(path.read_text("utf-8"))
    config["provider_sources"] = [{"id": "shared", "type": "openai_chat_completion", "api_base": "https://example.test/v1", "key": ["secret"]}]
    config["provider"] = [{"id": "shared/vector", "provider_source_id": "shared", "model": "vector", "service_kind": "embedding"}]
    path.write_text(json.dumps(config), encoding="utf-8")
    write_runtime_configs(tmp_path, wechat)
    fixed = json.loads(path.read_text("utf-8"))
    assert fixed["provider"][0]["type"] == "openai_embedding"
    assert fixed["provider"][0]["id"] == "shared/vector"
    assert fixed["provider_sources"][0]["embedding_api_key"] == "secret"
    assert fixed["provider_sources"][0]["type"] == "openai_chat_completion"
