from __future__ import annotations

import json
import subprocess

from .config import Settings, SettingsError


def build_inject_command(settings: Settings) -> list[str]:
    if settings.wechat_exe_path is None:
        raise SettingsError("wechat_exe_path is not configured")
    payload = {
        "recivemode": "http",
        "tcp_ip": "127.0.0.1",
        "tcp_port": 61108,
        "http_server_port": settings.hook_api_port,
        "http_callback_url": settings.callback_url,
        "usedefault": False,
        "start_server_while_login": True,
    }
    return [
        str(settings.injector_path),
        str(settings.wechat_exe_path),
        str(settings.hook_dll_path),
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
    ]


def start_injector(settings: Settings) -> subprocess.Popen[bytes]:
    errors = settings.preflight(require_wechat=True)
    if errors:
        raise SettingsError("; ".join(errors))
    return subprocess.Popen(
        build_inject_command(settings),
        cwd=settings.injector_path.parent,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
    )
