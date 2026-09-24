from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


class SettingsError(ValueError):
    """Raised when bridge configuration is invalid."""


def _loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _port(value: Any, name: str) -> int:
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise SettingsError(f"{name} must be between 1 and 65535")
    return port


def _positive_number(value: Any, name: str, cast: type[int] | type[float]) -> int | float:
    try:
        number = cast(value)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f"{name} must be a number") from exc
    if number <= 0:
        raise SettingsError(f"{name} must be greater than zero")
    return number


def _bool(value: Any, name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise SettingsError(f"{name} must be a boolean")


@dataclass(frozen=True, slots=True)
class Settings:
    project_root: Path
    wechat_exe_path: Path | None
    injector_path: Path
    hook_dll_path: Path
    hook_api_host: str
    hook_api_port: int
    callback_host: str
    callback_port: int
    callback_path: str
    astrbot_ws_url: str
    queue_size: int
    request_timeout_seconds: float
    video_download_timeout_seconds: float
    video_upload_timeout_seconds: float
    video_send_mode: str
    image_path_field: str
    callback_max_body_bytes: int
    media_root: Path
    media_max_bytes: int
    media_retention_hours: float
    dedup_ttl_seconds: float
    dedup_capacity: int
    reconnect_min_seconds: float
    reconnect_max_seconds: float
    hook_start_timeout_seconds: float
    allow_existing_hook_reuse: bool
    log_level: str

    @classmethod
    def load(cls, path: Path) -> "Settings":
        config_path = path.expanduser().resolve()
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except OSError as exc:
            raise SettingsError(f"cannot read config: {config_path}") from exc
        except json.JSONDecodeError as exc:
            raise SettingsError(f"invalid JSON config: {exc.msg}") from exc
        if not isinstance(raw, dict):
            raise SettingsError("config root must be a JSON object")

        root = config_path.parent

        def resolved(key: str, default: str) -> Path:
            candidate = Path(str(raw.get(key, default))).expanduser()
            return candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()

        configured_wechat = str(raw.get("wechat_exe_path", "")).strip()
        wechat_path = Path(configured_wechat).expanduser() if configured_wechat else None
        if wechat_path is not None and not wechat_path.is_absolute():
            raise SettingsError("wechat_exe_path must be absolute")
        if wechat_path is not None:
            wechat_path = wechat_path.resolve()

        hook_api_host = str(raw.get("hook_api_host", "127.0.0.1")).strip()
        callback_host = str(raw.get("callback_host", "127.0.0.1")).strip()
        if not _loopback(hook_api_host):
            raise SettingsError("hook_api_host must be a loopback address")
        if not _loopback(callback_host):
            raise SettingsError("callback_host must be a loopback address")

        hook_api_port = _port(raw.get("hook_api_port", 19088), "hook_api_port")
        callback_port = _port(raw.get("callback_port", 5000), "callback_port")
        if hook_api_port == callback_port:
            raise SettingsError("hook_api_port and callback_port must be different")

        callback_path = str(raw.get("callback_path", "/api/recvMsg")).strip()
        if not callback_path.startswith("/"):
            raise SettingsError("callback_path must start with /")

        astrbot_ws_url = str(raw.get("astrbot_ws_url", "ws://127.0.0.1:6200/ws")).strip()
        parsed_ws = urlsplit(astrbot_ws_url)
        if parsed_ws.scheme not in {"ws", "wss"} or not parsed_ws.hostname:
            raise SettingsError("astrbot_ws_url must be a ws:// or wss:// URL")

        image_path_field = str(raw.get("image_path_field", "filepath")).strip()
        if image_path_field not in {"image_path", "filepath"}:
            raise SettingsError("image_path_field must be image_path or filepath")

        video_send_mode = str(raw.get("video_send_mode", "file")).strip().lower()
        if video_send_mode not in {"file", "native"}:
            raise SettingsError("video_send_mode must be file or native")

        reconnect_min = float(_positive_number(raw.get("reconnect_min_seconds", 3), "reconnect_min_seconds", float))
        reconnect_max = float(_positive_number(raw.get("reconnect_max_seconds", 3), "reconnect_max_seconds", float))
        if reconnect_max < reconnect_min:
            raise SettingsError("reconnect_max_seconds must be at least reconnect_min_seconds")

        log_level = str(raw.get("log_level", "INFO")).upper()
        if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise SettingsError("log_level is invalid")

        return cls(
            project_root=root,
            wechat_exe_path=wechat_path,
            injector_path=resolved("injector_path", "vendor/hook/x64 inject.exe"),
            hook_dll_path=resolved("hook_dll_path", "vendor/hook/libGLESv1.dll"),
            hook_api_host=hook_api_host,
            hook_api_port=hook_api_port,
            callback_host=callback_host,
            callback_port=callback_port,
            callback_path=callback_path,
            astrbot_ws_url=astrbot_ws_url,
            queue_size=int(_positive_number(raw.get("queue_size", 256), "queue_size", int)),
            request_timeout_seconds=float(_positive_number(raw.get("request_timeout_seconds", 10), "request_timeout_seconds", float)),
            video_download_timeout_seconds=float(
                _positive_number(
                    raw.get("video_download_timeout_seconds", 180),
                    "video_download_timeout_seconds",
                    float,
                )
            ),
            video_upload_timeout_seconds=float(
                _positive_number(
                    raw.get("video_upload_timeout_seconds", 180),
                    "video_upload_timeout_seconds",
                    float,
                )
            ),
            video_send_mode=video_send_mode,
            image_path_field=image_path_field,
            callback_max_body_bytes=int(_positive_number(raw.get("callback_max_body_bytes", 16 * 1024 * 1024), "callback_max_body_bytes", int)),
            media_root=resolved("media_root", "var/media"),
            media_max_bytes=int(_positive_number(raw.get("media_max_bytes", 100 * 1024 * 1024), "media_max_bytes", int)),
            media_retention_hours=float(_positive_number(raw.get("media_retention_hours", 2), "media_retention_hours", float)),
            dedup_ttl_seconds=float(_positive_number(raw.get("dedup_ttl_seconds", 600), "dedup_ttl_seconds", float)),
            dedup_capacity=int(_positive_number(raw.get("dedup_capacity", 4096), "dedup_capacity", int)),
            reconnect_min_seconds=reconnect_min,
            reconnect_max_seconds=reconnect_max,
            hook_start_timeout_seconds=float(_positive_number(raw.get("hook_start_timeout_seconds", 30), "hook_start_timeout_seconds", float)),
            allow_existing_hook_reuse=_bool(
                raw.get("allow_existing_hook_reuse", False),
                "allow_existing_hook_reuse",
            ),
            log_level=log_level,
        )

    @property
    def hook_base_url(self) -> str:
        return f"http://{self.hook_api_host}:{self.hook_api_port}"

    @property
    def callback_url(self) -> str:
        return f"http://{self.callback_host}:{self.callback_port}{self.callback_path}"

    def preflight(self, require_wechat: bool) -> list[str]:
        errors: list[str] = []
        if require_wechat and self.wechat_exe_path is None:
            errors.append("wechat_exe_path is not configured")
        elif require_wechat and self.wechat_exe_path is not None and not self.wechat_exe_path.is_file():
            errors.append(f"wechat_exe_path does not exist: {self.wechat_exe_path}")
        for label, candidate in (
            ("injector_path", self.injector_path),
            ("hook_dll_path", self.hook_dll_path),
        ):
            if not candidate.is_file():
                errors.append(f"{label} does not exist: {candidate}")
        return errors
