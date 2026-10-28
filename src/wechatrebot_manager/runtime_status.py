from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .launcher import port_accepting
from .runtime_config import RuntimePorts


@dataclass(frozen=True, slots=True)
class BridgeHealth:
    service_ready: bool = False
    status_available: bool = False
    hook_ready: bool = False
    wechat_logged_in: bool = False
    wechat_identity: str = ""
    upstream_connected: bool = False
    heartbeat_fresh: bool = False
    legacy_profile_available: bool = False

    @property
    def connected(self) -> bool:
        return (
            self.service_ready
            and self.hook_ready
            and self.wechat_logged_in
            and bool(self.wechat_identity)
            and self.upstream_connected
            and self.heartbeat_fresh
        )


def probe_bridge_health(
    host: str = "127.0.0.1",
    port: int = 5000,
    *,
    timeout_seconds: float = 2.0,
) -> BridgeHealth:
    # The bridge checks HOOK connectivity before replying. A subsecond budget
    # can expire during a brief busy period even while the account is online.
    # Wait for this single fresh response; never substitute a cached identity.
    request = Request(f"http://{host}:{port}/api/health", method="GET")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read(256 * 1024).decode("utf-8"))
    except (
        OSError,
        HTTPError,
        URLError,
        TimeoutError,
        UnicodeDecodeError,
        json.JSONDecodeError,
    ):
        return BridgeHealth()
    if not isinstance(payload, dict):
        return BridgeHealth()
    return BridgeHealth(
        service_ready=payload.get("service_ready") is True,
        status_available=payload.get("status_available") is True,
        hook_ready=payload.get("hook_ready") is True,
        wechat_logged_in=payload.get("wechat_logged_in") is True,
        wechat_identity=str(payload.get("wechat_identity") or "").strip(),
        upstream_connected=payload.get("upstream_connected") is True,
        heartbeat_fresh=payload.get("heartbeat_fresh") is True,
        legacy_profile_available=(
            "status_available" not in payload and payload.get("service_ready") is True
        ),
    )


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
    hook_ready: bool | None = None
    upstream_connected: bool | None = None
    heartbeat_fresh: bool | None = None
    account_status_available: bool = True

    @property
    def online(self) -> bool:
        base = self.engine_ready and self.connection_ready and bool(self.wechat_identity)
        confirmations = (
            self.hook_ready,
            self.upstream_connected,
            self.heartbeat_fresh,
        )
        if all(value is None for value in confirmations):
            return base
        return base and all(value is True for value in confirmations)

    @property
    def login_required(self) -> bool:
        # An unreachable HOOK cannot confirm that the account logged out.
        # None keeps the legacy three-field snapshot contract intact.
        return (
            self.connection_ready
            and self.account_status_available
            and self.hook_ready is not False
            and not self.wechat_identity
        )

    @property
    def stage(self) -> str:
        if self.online:
            return "online"
        if self.login_required:
            return "login"
        if self.engine_ready or self.connection_ready:
            return "connection"
        return "idle"


def runtime_snapshot(ports: RuntimePorts = RuntimePorts()) -> RuntimeSnapshot:
    engine = port_accepting("127.0.0.1", ports.astrbot_ws)
    connection = port_accepting("127.0.0.1", ports.bridge_callback)
    health = probe_bridge_health(port=ports.bridge_callback) if connection else BridgeHealth()
    identity = health.wechat_identity
    # A current bridge explicitly reports whether its status is authoritative.
    # Do not call HOOK profile APIs before login: HOOK 4.1.8.27 may dereference
    # an uninitialised account object and crash the newly opened WeChat process.
    # Keep the direct profile probe only for legacy bridges that do not expose
    # the status_available capability flag.
    if connection and not identity and health.legacy_profile_available:
        identity = probe_wechat_identity(port=ports.hook_api)
    return RuntimeSnapshot(
        engine,
        connection and health.service_ready,
        identity,
        hook_ready=health.hook_ready,
        upstream_connected=health.upstream_connected,
        heartbeat_fresh=health.heartbeat_fresh,
        account_status_available=(health.status_available and health.hook_ready) or bool(identity),
    )


__all__ = [
    "BridgeHealth",
    "RuntimeSnapshot",
    "probe_bridge_health",
    "probe_wechat_identity",
    "runtime_snapshot",
]
