from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import re
import secrets

from .brand import BrandProfile
from .feature_policy import RETIRED_PLUGIN_MODULES, load_delivery_features
from .json_io import write_json_atomic
from .provider import (
    OpenAIProviderProfile,
    PROVIDER_REQUEST_MAX_ATTEMPTS,
    with_thinking_mode,
)
from .retired_plugins import purge_retired_plugins


_ADMIN_ID = re.compile(r"^[A-Za-z0-9_@.-]{3,128}$")


def normalize_admin_id(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized and not _ADMIN_ID.fullmatch(normalized):
        raise ValueError("管理员 wxid 格式无效，只能使用字母、数字及 _ @ . -。")
    return normalized


def _load_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"现有配置无法读取：{path}：{exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"现有配置根节点必须是 JSON 对象：{path}")
    return payload


@dataclass(frozen=True, slots=True)
class RuntimePorts:
    dashboard: int = 6185
    astrbot_ws: int = 6200
    bridge_callback: int = 5000
    hook_api: int = 19088

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        values = asdict(self)
        for name, value in values.items():
            if not 1 <= value <= 65535:
                errors.append(f"{name} 端口超出范围。")
        if len(set(values.values())) != len(values):
            errors.append("各本地服务端口不能重复。")
        return tuple(errors)


def bridge_config_payload(
    wechat_exe_path: str | Path | None = None,
    *,
    ports: RuntimePorts = RuntimePorts(),
) -> dict[str, object]:
    errors = ports.validate()
    if errors:
        raise ValueError("；".join(errors))
    wechat_path = ""
    if wechat_exe_path:
        selected_path = Path(wechat_exe_path).expanduser()
        if not selected_path.is_absolute():
            raise ValueError("微信主程序路径必须是绝对路径。")
        wechat_path = str(selected_path.resolve())
    return {
        "wechat_exe_path": wechat_path,
        "injector_path": "vendor/hook/x64 inject.exe",
        "hook_dll_path": "vendor/hook/libGLESv1-c2c-video-v2.dll",
        "hook_api_host": "127.0.0.1",
        "hook_api_port": ports.hook_api,
        "callback_host": "127.0.0.1",
        "callback_port": ports.bridge_callback,
        "callback_path": "/api/recvMsg",
        "astrbot_ws_url": f"ws://127.0.0.1:{ports.astrbot_ws}/ws",
        "queue_size": 256,
        "request_timeout_seconds": 10,
        "video_download_timeout_seconds": 180,
        "video_upload_timeout_seconds": 180,
        "video_send_mode": "file",
        "image_path_field": "filepath",
        "callback_max_body_bytes": 16 * 1024 * 1024,
        "media_root": "../../data/media",
        "media_max_bytes": 100 * 1024 * 1024,
        "media_retention_hours": 2,
        "dedup_ttl_seconds": 600,
        "dedup_capacity": 4096,
        "reconnect_min_seconds": 3,
        "reconnect_max_seconds": 3,
        "hook_start_timeout_seconds": 30,
        "allow_existing_hook_reuse": False,
        "log_level": "INFO",
    }


def astrbot_config_payload(
    *,
    ports: RuntimePorts = RuntimePorts(),
    brand_profile: BrandProfile | None = None,
    provider_profile: OpenAIProviderProfile | None = None,
    admin_id: str | None = None,
) -> dict[str, object]:
    errors = ports.validate()
    if errors:
        raise ValueError("；".join(errors))
    normalized_admin = normalize_admin_id(admin_id)
    payload: dict[str, object] = {
        "config_version": 2,
        "platform": [
            {
                "id": "aiocqhttp_hook41827",
                "type": "aiocqhttp",
                "enable": True,
                "ws_reverse_host": "127.0.0.1",
                "ws_reverse_port": ports.astrbot_ws,
                "ws_reverse_token": "",
            }
        ],
        "platform_settings": {
            "enable_id_white_list": False,
            "reply_with_mention": False,
            "reply_with_quote": True,
            "friend_message_needs_wake_prefix": False,
            "ignore_bot_self_message": True,
        },
        "provider_sources": [],
        "provider": [],
        "provider_settings": {
            "default_provider_id": "",
            "fallback_chat_models": [],
            "default_image_caption_provider_id": "",
            "default_personality": "default",
            "context_limit_reached_strategy": "llm_compress",
            "llm_compress_provider_id": "",
            "request_max_retries": PROVIDER_REQUEST_MAX_ATTEMPTS,
            "max_context_length": -1,
            "dequeue_context_length": 1,
        },
        "admins_id": [normalized_admin] if normalized_admin else [],
        "dashboard": {
            "enable": True,
            "username": "local-console",
            "password": "",
            "pbkdf2_password": "",
            "password_storage_upgraded": True,
            "password_change_required": False,
            "jwt_secret": "",
            "host": "127.0.0.1",
            "port": ports.dashboard,
        },
        "wake_prefix": ["/", "\\"],
        "persona": [],
        "plugin_set": ["*"],
        "timezone": "Asia/Shanghai",
        "log_level": "INFO",
        "log_file_enable": True,
        "log_file_path": "../../logs/astrbot-core.log",
        "trace_enable": False,
        "trace_log_enable": False,
        "disable_metrics": True,
    }
    if brand_profile is not None:
        _apply_brand_to_astrbot(payload, brand_profile)
    if provider_profile is not None:
        _apply_provider_to_astrbot(payload, provider_profile)
    return payload


def _ensure_local_dashboard_credentials(dashboard: dict[str, object]) -> None:
    """Provision loopback-only credentials used by the branded control window."""
    dashboard["enable"] = True
    dashboard["host"] = "127.0.0.1"
    dashboard["username"] = "local-console"
    if not str(dashboard.get("jwt_secret") or "").strip():
        dashboard["jwt_secret"] = secrets.token_hex(32)
    if not str(dashboard.get("pbkdf2_password") or "").strip():
        raw_password = secrets.token_urlsafe(24)
        salt = secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac(
            "sha256",
            raw_password.encode("utf-8"),
            bytes.fromhex(salt),
            600_000,
        ).hex()
        dashboard["password"] = hashlib.md5(  # noqa: S324 - legacy compatibility hash
            raw_password.encode("utf-8")
        ).hexdigest()
        dashboard["pbkdf2_password"] = f"pbkdf2_sha256$600000${salt}${digest}"
    dashboard["password_storage_upgraded"] = True
    dashboard["password_change_required"] = False


def _upsert_by_id(
    value: object,
    replacement: dict[str, object],
    *,
    updated_fields: tuple[str, ...] | None = None,
) -> list[object]:
    existing = value if isinstance(value, list) else []
    replacement_id = replacement["id"]
    matched = next(
        (
            item
            for item in existing
            if isinstance(item, dict) and item.get("id") == replacement_id
        ),
        None,
    )
    merged = replacement
    if matched is not None and updated_fields is not None:
        merged = {**replacement, **matched}
        merged.update(
            {
                field: replacement[field]
                for field in updated_fields
                if field in replacement
            }
        )
    return [
        item
        for item in existing
        if not isinstance(item, dict) or item.get("id") != replacement_id
    ] + [merged]


def _apply_brand_to_astrbot(
    payload: dict[str, object],
    profile: BrandProfile,
) -> None:
    normalized = profile.normalized()
    errors = normalized.validate()
    if errors:
        raise ValueError("；".join(errors))
    persona_name = "commercial_brand"
    persona = {
        "name": persona_name,
        "prompt": (
            f"你是{normalized.persona_name}，对外显示名称是{normalized.display_name}。"
            "交流自然、直接、可靠，不使用机械的助手套话；不虚构已经完成的操作。"
            f"用户询问身份时，使用名称“{normalized.display_name}”。"
        ),
        "begin_dialogs": ["你好", normalized.welcome_text],
        "mood_imitation_dialogs": [],
        "tools": None,
        "skills": None,
        "custom_error_message": f"{normalized.short_name}暂时无法处理这条消息，请稍后再试。",
    }
    existing = payload.get("persona")
    existing_personas = existing if isinstance(existing, list) else []
    payload["persona"] = [
        item
        for item in existing_personas
        if not isinstance(item, dict) or item.get("name") != persona_name
    ] + [persona]
    provider_settings = payload.get("provider_settings")
    if not isinstance(provider_settings, dict):
        provider_settings = {}
        payload["provider_settings"] = provider_settings
    provider_settings["default_personality"] = persona_name


def _apply_provider_to_astrbot(
    payload: dict[str, object],
    profile: OpenAIProviderProfile,
    *,
    make_default: bool = True,
) -> None:
    normalized = profile.normalized()
    errors = normalized.validate()
    if errors:
        raise ValueError("；".join(errors))
    payload["provider_sources"] = _upsert_by_id(
        payload.get("provider_sources"),
        normalized.astrbot_source(),
        # Keep vendor-specific extras, but never inherit an old unbounded/very
        # long request timeout: it can make a simple @ reply appear frozen for
        # minutes. The product timeout is part of the unified provider policy.
        updated_fields=("id", "key", "api_base", "enable", "timeout"),
    )
    model_payload = normalized.astrbot_model()
    updated_model_fields = ("id", "provider_source_id", "model", "enable")
    if normalized.thinking_enabled is not None:
        existing_models = payload.get("provider")
        existing_model = next(
            (
                item for item in existing_models
                if isinstance(item, dict) and item.get("id") == normalized.model_id
            ),
            None,
        ) if isinstance(existing_models, list) else None
        model_payload["custom_extra_body"] = with_thinking_mode(
            existing_model.get("custom_extra_body") if isinstance(existing_model, dict) else None,
            normalized.thinking_enabled,
            api_base=normalized.api_base,
            model=normalized.model,
        )
        updated_model_fields += ("custom_extra_body",)
    payload["provider"] = _upsert_by_id(
        payload.get("provider"),
        model_payload,
        updated_fields=updated_model_fields,
    )
    if make_default:
        provider_settings = payload.get("provider_settings")
        if not isinstance(provider_settings, dict):
            provider_settings = {}
            payload["provider_settings"] = provider_settings
        provider_settings["request_max_retries"] = PROVIDER_REQUEST_MAX_ATTEMPTS
        # Hidden fallback chains make a short @ message wait through several
        # providers. Keep services available for manual selection, but execute
        # only the model the user explicitly chose as the reply model.
        provider_settings["fallback_chat_models"] = []
        provider_settings["default_provider_id"] = normalized.model_id
        provider_settings["context_limit_reached_strategy"] = "llm_compress"
        provider_settings["llm_compress_provider_id"] = normalized.model_id


def _sync_delivery_plugins(
    root: Path,
    astrbot_payload: dict[str, object],
) -> None:
    features = load_delivery_features(root)
    if features is not None:
        # Customer features stay loaded globally. Their actual use is granted
        # per chat by group/private policy, so a stale global switch cannot
        # silently override a chat selection. Hidden experimental features
        # still follow their delivery default and remain outside customer UI.
        astrbot_payload["plugin_set"] = [
            feature.module
            for feature in features
            if feature.visible or feature.default_enabled
        ]
        astrbot_payload["wechatrebot_feature_scope_version"] = 2
    configured = astrbot_payload.get("plugin_set")
    if isinstance(configured, list):
        astrbot_payload["plugin_set"] = [
            name for name in configured
            if str(name) not in RETIRED_PLUGIN_MODULES
        ]


def _sync_angel_heart_model(
    astrbot_data_root: Path,
    provider_profile: OpenAIProviderProfile | None,
    robot_name: str = "小妹",
) -> None:
    plugin_dir = astrbot_data_root / "plugins" / "astrbot_plugin_angel_heart"
    if not plugin_dir.is_dir():
        return
    path = astrbot_data_root / "config" / "astrbot_plugin_angel_heart_config.json"
    new_config = not path.is_file()
    payload = _load_object(path)
    wake = payload.get("wake_interaction")
    if not isinstance(wake, dict):
        wake = {}
        payload["wake_interaction"] = wake
    wake.setdefault("enter_on_mention_only", False)
    wake.setdefault("direct_wake_fast_path", True)
    if new_config:
        wake["alias"] = robot_name
        payload["personality"] = {
            "ai_self_identity": (
                f"你是{robot_name}，是群聊里的熟人型机器人。"
                "说话自然、直接、简短，不使用机械的客服措辞。"
            )
        }
    if provider_profile is not None:
        model_id = provider_profile.normalized().model_id
        for field in ("analyzer_model", "image_caption_provider_id"):
            if not str(payload.get(field) or "").strip():
                payload[field] = model_id
    if payload:
        write_json_atomic(path, payload)


def _nested_defaults(target: dict[str, object], defaults: dict[str, object]) -> None:
    for key, value in defaults.items():
        current = target.get(key)
        if isinstance(value, dict):
            if not isinstance(current, dict):
                current = {}
                target[key] = current
            _nested_defaults(current, value)
        else:
            target.setdefault(key, value)


def _sync_living_memory_config(
    astrbot_data_root: Path,
    provider_profile: OpenAIProviderProfile | None,
) -> None:
    """Seed the one primary long-term-memory engine without replacing user choices."""
    plugin_dir = astrbot_data_root / "plugins" / "astrbot_plugin_livingmemory"
    if not plugin_dir.is_dir():
        return
    path = astrbot_data_root / "config" / "astrbot_plugin_livingmemory_config.json"
    payload = _load_object(path)
    _nested_defaults(
        payload,
        {
            "recall_engine": {
                "top_k": 3,
                "auto_recall_mode": "smart",
                "casual_query_max_chars": 24,
                "injection_method": "extra_user_content",
                "inject_with_recent_context": False,
                "vector_search_timeout_seconds": 0.8,
            },
            "filtering_settings": {
                "use_persona_filtering": True,
                "use_session_filtering": True,
            },
            "reflection_engine": {"summary_trigger_rounds": 10},
            # The graph route is valuable for advanced installations but adds a
            # second vector request to every recall. Keep the small-user default fast.
            "graph_memory": {"enabled": False},
            "agent_tools": {
                "enable_recall_tool": True,
                "enable_memorize_tool": False,
            },
        },
    )
    if provider_profile is not None:
        provider_settings = payload.get("provider_settings")
        if not isinstance(provider_settings, dict):
            provider_settings = {}
            payload["provider_settings"] = provider_settings
        provider_settings.setdefault(
            "llm_provider_id", provider_profile.normalized().model_id
        )
    write_json_atomic(path, payload)


def _sync_emotion_config(astrbot_data_root: Path) -> None:
    """Enable the compact emotion layer with privacy-first learning defaults."""
    plugin_dir = astrbot_data_root / "plugins" / "astrbot_plugin_self_learning"
    if not plugin_dir.is_dir():
        return
    path = astrbot_data_root / "config" / "astrbot_plugin_self_learning_config.json"
    payload = _load_object(path)
    _nested_defaults(
        payload,
        {
            "Self_Learning_Basic": {
                "enable_message_capture": False,
                "enable_auto_learning": False,
                "enable_realtime_learning": False,
                "enable_jargon_learning": False,
                "enable_style_learning": False,
                "enable_realtime_llm_filter": False,
                "enable_web_interface": False,
            },
            "Advanced_Settings": {
                "save_raw_messages": False,
                "auto_apply_approved_persona": False,
            },
            "Affection_System_Settings": {"enable_affection_system": True},
            "Mood_System_Settings": {"enable_daily_mood": True},
            "Social_Context_Settings": {
                "enable_social_context_injection": True,
                "include_social_relations": False,
                "include_affection_info": True,
                "include_mood_info": True,
                "context_injection_position": "end",
            },
            "Integration_Settings": {
                "delegate_memory_to_livingmemory": True,
                "disable_local_memory_when_delegated": True,
                "delegate_reply_to_group_chat_plus": True,
            },
            "Runtime_Internal_Settings": {
                "enable_llm_hooks": True,
                "llm_hook_profile": "compact",
            },
            "Database_Settings": {"db_type": "sqlite"},
            "Persona_Evolution_Settings": {
                "auto_apply_persona_updates": False,
                "persona_update_backup_enabled": True,
            },
        },
    )
    basic = payload.get("Self_Learning_Basic")
    if isinstance(basic, dict):
        for field in (
            "enable_message_capture",
            "enable_auto_learning",
            "enable_realtime_learning",
            "enable_jargon_learning",
            "enable_style_learning",
            "enable_realtime_llm_filter",
            "enable_web_interface",
        ):
            basic[field] = False
    advanced = payload.get("Advanced_Settings")
    if isinstance(advanced, dict):
        advanced["save_raw_messages"] = False
        advanced["auto_apply_approved_persona"] = False
    write_json_atomic(path, payload)


def write_runtime_configs(
    install_root: str | Path,
    wechat_exe_path: str | Path | None = None,
    *,
    wechat_version: str | None = None,
    ports: RuntimePorts = RuntimePorts(),
    brand_profile: BrandProfile | None = None,
    apply_brand_persona: bool = True,
    provider_profile: OpenAIProviderProfile | None = None,
    provider_make_default: bool = True,
    admin_id: str | None = None,
) -> None:
    root = Path(install_root).expanduser().resolve()
    bridge_config = root / "app" / "bridge" / "config.json"
    bridge_payload = _load_object(bridge_config)
    bridge_payload.update(bridge_config_payload(wechat_exe_path, ports=ports))
    if not isinstance(bridge_payload.get("manager_query_token"), str) or len(bridge_payload["manager_query_token"]) < 32:
        bridge_payload["manager_query_token"] = secrets.token_urlsafe(32)
    write_json_atomic(
        bridge_config,
        bridge_payload,
    )
    astrbot_root = root / "app" / "astrbot"
    astrbot_data_root = astrbot_root / "data"
    astrbot_data_root.mkdir(parents=True, exist_ok=True)
    (astrbot_data_root / "config").mkdir(parents=True, exist_ok=True)
    purge_retired_plugins(root)
    (astrbot_root / ".astrbot").touch(exist_ok=True)
    astrbot_path = astrbot_data_root / "cmd_config.json"
    generated_astrbot = astrbot_config_payload(
        ports=ports,
        brand_profile=brand_profile,
        provider_profile=provider_profile,
        admin_id=admin_id,
    )
    astrbot_payload = _load_object(astrbot_path)
    if astrbot_payload:
        astrbot_payload["platform"] = generated_astrbot["platform"]
        astrbot_payload["platform_settings"] = {
            **(
                astrbot_payload.get("platform_settings", {})
                if isinstance(astrbot_payload.get("platform_settings"), dict)
                else {}
            ),
            **generated_astrbot["platform_settings"],
        }
        astrbot_payload["dashboard"] = {
            **(
                astrbot_payload.get("dashboard", {})
                if isinstance(astrbot_payload.get("dashboard"), dict)
                else {}
            ),
            "enable": True,
            "host": "127.0.0.1",
            "port": ports.dashboard,
        }
        for key in (
            "log_level",
            "log_file_enable",
            "log_file_path",
            "trace_enable",
            "trace_log_enable",
            "disable_metrics",
        ):
            astrbot_payload[key] = generated_astrbot[key]
        if brand_profile is not None and apply_brand_persona:
            _apply_brand_to_astrbot(astrbot_payload, brand_profile)
        if provider_profile is not None:
            _apply_provider_to_astrbot(
                astrbot_payload,
                provider_profile,
                make_default=provider_make_default,
            )
        normalized_admin = normalize_admin_id(admin_id)
        if normalized_admin is not None:
            astrbot_payload["admins_id"] = (
                [normalized_admin] if normalized_admin else []
            )
    else:
        astrbot_payload = generated_astrbot
    _sync_delivery_plugins(root, astrbot_payload)
    dashboard = astrbot_payload.get("dashboard")
    if not isinstance(dashboard, dict):
        dashboard = {}
        astrbot_payload["dashboard"] = dashboard
    _ensure_local_dashboard_credentials(dashboard)
    dashboard["port"] = ports.dashboard
    write_json_atomic(
        astrbot_path,
        astrbot_payload,
    )
    saved_brand = _load_object(root / "config" / "brand.json")
    robot_name = str(
        (brand_profile.display_name if brand_profile is not None else saved_brand.get("display_name"))
        or "小妹"
    ).strip()
    if robot_name == "\u59dc\u5c0f\u59b9\u673a\u5668\u4eba":
        robot_name = "小妹"
    _sync_angel_heart_model(astrbot_data_root, provider_profile, robot_name)
    _sync_living_memory_config(astrbot_data_root, provider_profile)
    _sync_emotion_config(astrbot_data_root)
    manager_path = root / "config" / "manager.json"
    manager_payload = _load_object(manager_path)
    manager_payload.update(
        {
            "schema_version": 1,
            "install_root": str(root),
            "wechat_exe_path": (
                str(Path(wechat_exe_path).expanduser().resolve())
                if wechat_exe_path
                else ""
            ),
            "wechat_version": wechat_version,
            "ports": asdict(ports),
        }
    )
    if provider_profile is not None:
        manager_payload["provider_configured"] = True
    else:
        manager_payload.setdefault("provider_configured", False)
    normalized_admin = normalize_admin_id(admin_id)
    if normalized_admin is not None:
        manager_payload["admin_configured"] = bool(normalized_admin)
    else:
        manager_payload.setdefault("admin_configured", False)
    write_json_atomic(
        manager_path,
        manager_payload,
    )
