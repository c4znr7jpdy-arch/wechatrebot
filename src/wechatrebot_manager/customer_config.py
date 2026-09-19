from __future__ import annotations

import copy
from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from .brand import BrandProfile
from .feature_policy import RETIRED_PLUGIN_MODULES
from .json_io import write_json_atomic
from .provider import (
    DEFAULT_PROVIDER_MODEL,
    DEFAULT_PROVIDER_SOURCE_ID,
    OpenAIProviderProfile,
    PROVIDER_REQUEST_MAX_ATTEMPTS,
    infer_thinking_mode,
    provider_display_name,
    supports_thinking_toggle,
    thinking_mode_matches,
    with_thinking_mode,
)
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


def _provider_thinking_preferences(root: str | Path) -> dict[str, bool]:
    _, manager_path, _ = _paths(root)
    manager = _object(manager_path)
    values = manager.get("provider_thinking_modes")
    if not isinstance(values, dict):
        return {}
    return {
        str(provider_id): enabled
        for provider_id, enabled in values.items()
        if isinstance(provider_id, str) and isinstance(enabled, bool)
    }


def save_provider_thinking_preference(
    root: str | Path, provider_id: str, enabled: bool
) -> None:
    if not isinstance(enabled, bool):
        raise ValueError("思考模式只能选择开启或关闭。")
    _, manager_path, _ = _paths(root)
    manager = _object(manager_path)
    preferences = _provider_thinking_preferences(root)
    preferences[str(provider_id)] = enabled
    manager["provider_thinking_modes"] = preferences
    write_json_atomic(manager_path, manager)


def delete_provider_thinking_preference(root: str | Path, provider_id: str) -> None:
    _, manager_path, _ = _paths(root)
    manager = _object(manager_path)
    preferences = _provider_thinking_preferences(root)
    if str(provider_id) not in preferences:
        return
    preferences.pop(str(provider_id), None)
    manager["provider_thinking_modes"] = preferences
    write_json_atomic(manager_path, manager)


@dataclass(frozen=True, slots=True)
class RobotSettings:
    profile: BrandProfile
    admin_id: str = ""


@dataclass(frozen=True, slots=True)
class ProviderService:
    """One configured chat model as shown in the customer control window."""

    provider_id: str
    source_id: str
    display_name: str
    api_base: str
    model: str
    api_key: str
    is_default: bool
    is_recommended: bool
    reference_modules: tuple[str, ...] = ()
    thinking_enabled: bool = True

    @property
    def thinking_supported(self) -> bool:
        return supports_thinking_toggle(self.api_base, self.model)

    @property
    def configured(self) -> bool:
        return not OpenAIProviderProfile(
            api_key=self.api_key,
            api_base=self.api_base,
            model=self.model,
            source_id=self.source_id,
        ).validate()

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.provider_id,
            "name": self.display_name,
            "api_base": self.api_base,
            "model": self.model,
            "configured": self.configured,
            "api_key_configured": bool(self.api_key),
            "is_default": self.is_default,
            "is_recommended": self.is_recommended,
            "thinking_supported": self.thinking_supported,
            "thinking_enabled": self.thinking_enabled,
            "references": [
                _plugin_display_name(module_name)
                for module_name in self.reference_modules
            ],
        }


def replace_provider_value(
    value: Any, provider_id: str, replacement_provider_id: str
) -> tuple[Any, int]:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        replaced = 0
        for key, item in value.items():
            result[key], count = replace_provider_value(
                item, provider_id, replacement_provider_id
            )
            replaced += count
        return result, replaced
    if isinstance(value, list):
        result = []
        replaced = 0
        for item in value:
            new_item, count = replace_provider_value(
                item, provider_id, replacement_provider_id
            )
            result.append(new_item)
            replaced += count
        return result, replaced
    if value == provider_id:
        return replacement_provider_id, 1
    return value, 0


def _provider_reference_files(
    root: str | Path,
    provider_id: str,
    *,
    strict: bool,
) -> tuple[tuple[str, Path, dict[str, Any]], ...]:
    install = Path(root).expanduser().resolve()
    config_root = install / "app" / "astrbot" / "data" / "config"
    if not config_root.is_dir():
        return ()
    references: list[tuple[str, Path, dict[str, Any]]] = []
    for path in sorted(config_root.glob("*_config.json"), key=lambda item: item.name.casefold()):
        try:
            payload = _object(path)
        except ValueError:
            if strict:
                raise ValueError(
                    f"{path.name} 无法读取；为避免留下失效模型引用，暂不能删除服务。"
                ) from None
            continue
        _, count = replace_provider_value(payload, provider_id, provider_id)
        if count:
            references.append((path.name[: -len("_config.json")], path, payload))
    return tuple(references)


