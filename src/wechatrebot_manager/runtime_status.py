from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .launcher import port_accepting
from .runtime_config import RuntimePorts


def _find_identity(value: Any) -> str:
    if isinstance(value, dict):
        for key in ("wxid", "account_id", "userName", "user_name"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        for nested in value.values():
            candidate = _find_identity(nested)
            if candidate:
                return candidate
    elif isinstance(value, list):
        for nested in value:
            candidate = _find_identity(nested)
            if candidate:
                return candidate
    return ""


def probe_wechat_identity(
    host: str = "127.0.0.1",
    port: int = 19088,
    *,
    timeout_seconds: float = 0.35,
) -> str:
    request = Request(
        f"http://{host}:{port}/api/get_profile_cache",
        data=b"{}",
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            value = json.loads(response.read(256 * 1024).decode("utf-8"))
    except (OSError, HTTPError, URLError, TimeoutError, UnicodeDecodeError, json.JSONDecodeError):
        return ""
    return _find_identity(value)


@dataclass(frozen=True, slots=True)
class RuntimeSnapshot:
    engine_ready: bool
    connection_ready: bool
    wechat_identity: str

    @property
    def online(self) -> bool:
        return self.engine_ready and self.connection_ready and bool(self.wechat_identity)

    @property
    def stage(self) -> str:
        if self.online:
            return "online"
        if self.connection_ready:
            return "login"
        if self.engine_ready:
            return "connection"
        return "idle"


def runtime_snapshot(ports: RuntimePorts = RuntimePorts()) -> RuntimeSnapshot:
    engine = port_accepting("127.0.0.1", ports.astrbot_ws)
    connection = port_accepting("127.0.0.1", ports.bridge_callback)
    identity = probe_wechat_identity(port=ports.hook_api) if connection else ""
    return RuntimeSnapshot(engine, connection, identity)


__all__ = ["RuntimeSnapshot", "probe_wechat_identity", "runtime_snapshot"]
