import json
from pathlib import Path

import pytest

from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.customer_config import (
    RobotSettings,
    delete_provider_profile,
    load_plugins,
    load_provider_profile,
    load_provider_services,
    load_web_search_settings,
    load_robot_settings,
    provider_reference_modules,
    replace_provider_references,
    save_provider_profile,
    save_web_search_settings,
    save_robot_settings,
    set_plugin_enabled,
    set_default_provider,
    set_provider_thinking,
)
from wechatrebot_manager.customer_logs import sanitize_log_text, tail_log
from wechatrebot_manager.diagnostics import white_label_terms
from wechatrebot_manager.provider import OpenAIProviderProfile
from wechatrebot_manager.runtime_config import write_runtime_configs
from wechatrebot_manager.runtime_status import RuntimeSnapshot, _find_identity


def _install(tmp_path: Path) -> tuple[Path, Path]:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    brand = BrandProfile("群聊管家", "管家", "小管家").normalized()
    save_brand_profile(install / "config" / "brand.json", brand)
    write_runtime_configs(install, wechat, brand_profile=brand, admin_id="wxid_admin")
    return install, wechat


def test_robot_and_provider_settings_round_trip(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    robot = load_robot_settings(install)
    assert robot.admin_id == "wxid_admin"

    updated = save_robot_settings(
        install,
        RobotSettings(BrandProfile("新机器人", "小新", "新称呼"), "wxid_new"),
    )
    assert updated.profile.display_name == "新机器人"
    assert load_robot_settings(install).admin_id == "wxid_new"

    expected = OpenAIProviderProfile(
        api_key="customer-key",
        api_base="https://api.example.com/v1",
        model="chat-model",
    )
    save_provider_profile(install, expected)
    loaded = load_provider_profile(install)
    assert loaded is not None
    assert loaded.api_key == "customer-key"
    assert loaded.model == "chat-model"


def test_web_search_settings_round_trip_and_key_redaction(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    initial = load_web_search_settings(install)
    assert initial["enabled"] is False
    assert initial["provider"] == "tavily"
    assert initial["providers"]["bocha"]["configured"] is False

    saved = save_web_search_settings(
        install,
        {"enabled": True, "provider": "bocha", "link_enabled": True, "key": "bocha-key"},
    )
    assert saved["enabled"] is True
    assert saved["provider"] == "bocha"
    assert saved["link_enabled"] is True
    assert saved["providers"]["bocha"]["configured"] is True
    assert "key" not in saved["providers"]["bocha"]

    config_path = install / "app/astrbot/data/cmd_config.json"
    engine = json.loads(config_path.read_text(encoding="utf-8"))
    assert engine["provider_settings"]["web_search"] is True
    assert engine["provider_settings"]["websearch_provider"] == "bocha"
    assert engine["provider_settings"]["websearch_bocha_key"] == ["bocha-key"]

    cleared = save_web_search_settings(
        install,
        {"enabled": False, "provider": "bocha", "link_enabled": False, "clear_key": True},
    )
    assert cleared["enabled"] is False
    assert cleared["providers"]["bocha"]["configured"] is False
    engine = json.loads(config_path.read_text(encoding="utf-8"))
    assert engine["provider_settings"]["websearch_bocha_key"] == []


def test_multiple_model_services_share_one_catalog_and_default(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    grok = OpenAIProviderProfile(
        api_key="free-service-key",
        api_base="https://api.xiuxianjyj.xin/v1",
        model="grok-4.6",
        source_id="jyjapi",
    )
    deepseek = OpenAIProviderProfile(
        api_key="developer-key",
        api_base="https://api.deepseek.com/v1",
        model="deepseek-v4-flash",
        source_id="deepseek",
    )
    save_provider_profile(install, grok)
    save_provider_profile(install, deepseek, make_default=False)

    services = load_provider_services(install)
    assert [item.display_name for item in services] == [
        "免费 Grok 4.6",
        "DeepSeek · v4-flash",
    ]
    assert services[0].is_default is True
    assert all("api_key" not in item.to_public_dict() for item in services)

    set_default_provider(install, deepseek.model_id)
    loaded = load_provider_profile(install)
    assert loaded is not None
    assert loaded.api_key == deepseek.api_key
    assert loaded.api_base == deepseek.api_base
    assert loaded.model == deepseek.model
    assert loaded.source_id == deepseek.source_id
    assert loaded.thinking_enabled is True
    engine = json.loads(
        (install / "app/astrbot/data/cmd_config.json").read_text(encoding="utf-8")
    )
    assert engine["provider_settings"]["request_max_retries"] == 1
    assert engine["provider_settings"]["fallback_chat_models"] == []
    delete_provider_profile(install, grok.model_id)
    assert [item.provider_id for item in load_provider_services(install)] == [
        deepseek.model_id
    ]


def test_mimo_thinking_offline_save_and_card_switch_preserve_extra_fields(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    profile = OpenAIProviderProfile(
        api_key="mimo-secret",
        api_base="https://token-plan-cn.xiaomimimo.com/v1",
        model="mimo-v2.5",
        source_id="custom_mimo",
        thinking_enabled=False,
    )
    save_provider_profile(install, profile)
    config_path = install / "app/astrbot/data/cmd_config.json"
    engine = json.loads(config_path.read_text(encoding="utf-8"))
    model = next(item for item in engine["provider"] if item["id"] == profile.model_id)
    assert model["custom_extra_body"] == {"thinking": {"type": "disabled"}}
    model["custom_extra_body"]["safe_extra"] = "keep"
    config_path.write_text(json.dumps(engine), encoding="utf-8")

    set_provider_thinking(install, profile.model_id, True)
    service = next(item for item in load_provider_services(install) if item.provider_id == profile.model_id)
    assert service.to_public_dict()["thinking_enabled"] is True
    assert service.to_public_dict()["thinking_supported"] is True
    assert load_provider_profile(install).thinking_enabled is True
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    current = next(item for item in updated["provider"] if item["id"] == profile.model_id)
    assert current["custom_extra_body"] == {"thinking": {"type": "enabled"}, "safe_extra": "keep"}
    assert updated["provider_sources"][0]["key"] == ["mimo-secret"]
    assert updated["provider_settings"]["default_provider_id"] == profile.model_id

    save_provider_profile(install, OpenAIProviderProfile(
        api_key="mimo-secret", api_base=profile.api_base, model=profile.model,
        source_id=profile.source_id, thinking_enabled=False,
    ))
    refreshed = json.loads(config_path.read_text(encoding="utf-8"))
    assert refreshed["provider"][0]["custom_extra_body"] == {"thinking": {"type": "disabled"}, "safe_extra": "keep"}


def test_unknown_model_keeps_universal_thinking_preference_without_request_fields(
    tmp_path: Path,
) -> None:
    install, _ = _install(tmp_path)
    profile = OpenAIProviderProfile(
        api_key="custom-secret",
        api_base="https://example.com/v1",
        model="custom-chat",
        source_id="custom_model",
        thinking_enabled=False,
    )
    save_provider_profile(install, profile)

    service = next(
        item for item in load_provider_services(install)
        if item.provider_id == profile.model_id
    )
    assert service.thinking_enabled is False
    assert service.to_public_dict()["thinking_supported"] is False
    engine = json.loads(
        (install / "app/astrbot/data/cmd_config.json").read_text(encoding="utf-8")
    )
    model = next(item for item in engine["provider"] if item["id"] == profile.model_id)
    assert model["custom_extra_body"] == {}

    assert set_provider_thinking(install, profile.model_id, True) is False
    refreshed = next(
        item for item in load_provider_services(install)
        if item.provider_id == profile.model_id
    )
    assert refreshed.thinking_enabled is True
    manager = json.loads((install / "config/manager.json").read_text(encoding="utf-8"))
    assert manager["provider_thinking_modes"][profile.model_id] is True


def test_provider_references_are_named_and_migrated_before_deletion(
    tmp_path: Path,
) -> None:
    install, _ = _install(tmp_path)
    grok = OpenAIProviderProfile(
        api_key="free-service-key",
        api_base="https://api.xiuxianjyj.xin/v1",
        model="grok-4.6",
        source_id="jyjapi",
    )
    deepseek = OpenAIProviderProfile(
        api_key="developer-key",
        api_base="https://api.deepseek.com/v1",
        model="deepseek-chat",
        source_id="deepseek",
    )
    save_provider_profile(install, grok)
    save_provider_profile(install, deepseek, make_default=False)
    config_path = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "config"
        / "astrbot_plugin_livingmemory_config.json"
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "summary_provider_id": grok.model_id,
                "nested": {"providers": [grok.model_id, deepseek.model_id]},
                "description": f"不要替换包含 {grok.model_id} 的普通文字",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    services = load_provider_services(install)
    current = next(item for item in services if item.provider_id == grok.model_id)
    assert current.reference_modules == ("astrbot_plugin_livingmemory",)
    assert current.to_public_dict()["references"] == ["长期记忆"]
    assert provider_reference_modules(install, grok.model_id, strict=True) == (
        "astrbot_plugin_livingmemory",
    )

    assert replace_provider_references(
        install, grok.model_id, deepseek.model_id
    ) == ("astrbot_plugin_livingmemory",)
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert updated["summary_provider_id"] == deepseek.model_id
    assert updated["nested"]["providers"] == [
        deepseek.model_id,
        deepseek.model_id,
    ]
    assert updated["description"] == f"不要替换包含 {grok.model_id} 的普通文字"

    set_default_provider(install, deepseek.model_id)
    delete_provider_profile(install, grok.model_id)
    assert [item.provider_id for item in load_provider_services(install)] == [
        deepseek.model_id
    ]


def test_plugin_list_only_uses_customer_names_and_persists_toggle(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    plugin_root = install / "app" / "astrbot" / "data" / "plugins"
    (plugin_root / "jiang_commands").mkdir(parents=True)
    (plugin_root / "astrbot_plugin_music").mkdir()
    (plugin_root / "astrbot_plugin_hapi_connector_disabled").mkdir()

    plugins = load_plugins(install)
    assert {item.display_name for item in plugins} == {"常用指令", "点歌"}
    assert all("astrbot" not in item.display_name.casefold() for item in plugins)
    set_plugin_enabled(install, "astrbot_plugin_music", False)
    state = {item.module_name: item.enabled for item in load_plugins(install)}
    assert state["astrbot_plugin_music"] is False
    config = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(encoding="utf-8")
    )
    assert "astrbot_plugin_music" not in config["plugin_set"]


def test_plugin_toggle_preserves_hidden_foundations_and_retires_old_vector_memory(
    tmp_path: Path,
) -> None:
    install, _ = _install(tmp_path)
    plugin_root = install / "app" / "astrbot" / "data" / "plugins"
    for module in (
        "astrbot_plugin_music",
        "astrbot_plugin_livingmemory",
        "astrbot_plugin_self_learning",
        "astrbot_plugin_mnemosyne",
    ):
        (plugin_root / module).mkdir(parents=True, exist_ok=True)
    config_path = install / "app" / "astrbot" / "data" / "cmd_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["plugin_set"] = ["*"]
    config_path.write_text(json.dumps(config), encoding="utf-8")

    set_plugin_enabled(install, "astrbot_plugin_music", False)
    updated = json.loads(config_path.read_text(encoding="utf-8"))
    assert "astrbot_plugin_livingmemory" in updated["plugin_set"]
    assert "astrbot_plugin_self_learning" in updated["plugin_set"]
    assert "astrbot_plugin_mnemosyne" not in {
        item.module_name for item in load_plugins(install)
    }
    with pytest.raises(ValueError, match="不存在或尚未交付"):
        set_plugin_enabled(install, "astrbot_plugin_mnemosyne", True)


def test_logs_and_diagnostics_remove_implementation_brand(tmp_path: Path) -> None:
    path = tmp_path / "service.log"
    path.write_text("AstrBot started\nnew-hook ready\napi_key=private", encoding="utf-8")
    text = tail_log(path)
    assert "astrbot" not in text.casefold()
    assert "hook" not in text.casefold()
    assert "private" not in text
    result = white_label_terms({"astrbot_ws": "AstrBot", "new-hook": "ready"})
    serialized = json.dumps(result).casefold()
    assert "astrbot" not in serialized
    assert "hook" not in serialized


def test_runtime_snapshot_and_identity_extraction() -> None:
    assert _find_identity({"data": {"wxid": "wxid_bot"}}) == "wxid_bot"
    assert RuntimeSnapshot(True, True, "wxid_bot").online is True
    assert RuntimeSnapshot(True, True, "").stage == "login"
