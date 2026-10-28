"""Native download overlap regressions using the complete delivery patch stack."""
import asyncio
import copy
import logging
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientConnectionError
import pytest

from test_group_self_join_notice import BOT, ROOM, bridge, members  # noqa: F401


def setup_health(app):
    app._hook_ready = True
    app.onebot.connected = True
    app.onebot.heartbeat_fresh = True
    app._hook_api_accepting_connections = AsyncMock(return_value=False)


@pytest.mark.parametrize("ready", [False, True])
def test_snapshot_preserves_last_state_without_probing_during_download(bridge, ready):
    app, _ = bridge
    setup_health(app)
    app._hook_ready = ready
    app._active_native_downloads = 2
    snapshot = asyncio.run(app._health_snapshot())
    assert snapshot["hook_ready"] is ready
    assert snapshot["wechat_logged_in"] is ready
    assert snapshot["heartbeat_fresh"] is ready
    app._hook_api_accepting_connections.assert_not_awaited()


@pytest.mark.parametrize("download_finished", [False, True])
def test_snapshot_ignores_overlapping_probe_but_next_failure_marks_offline(
    bridge, caplog, download_finished,
):
    app, _ = bridge
    setup_health(app)

    async def overlapping_probe():
        app._native_download_generation += 1
        app._active_native_downloads = 0 if download_finished else 1
        return False

    app._hook_api_accepting_connections.side_effect = overlapping_probe
    with caplog.at_level(logging.INFO):
        snapshot = asyncio.run(app._health_snapshot())
        assert snapshot["hook_ready"] and snapshot["wechat_identity"] == BOT
        assert "no longer accepting" not in caplog.text
        app._active_native_downloads = 0
        app._hook_api_accepting_connections.side_effect = None
        snapshot = asyncio.run(app._health_snapshot())
    assert not snapshot["hook_ready"] and not app._hook_ready
    assert "no longer accepting" in caplog.text


def test_poll_pause_keeps_snapshots_and_resumes_real_member_changes(bridge, caplog):
    app, _ = bridge
    app.hook.get_group_members.return_value = members(BOT, "existing")
    asyncio.run(app._poll_group_member_changes_once())
    baseline = copy.deepcopy(app._polled_group_members)
    state = copy.deepcopy(app.state.group_members)
    app.hook.get_groups.reset_mock()
    app.hook.get_group_members.reset_mock()
    app._active_native_downloads = 1
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    app.hook.get_groups.assert_not_awaited()
    app.hook.get_group_members.assert_not_awaited()
    assert app._polled_group_members == baseline and app.state.group_members == state
    assert not caplog.records
    app._active_native_downloads = 0
    app.hook.get_group_members.return_value = members(BOT, "existing", "new")
    assert asyncio.run(app._poll_group_member_changes_once()) == 1
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    assert app.onebot.send_event.await_count == 1


@pytest.mark.parametrize("stage", ["get_groups", "get_group_members"])
def test_successful_response_overlapping_download_does_not_replace_snapshots(bridge, stage):
    app, _ = bridge
    app.hook.get_group_members.return_value = members(BOT, "existing")
    asyncio.run(app._poll_group_member_changes_once())
    baseline = copy.deepcopy(app._polled_group_members)
    state = copy.deepcopy(app.state.group_members)
    app.hook.get_group_members.reset_mock()

    async def response(*args):
        app._native_download_generation += 1  # Download already finished on return.
        if stage == "get_groups":
            return [{"wxid": ROOM}]
        return members(BOT, "unreliable")

    getattr(app.hook, stage).side_effect = response
    assert asyncio.run(app._poll_group_member_changes_once()) == 0
    assert app._polled_group_members == baseline and app.state.group_members == state
    app.onebot.send_event.assert_not_awaited()
    if stage == "get_groups":
        app.hook.get_group_members.assert_not_awaited()


