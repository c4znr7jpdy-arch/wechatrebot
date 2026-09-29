from wechatrebot_manager import runtime_status
from wechatrebot_manager.runtime_status import BridgeHealth, RuntimeSnapshot
import io
import json

import pytest


def test_real_runtime_requires_full_connection_and_heartbeat() -> None:
    snapshot = RuntimeSnapshot(
        True,
        True,
        "wxid_bot",
        hook_ready=True,
        upstream_connected=True,
        heartbeat_fresh=False,
    )
    assert snapshot.online is False

    ready = RuntimeSnapshot(
        True,
        True,
        "wxid_bot",
        hook_ready=True,
        upstream_connected=True,
        heartbeat_fresh=True,
    )
    assert ready.online is True


def test_legacy_runtime_snapshot_remains_compatible() -> None:
    assert RuntimeSnapshot(True, True, "wxid_bot").online is True
    assert RuntimeSnapshot(True, True, "").stage == "login"


def test_bridge_health_connected_requires_every_signal() -> None:
    assert BridgeHealth(
        service_ready=True,
        hook_ready=True,
        wechat_logged_in=True,
        wechat_identity="wxid_bot",
        upstream_connected=True,
        heartbeat_fresh=True,
    ).connected


def test_authoritative_prelogin_health_never_calls_unsafe_profile_api(monkeypatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(runtime_status, "port_accepting", lambda host, port: True)
    monkeypatch.setattr(
        runtime_status,
        "probe_bridge_health",
        lambda **kwargs: BridgeHealth(service_ready=True, status_available=True),
    )
    monkeypatch.setattr(
        runtime_status,
        "probe_wechat_identity",
        lambda **kwargs: calls.append(int(kwargs["port"])) or "unsafe",
    )

    snapshot = runtime_status.runtime_snapshot()

    assert snapshot.wechat_identity == ""
    assert calls == []


def test_legacy_bridge_can_still_use_profile_fallback(monkeypatch) -> None:
    monkeypatch.setattr(runtime_status, "port_accepting", lambda host, port: True)
    monkeypatch.setattr(
        runtime_status,
        "probe_bridge_health",
        lambda **kwargs: BridgeHealth(service_ready=True, legacy_profile_available=True),
    )
    monkeypatch.setattr(
        runtime_status,
        "probe_wechat_identity",
        lambda **kwargs: "wxid_legacy",
    )

    assert runtime_status.runtime_snapshot().wechat_identity == "wxid_legacy"


@pytest.mark.parametrize("payload", [None, {"service_ready": True, "status_available": False}])
def test_unknown_health_does_not_probe_hook_or_confirm_logout(monkeypatch, payload):
    def response(*args, **kwargs):
        if payload is None:
            raise TimeoutError()
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(runtime_status, "urlopen", response)
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *args: True)
    monkeypatch.setattr(runtime_status, "probe_wechat_identity", lambda **kw: pytest.fail("unsafe HOOK probe"))
    snapshot = runtime_status.runtime_snapshot()
    assert snapshot.account_status_available is False
    assert snapshot.wechat_identity == ""
    assert snapshot.online is False


def test_confirmed_logout_is_authoritative(monkeypatch):
    monkeypatch.setattr(runtime_status, "port_accepting", lambda *args: True)
    monkeypatch.setattr(runtime_status, "probe_bridge_health", lambda **kw: BridgeHealth(
        service_ready=True, status_available=True,
    ))
    snapshot = runtime_status.runtime_snapshot()
    assert snapshot.account_status_available is True
    assert snapshot.wechat_identity == ""