def provider_reference_modules(
    root: str | Path, provider_id: str, *, strict: bool = False
) -> tuple[str, ...]:
    return tuple(
        module_name
        for module_name, _path, _payload in _provider_reference_files(
            root, provider_id, strict=strict
        )
    )


def replace_provider_references(
    root: str | Path,
    provider_id: str,
    replacement_provider_id: str,
) -> tuple[str, ...]:
    if not replacement_provider_id or replacement_provider_id == provider_id:
        raise ValueError("请选择另一个已配置的模型接替当前服务。")
    pending: list[tuple[str, Path, dict[str, Any], dict[str, Any]]] = []
    for module_name, path, original in _provider_reference_files(
        root, provider_id, strict=True
    ):
        updated, count = replace_provider_value(
            copy.deepcopy(original), provider_id, replacement_provider_id
        )
        if count:
            pending.append((module_name, path, original, updated))

    written: list[tuple[Path, dict[str, Any]]] = []
    try:
        for _module_name, path, original, updated in pending:
            write_json_atomic(path, updated)
            written.append((path, original))
    except OSError:
        for path, original in reversed(written):
            try:
                write_json_atomic(path, original)
            except OSError:
                pass
        raise
    return tuple(item[0] for item in pending)


def load_robot_settings(root: str | Path) -> RobotSettings:
    install, _, engine_path = _paths(root)
    brand_payload = _object(install / "config" / "brand.json")
    profile = BrandProfile(
        display_name=str(brand_payload.get("display_name", "小妹")),
        short_name=str(brand_payload.get("short_name", "小妹")),
        persona_name=str(brand_payload.get("persona_name", "小妹")),
        avatar_path=str(brand_payload.get("avatar_path", "")),
        welcome_text=str(brand_payload.get("welcome_text", "")),
        help_title=str(brand_payload.get("help_title", "")),
    ).normalized()
    # Migrate the product's former factory identity in memory. Custom brands are
    # never rewritten; the new value is persisted when the unified identity
    # form is next saved.
    if (
        profile.display_name == "\u59dc\u5c0f\u59b9\u673a\u5668\u4eba"
        and profile.short_name == "\u59dc\u5c0f\u59b9"
        and profile.persona_name == "\u59dc\u5c0f\u59b9"
    ):
        profile = BrandProfile(
            display_name="小妹",
            short_name="小妹",
            persona_name="小妹",
            avatar_path=profile.avatar_path,
            welcome_text=(
                profile.welcome_text
                .replace("\u59dc\u5c0f\u59b9\u673a\u5668\u4eba", "小妹")
                .replace("\u59dc\u5c0f\u59b9", "小妹")
            ),
            help_title=(
                profile.help_title
                .replace("\u59dc\u5c0f\u59b9\u673a\u5668\u4eba", "小妹")
                .replace("\u59dc\u5c0f\u59b9", "小妹")
            ),
        )
    engine = _object(engine_path)
    admins = engine.get("admins_id")
    admin_id = str(admins[0]) if isinstance(admins, list) and admins else ""
    return RobotSettings(profile=profile, admin_id=admin_id)


def save_robot_settings(
    root: str | Path, settings: RobotSettings, *, apply_reply_identity: bool = True,
) -> RobotSettings:
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
        apply_brand_persona=apply_reply_identity,
        admin_id=normalized_admin,
    )
    return RobotSettings(profile=profile, admin_id=normalized_admin)