@pytest.mark.parametrize("stage", ["get_groups", "get_group_members"])
@pytest.mark.parametrize("download_finished", [False, True])
@pytest.mark.parametrize("error_type", [ClientConnectionError, asyncio.TimeoutError])
def test_poll_overlap_is_debug_but_next_connection_failure_warns(
    bridge, monkeypatch, caplog, stage, download_finished, error_type,
):
    app, _ = bridge
    app.group_member_poll_interval_seconds = 15
    app.hook.get_group_members.return_value = members(BOT, "existing")
    asyncio.run(app._poll_group_member_changes_once())
    baseline = copy.deepcopy(app._polled_group_members)
    state = copy.deepcopy(app.state.group_members)
    requests = 0

    async def request(*args):
        nonlocal requests
        requests += 1
        if requests == 1:
            app._native_download_generation += 1
            app._active_native_downloads = 0 if download_finished else 1
            raise error_type("during-download")
        raise error_type("after-download")

    getattr(app.hook, stage).side_effect = request
    sleeps = 0

    async def sleep(_):
        nonlocal sleeps
        sleeps += 1
        app._active_native_downloads = 0
        if sleeps == 3:
            raise asyncio.CancelledError

    monkeypatch.setattr(sys.modules[app.__module__].asyncio, "sleep", sleep)
    with caplog.at_level(logging.DEBUG), pytest.raises(asyncio.CancelledError):
        asyncio.run(app._watch_group_member_changes())
    warnings = [record for record in caplog.records if record.levelno >= logging.WARNING]
    assert len(warnings) == 1
    if stage == "get_groups":
        assert "after-download" in warnings[0].message
    else:
        assert "after-download" in str(warnings[0].exc_info[1])
    assert any("during-download" in record.message and record.levelno == logging.DEBUG
               for record in caplog.records)
    assert app._polled_group_members == baseline and app.state.group_members == state


def test_one_room_timeout_does_not_block_later_rooms(bridge, caplog):
    app, _ = bridge
    app.hook.get_group_members.return_value = members(BOT, "existing")
    asyncio.run(app._poll_group_member_changes_once())
    app.hook.get_groups.return_value = [{"wxid": "slow@chatroom"}, {"wxid": ROOM}]

    async def request(room):
        if room == "slow@chatroom":
            raise asyncio.TimeoutError("slow-room")
        return members(BOT, "existing", "new")

    app.hook.get_group_members.side_effect = request
    assert asyncio.run(app._poll_group_member_changes_once()) == 1
    assert "new" in app._polled_group_members[ROOM]
    assert app.onebot.send_event.await_count == 1
    assert any("slow@chatroom" in record.message and record.levelno == logging.ERROR
               for record in caplog.records)


def test_unrelated_member_api_error_is_not_hidden_by_video(bridge, caplog):
    app, _ = bridge

    async def bad_response(_):
        app._native_download_generation += 1
        app._active_native_downloads = 1
        raise ValueError("invalid-member-response")

    app.hook.get_group_members.side_effect = bad_response
    asyncio.run(app._poll_group_member_changes_once())
    assert any(record.levelno == logging.ERROR and "failed to poll" in record.message
               for record in caplog.records)


@pytest.mark.parametrize("download_finished", [False, True])
def test_health_watcher_defers_overlap_then_detects_sustained_failure(
    bridge, monkeypatch, download_finished,
):
    app, _ = bridge
    setup_health(app)
    app.hook_health_interval_seconds = 1
    app.hook_health_failure_limit = 2
    app.settings = SimpleNamespace(hook_api_host="127.0.0.1", hook_api_port=19088)
    probes = 0

    async def probe():
        nonlocal probes
        probes += 1
        if probes == 1:
            app._native_download_generation += 1
            app._active_native_downloads = 0 if download_finished else 1
        return False

    async def sleep(_):
        app._active_native_downloads = 0

    monkeypatch.setattr(sys.modules[app.__module__].asyncio, "sleep", sleep)
    app._hook_api_accepting_connections.side_effect = probe
    with pytest.raises(RuntimeError, match="2 consecutive checks"):
        asyncio.run(app._watch_hook_api())
    assert probes == 3 and not app._hook_ready


@pytest.mark.parametrize("cancelled", [False, True])
def test_actual_video_download_tracks_overlap_until_wait_finishes(bridge, tmp_path, cancelled):
    app, models = bridge
    app.settings = SimpleNamespace(media_root=tmp_path, media_max_bytes=100,
                                   video_download_timeout_seconds=180)
    observed = []

    async def download(payload):
        observed.append((app._active_native_downloads, app._native_download_generation))
        Path(payload["path"]).write_bytes(b"video")
        return {"status": "success"}

    async def wait(*args, **kwargs):
        observed.append((app._active_native_downloads, app._native_download_generation))
        if cancelled:
            raise asyncio.CancelledError
        return True, 5

    app.hook.download_video = download
    app._wait_for_media_download = wait
    event = models.NormalizedEvent(
        kind="message", message_type=43, message_id="123", sender_id="member",
        conversation_id=ROOM, raw={"total_len": 5, "newMsgId": 123},
    )
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(app._prepare_inbound_media(event))
    else:
        result = asyncio.run(app._prepare_inbound_media(event))
        assert result.media_path.read_bytes() == b"video"
    assert observed == [(1, 1), (1, 1)]
    assert app._active_native_downloads == 0 and app._native_download_generation == 1
