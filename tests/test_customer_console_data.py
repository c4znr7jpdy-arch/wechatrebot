import json
from pathlib import Path

from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.customer_config import (
    RobotSettings,
    load_plugins,
    load_provider_profile,
    load_robot_settings,
    save_provider_profile,
    save_robot_settings,
    set_plugin_enabled,
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


def test_plugin_list_only_uses_customer_names_and_persists_toggle(tmp_path: Path) -> None:
    install, _ = _install(tmp_path)
    plugin_root = install / "app" / "astrbot" / "data" / "plugins"
    (plugin_root / "jiang_commands").mkdir(parents=True)
    (plugin_root / "astrbot_plugin_music").mkdir()
    (plugin_root / "astrbot_plugin_hapi_connector_disabled").mkdir()

    plugins = load_plugins(install)
    assert {item.display_name for item in plugins} == {"常用指令", "音乐点播"}
    assert all("astrbot" not in item.display_name.casefold() for item in plugins)
    set_plugin_enabled(install, "astrbot_plugin_music", False)
    state = {item.module_name: item.enabled for item in load_plugins(install)}
    assert state["astrbot_plugin_music"] is False
    config = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(encoding="utf-8")
    )
    assert "astrbot_plugin_music" not in config["plugin_set"]


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
