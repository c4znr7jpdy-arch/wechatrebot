from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path

from .json_io import write_json_atomic


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
    wechat_exe_path: str | Path,
    *,
    ports: RuntimePorts = RuntimePorts(),
) -> dict[str, object]:
    errors = ports.validate()
    if errors:
        raise ValueError("；".join(errors))
    wechat_path = Path(wechat_exe_path).expanduser()
    if not wechat_path.is_absolute():
        raise ValueError("微信主程序路径必须是绝对路径。")
    return {
        "wechat_exe_path": str(wechat_path.resolve()),
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
) -> dict[str, object]:
    errors = ports.validate()
    if errors:
        raise ValueError("；".join(errors))
    return {
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
        },
        "admins_id": [],
        "dashboard": {
            "enable": True,
            "username": "astrbot",
            "password": "",
            "pbkdf2_password": "",
            "password_storage_upgraded": False,
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


def write_runtime_configs(
    install_root: str | Path,
    wechat_exe_path: str | Path,
    *,
    wechat_version: str | None = None,
    ports: RuntimePorts = RuntimePorts(),
) -> None:
    root = Path(install_root).expanduser().resolve()
    bridge_config = root / "app" / "bridge" / "config.json"
    bridge_payload = _load_object(bridge_config)
    bridge_payload.update(bridge_config_payload(wechat_exe_path, ports=ports))
    write_json_atomic(
        bridge_config,
        bridge_payload,
    )
    astrbot_root = root / "app" / "astrbot"
    (astrbot_root / "data").mkdir(parents=True, exist_ok=True)
    (astrbot_root / ".astrbot").touch(exist_ok=True)
    astrbot_path = astrbot_root / "data" / "cmd_config.json"
    generated_astrbot = astrbot_config_payload(ports=ports)
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
    else:
        astrbot_payload = generated_astrbot
    write_json_atomic(
        astrbot_path,
        astrbot_payload,
    )
    manager_path = root / "config" / "manager.json"
    manager_payload = _load_object(manager_path)
    manager_payload.update(
        {
            "schema_version": 1,
            "install_root": str(root),
            "wechat_exe_path": str(Path(wechat_exe_path).expanduser().resolve()),
            "wechat_version": wechat_version,
            "ports": asdict(ports),
        }
    )
    write_json_atomic(
        manager_path,
        manager_payload,
    )
