from wechatrebot_manager.runtime_status import BridgeHealth, RuntimeSnapshot


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
