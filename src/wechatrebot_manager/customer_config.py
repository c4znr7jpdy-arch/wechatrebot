from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .brand import BrandProfile
from .json_io import write_json_atomic
from .provider import OpenAIProviderProfile
from .runtime_config import normalize_admin_id, write_runtime_configs


def _object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"配置文件无法读取：{path.name}：{exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"配置文件格式无效：{path.name}")
    return value


def _paths(root: str | Path) -> tuple[Path, Path, Path]:
    install = Path(root).expanduser().resolve()
    return (
        install,
        install / "config" / "manager.json",
        install / "app" / "astrbot" / "data" / "cmd_config.json",
    )


@dataclass(frozen=True, slots=True)
class RobotSettings:
    profile: BrandProfile
    admin_id: str = ""


def load_robot_settings(root: str | Path) -> RobotSettings:
    install, _, engine_path = _paths(root)
    brand_payload = _object(install / "config" / "brand.json")
    profile = BrandProfile(
        display_name=str(brand_payload.get("display_name", "姜小妹机器人")),
        short_name=str(brand_payload.get("short_name", "姜小妹")),
        persona_name=str(brand_payload.get("persona_name", "姜小妹")),
        avatar_path=str(brand_payload.get("avatar_path", "")),
        welcome_text=str(brand_payload.get("welcome_text", "")),
        help_title=str(brand_payload.get("help_title", "")),
    ).normalized()
    engine = _object(engine_path)
    admins = engine.get("admins_id")
    admin_id = str(admins[0]) if isinstance(admins, list) and admins else ""
    return RobotSettings(profile=profile, admin_id=admin_id)


def save_robot_settings(root: str | Path, settings: RobotSettings) -> RobotSettings:
    install, manager_path, _ = _paths(root)
    manager = _object(manager_path)
    wechat_path = str(manager.get("wechat_exe_path", ""))
    normalized_admin = normalize_admin_id(settings.admin_id) or ""
    from .brand import install_brand_profile

    profile = install_brand_profile(install, settings.profile)
    write_runtime_configs(
        install,
        wechat_path or None,
        wechat_version=str(manager.get("wechat_version") or "") or None,
        brand_profile=profile,
        admin_id=normalized_admin,
    )
    return RobotSettings(profile=profile, admin_id=normalized_admin)


def load_provider_profile(root: str | Path) -> OpenAIProviderProfile | None:
    _, _, engine_path = _paths(root)
    engine = _object(engine_path)
    settings = engine.get("provider_settings")
    default_id = (
        str(settings.get("default_provider_id", ""))
        if isinstance(settings, dict)
        else ""
    )
    providers = engine.get("provider")
    sources = engine.get("provider_sources")
    provider_items = providers if isinstance(providers, list) else []
    source_items = sources if isinstance(sources, list) else []
    provider = next(
        (item for item in provider_items if isinstance(item, dict) and item.get("id") == default_id),
        None,
    )
    if not isinstance(provider, dict):
        return None
    source_id = str(provider.get("provider_source_id", ""))
    source = next(
        (item for item in source_items if isinstance(item, dict) and item.get("id") == source_id),
        None,
    )
    if not isinstance(source, dict):
        return None
    keys = source.get("key")
    api_key = str(keys[0]) if isinstance(keys, list) and keys else ""
    profile = OpenAIProviderProfile(
        api_key=api_key,
        api_base=str(source.get("api_base", "")),
        model=str(provider.get("model", "")),
        source_id=source_id or "commercial_openai",
    ).normalized()
    return profile if not profile.validate() else None


def save_provider_profile(root: str | Path, profile: OpenAIProviderProfile) -> OpenAIProviderProfile:
    install, manager_path, _ = _paths(root)
    normalized = profile.normalized()
    errors = normalized.validate()
    if errors:
        raise ValueError("；".join(errors))
    manager = _object(manager_path)
    wechat_path = str(manager.get("wechat_exe_path", ""))
    write_runtime_configs(
        install,
        wechat_path or None,
        wechat_version=str(manager.get("wechat_version") or "") or None,
        provider_profile=normalized,
    )
    return normalized


@dataclass(frozen=True, slots=True)
class PluginEntry:
    module_name: str
    display_name: str
    enabled: bool