def load_provider_services(root: str | Path) -> tuple[ProviderService, ...]:
    install, _, engine_path = _paths(root)
    engine = _object(engine_path)
    thinking_preferences = _provider_thinking_preferences(root)
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
    sources_by_id = {
        str(item.get("id") or ""): item
        for item in source_items
        if isinstance(item, dict) and str(item.get("id") or "").strip()
    }
    services: list[ProviderService] = []
    for provider in provider_items:
        if not isinstance(provider, dict) or provider.get("enable") is False:
            continue
        provider_id = str(provider.get("id") or "").strip()
        source_id = str(provider.get("provider_source_id") or "").strip()
        model = str(provider.get("model") or "").strip()
        source = sources_by_id.get(source_id)
        if not provider_id or not model or not isinstance(source, dict):
            continue
        provider_type = str(
            provider.get("provider_type")
            or source.get("provider_type")
            or "chat_completion"
        ).casefold()
        if provider_type not in {"", "chat", "chat_completion"}:
            continue
        keys = source.get("key")
        api_key = str(keys[0]) if isinstance(keys, list) and keys else ""
        api_base = str(source.get("api_base") or "").strip().rstrip("/")
        extra_body = provider.get("custom_extra_body")
        services.append(
            ProviderService(
                provider_id=provider_id,
                source_id=source_id,
                display_name=provider_display_name(source_id, model),
                api_base=api_base,
                model=model,
                api_key=api_key,
                is_default=provider_id == default_id,
                is_recommended=(
                    source_id == DEFAULT_PROVIDER_SOURCE_ID
                    and model.casefold() == DEFAULT_PROVIDER_MODEL.casefold()
                ),
                thinking_enabled=(
                    thinking_preferences[provider_id]
                    if provider_id in thinking_preferences
                    else infer_thinking_mode(
                        extra_body, api_base=api_base, model=model
                    )
                ),
            )
        )
    services = [
        ProviderService(
            provider_id=item.provider_id,
            source_id=item.source_id,
            display_name=item.display_name,
            api_base=item.api_base,
            model=item.model,
            api_key=item.api_key,
            is_default=item.is_default,
            is_recommended=item.is_recommended,
            reference_modules=provider_reference_modules(
                install, item.provider_id
            ),
            thinking_enabled=item.thinking_enabled,
        )
        for item in services
    ]
    return tuple(
        sorted(
            services,
            key=lambda item: (
                not item.is_default,
                not item.is_recommended,
                item.display_name.casefold(),
            ),
        )
    )


def load_provider_profile(root: str | Path) -> OpenAIProviderProfile | None:
    service = next(
        (item for item in load_provider_services(root) if item.is_default),
        None,
    )
    if service is None or not service.configured:
        return None
    return OpenAIProviderProfile(
        api_key=service.api_key,
        api_base=service.api_base,
        model=service.model,
        source_id=service.source_id,
        thinking_enabled=service.thinking_enabled,
    ).normalized()


def save_provider_profile(
    root: str | Path,
    profile: OpenAIProviderProfile,
    *,
    make_default: bool = True,
) -> OpenAIProviderProfile:
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
        provider_make_default=make_default,
    )
    if normalized.thinking_enabled is not None:
        save_provider_thinking_preference(
            install, normalized.model_id, normalized.thinking_enabled
        )
    return normalized


def set_default_provider(root: str | Path, provider_id: str) -> None:
    _, _, engine_path = _paths(root)
    engine = _object(engine_path)
    providers = engine.get("provider")
    available = {
        str(item.get("id") or "")
        for item in (providers if isinstance(providers, list) else [])
        if isinstance(item, dict) and item.get("enable") is not False
    }
    if provider_id not in available:
        raise ValueError("所选模型服务不存在或尚未启用。")
    settings = engine.get("provider_settings")
    if not isinstance(settings, dict):
        settings = {}
        engine["provider_settings"] = settings
    settings["default_provider_id"] = provider_id
    settings["request_max_retries"] = PROVIDER_REQUEST_MAX_ATTEMPTS
    settings["fallback_chat_models"] = []
    settings["context_limit_reached_strategy"] = "llm_compress"
    settings["llm_compress_provider_id"] = provider_id
    write_json_atomic(engine_path, engine)


def set_provider_thinking(root: str | Path, provider_id: str, enabled: bool) -> bool:
    if not isinstance(enabled, bool):
        raise ValueError("思考模式只能选择开启或关闭。")
    _, _, engine_path = _paths(root)
    engine = _object(engine_path)
    providers = engine.get("provider")
    sources = engine.get("provider_sources")
    model = next(
        (item for item in providers if isinstance(item, dict) and item.get("id") == provider_id),
        None,
    ) if isinstance(providers, list) else None
    source = next(
        (
            item
            for item in sources
            if isinstance(item, dict)
            and item.get("id") == model.get("provider_source_id")
        ),
        None,
    ) if isinstance(model, dict) and isinstance(sources, list) else None
    if not isinstance(model, dict) or not isinstance(source, dict):
        raise ValueError("所选模型服务不存在，请刷新后重试。")
    api_base = str(source.get("api_base") or "")
    model_name = str(model.get("model") or "")
    protocol_applied = supports_thinking_toggle(api_base, model_name)
    if protocol_applied:
        model["custom_extra_body"] = with_thinking_mode(
            model.get("custom_extra_body"), enabled,
            api_base=api_base, model=model_name,
        )
        write_json_atomic(engine_path, engine)
        if not thinking_mode_matches(
            model.get("custom_extra_body"), enabled,
            api_base=api_base, model=model_name,
        ):
            raise ValueError("思考模式已提交，但未能确认保存，请刷新后重试。")
    save_provider_thinking_preference(root, provider_id, enabled)
    updated = next(
        (item for item in load_provider_services(root) if item.provider_id == provider_id),
        None,
    )
    if updated is None or updated.thinking_enabled is not enabled:
        raise ValueError("思考模式已提交，但未能确认保存，请刷新后重试。")
    return protocol_applied


