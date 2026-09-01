from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .json_io import write_json_atomic


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


def write_runtime_configs(
    install_root: str | Path,
    wechat_exe_path: str | Path,
    *,
    wechat_version: str | None = None,
    ports: RuntimePorts = RuntimePorts(),
) -> None:
    root = Path(install_root).expanduser().resolve()
    bridge_config = root / "app" / "bridge" / "config.json"
    write_json_atomic(
        bridge_config,
        bridge_config_payload(wechat_exe_path, ports=ports),
    )
    write_json_atomic(
        root / "config" / "manager.json",
        {
            "schema_version": 1,
            "install_root": str(root),
            "wechat_exe_path": str(Path(wechat_exe_path).expanduser().resolve()),
            "wechat_version": wechat_version,
            "ports": asdict(ports),
        },
    )