_PLUGIN_LABELS = {
    "astrbot_plugin_angel_heart": "长期记忆",
    "astrbot_plugin_apis": "实用接口",
    "astrbot_plugin_bittorrent": "资源检索",
    "astrbot_plugin_kimi_web_search": "联网搜索",
    "astrbot_plugin_mimo_tts_clone": "语音克隆",
    "astrbot_plugin_mnemosyne": "记忆管理",
    "astrbot_plugin_music": "点歌",
    "astrbot_plugin_parser": "链接解析",
    "astrbot_plugin_patch_at_wxid": "群聊兼容",
    "astrbot_plugin_proactive_chat": "主动聊天",
    "astrbot_plugin_qq_group_daily_analysis": "群聊分析",
    "astrbot_plugin_self_iterative_core": "自我优化",
    "astrbot_plugin_twitter": "动态订阅",
    "astrbot_plugin_zhenxunribao": "群聊日报",
    "jiang_12123_notify": "车辆提醒",
    "jiang_capability_router": "功能调度",
    "jiang_commands": "常用指令",
    "jiang_group_notice": "群公告助手",
    "jiang_image": "图片生成",
    "jiang_lol_match": "战绩查询",
    "jiang_mimo_emoji_voice": "表情语音",
    "jiang_mystool": "游戏工具",
    "jiang_raid_signup": "活动报名",
    "jiang_repeater": "趣味复读",
    "jiang_rocom": "运势娱乐",
    "jiang_schedule": "定时任务",
    "jiang_server_status": "服务状态",
    "jiang_short_drama": "短剧",
    "jiang_tide": "潮汐查询",
    "jiang_video_understanding": "视频理解",
    "jiang_yanyun_checkin": "签到助手",
    "meme_manager": "表情管理",
}

_HIDDEN_CUSTOMER_PLUGINS = {
    "astrbot_plugin_livingmemory",
    "astrbot_plugin_self_learning",
}


def _plugin_display_name(module_name: str) -> str:
    if module_name in _PLUGIN_LABELS:
        return _PLUGIN_LABELS[module_name]
    value = module_name
    for prefix in ("astrbot_plugin_", "jiang_"):
        if value.startswith(prefix):
            value = value[len(prefix) :]
    return value.replace("_", " ").strip().title() or "扩展功能"


def load_plugins(root: str | Path) -> tuple[PluginEntry, ...]:
    install, _, engine_path = _paths(root)
    plugin_root = install / "app" / "astrbot" / "data" / "plugins"
    engine = _object(engine_path)
    configured = engine.get("plugin_set", ["*"])
    enabled_names = {str(item) for item in configured} if isinstance(configured, list) else {"*"}
    entries: list[PluginEntry] = []
    if plugin_root.is_dir():
        for path in sorted(plugin_root.iterdir(), key=lambda item: item.name.casefold()):
            if (
                not path.is_dir()
                or path.name.startswith((".", "__"))
                or path.name in {"data", "logs"}
                or path.name.endswith("_disabled")
                or path.name in _HIDDEN_CUSTOMER_PLUGINS
            ):
                continue
            entries.append(
                PluginEntry(
                    module_name=path.name,
                    display_name=_plugin_display_name(path.name),
                    enabled="*" in enabled_names or path.name in enabled_names,
                )
            )
    return tuple(entries)


def set_plugin_enabled(root: str | Path, module_name: str, enabled: bool) -> None:
    _, _, engine_path = _paths(root)
    engine = _object(engine_path)
    plugins = load_plugins(root)
    available = {item.module_name for item in plugins}
    if module_name not in available:
        raise ValueError("所选功能不存在或尚未交付。")
    configured = engine.get("plugin_set", ["*"])
    names = {str(item) for item in configured} if isinstance(configured, list) else {"*"}
    if "*" in names:
        names = set(available)
    if enabled:
        names.add(module_name)
    else:
        names.discard(module_name)
    engine["plugin_set"] = sorted(names)
    write_json_atomic(engine_path, engine)


__all__ = [
    "PluginEntry",
    "RobotSettings",
    "load_plugins",
    "load_provider_profile",
    "load_robot_settings",
    "save_provider_profile",
    "save_robot_settings",
    "set_plugin_enabled",
]