def delete_provider_profile(root: str | Path, provider_id: str) -> None:
    _, _, engine_path = _paths(root)
    engine = _object(engine_path)
    settings = engine.get("provider_settings")
    default_id = (
        str(settings.get("default_provider_id") or "")
        if isinstance(settings, dict)
        else ""
    )
    if provider_id == default_id:
        raise ValueError("正在使用的回复模型不能删除，请先选择另一个模型。")
    providers = engine.get("provider")
    provider_items = providers if isinstance(providers, list) else []
    target = next(
        (
            item
            for item in provider_items
            if isinstance(item, dict) and item.get("id") == provider_id
        ),
        None,
    )
    if not isinstance(target, dict):
        raise ValueError("所选模型服务不存在，请刷新后重试。")
    source_id = str(target.get("provider_source_id") or "")
    remaining = [
        item
        for item in provider_items
        if not isinstance(item, dict) or item.get("id") != provider_id
    ]
    engine["provider"] = remaining
    if source_id and not any(
        isinstance(item, dict) and item.get("provider_source_id") == source_id
        for item in remaining
    ):
        sources = engine.get("provider_sources")
        engine["provider_sources"] = [
            item
            for item in (sources if isinstance(sources, list) else [])
            if not isinstance(item, dict) or item.get("id") != source_id
        ]
    write_json_atomic(engine_path, engine)
    delete_provider_thinking_preference(root, provider_id)


@dataclass(frozen=True, slots=True)
class PluginEntry:
    module_name: str
    display_name: str
    enabled: bool


_PLUGIN_LABELS = {
    "astrbot_plugin_angel_heart": "群聊上下文",
    "astrbot_plugin_bittorrent": "资源检索",
    "astrbot_plugin_livingmemory": "长期记忆",
    "astrbot_plugin_mimo_tts_clone": "语音克隆",
    "astrbot_plugin_music": "点歌",
    "astrbot_plugin_parser": "链接解析",
    "astrbot_plugin_patch_at_wxid": "群聊兼容",
    "astrbot_plugin_proactive_chat": "主动聊天",
    "astrbot_plugin_qq_group_daily_analysis": "群聊分析",
    "astrbot_plugin_self_iterative_core": "自动改代码（实验）",
    "astrbot_plugin_self_learning": "情感与关系",
    "astrbot_plugin_twitter": "推特推送",
    "jiang_capability_router": "功能调度",
    "jiang_commands": "常用指令",
    "jiang_group_notice": "群成员变动通知",
    "jiang_image": "图片生成",
    "jiang_mimo_emoji_voice": "表情语音",
    "jiang_mystool": "米游社签到",
    "jiang_repeater": "复读功能",
    "jiang_rocom": "洛克王国助手",
    "jiang_schedule": "定时任务",
    "jiang_short_drama": "短剧",
    "jiang_video_understanding": "视频理解",
    "meme_manager": "表情管理",
}

_HIDDEN_CUSTOMER_PLUGINS = {
    "astrbot_plugin_self_learning",
    *RETIRED_PLUGIN_MODULES,
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
    install, _, engine_path = _paths(root)
    engine = _object(engine_path)
    plugins = load_plugins(root)
    available = {item.module_name for item in plugins}
    if module_name not in available:
        raise ValueError("所选功能不存在或尚未交付。")
    configured = engine.get("plugin_set", ["*"])
    names = {str(item) for item in configured} if isinstance(configured, list) else {"*"}
    if "*" in names:
        plugin_root = install / "app" / "astrbot" / "data" / "plugins"
        names = {
            path.name
            for path in plugin_root.iterdir()
            if path.is_dir()
            and not path.name.startswith((".", "__"))
            and path.name not in {"data", "logs"}
            and not path.name.endswith("_disabled")
        }
    if enabled:
        names.add(module_name)
    else:
        names.discard(module_name)
    engine["plugin_set"] = sorted(names)
    write_json_atomic(engine_path, engine)


__all__ = [
    "PluginEntry",
    "ProviderService",
    "RobotSettings",
    "delete_provider_profile",
    "delete_provider_thinking_preference",
    "load_plugins",
    "load_provider_profile",
    "load_provider_services",
    "provider_reference_modules",
    "replace_provider_references",
    "replace_provider_value",
    "load_robot_settings",
    "save_provider_profile",
    "save_provider_thinking_preference",
    "save_robot_settings",
    "set_plugin_enabled",
    "set_default_provider",
    "set_provider_thinking",
]
