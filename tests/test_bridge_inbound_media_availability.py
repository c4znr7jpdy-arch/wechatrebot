"""Voice/sticker downloads must finish before their native files are consumed."""

import asyncio
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_office_integration import office_bridge  # noqa: F401


def _inbound_app(office_bridge, tmp_path, message_type, *, expected_size=4, timeout=0.1):
    app = object.__new__(office_bridge.app.BridgeApplication)
    app.state = office_bridge.app.BridgeState(600, 256)
    app._active_native_downloads = 0
    app._native_download_generation = 0
    app.settings = SimpleNamespace(
        media_root=tmp_path, media_max_bytes=100, request_timeout_seconds=timeout,
    )
    event = office_bridge.models.NormalizedEvent(
        kind="message", message_type=message_type, message_id="123",
        sender_id="sender", conversation_id="room@chatroom",
        raw={"data": {"totalLen": expected_size, "voiceLength": 60000}},
    )
    return app, event


@pytest.mark.parametrize("message_type", [34, 47])
@pytest.mark.parametrize("initial", [None, b"", b"da"])
def test_native_download_waits_for_late_media_and_hands_it_to_onebot(
    office_bridge, tmp_path, message_type, initial, caplog,
):
    app, event = _inbound_app(office_bridge, tmp_path, message_type)
    writers = []

    async def download(payload):
        destination = Path(payload["path"])
        if initial is not None:
            destination.write_bytes(initial)

        async def finish():
            await asyncio.sleep(0.01)
            destination.write_bytes(b"data")

        writers.append(asyncio.create_task(finish()))

    native_download = AsyncMock(side_effect=download)
    app.hook = SimpleNamespace(download_voice=native_download, download_image=native_download)

    async def run():
        result = await app._prepare_inbound_media(event)
        await asyncio.gather(*writers)
        return result

    with caplog.at_level(logging.WARNING):
        result = asyncio.run(run())
    assert result.media_path is not None
    assert result.media_path.read_bytes() == b"data"
    message = office_bridge.message_codec.encode_inbound(result, "bot", 123)
    segment = message["message"][0]
    assert segment["type"] == ("record" if message_type == 34 else "image")
    assert segment["data"]["file"] == result.media_path.as_uri()
    assert not caplog.records
    native_download.assert_awaited_once()
    assert app._active_native_downloads == 0 and app._native_download_generation == 1


@pytest.mark.parametrize("message_type", [34, 47])
@pytest.mark.parametrize("contents", [None, b"", b"da"])
def test_unfinished_media_is_not_forwarded_and_remains_a_warning(
    office_bridge, tmp_path, message_type, contents, caplog,
):
    app, event = _inbound_app(office_bridge, tmp_path, message_type, timeout=0.03)

    async def download(payload):
        if contents is not None:
            Path(payload["path"]).write_bytes(contents)

    native_download = AsyncMock(side_effect=download)
    app.hook = SimpleNamespace(download_voice=native_download, download_image=native_download)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(app._prepare_inbound_media(event))
    assert result.media_path is None
    assert not list(tmp_path.iterdir())
    expected_actual = None if contents is None else len(contents)
    assert (
        f"inbound media download incomplete message=123 message_type={message_type} "
        f"expected_size=4 actual_size={expected_actual}"
    ) in caplog.text
    native_download.assert_awaited_once()
    assert app._active_native_downloads == 0 and app._native_download_generation == 1


@pytest.mark.parametrize("message_type", [34, 47])
def test_unknown_byte_size_does_not_use_voice_duration(office_bridge, tmp_path, message_type):
    app, event = _inbound_app(office_bridge, tmp_path, message_type, expected_size=0)

    async def download(payload):
        Path(payload["path"]).write_bytes(b"data")

    app.hook = SimpleNamespace(download_voice=download, download_image=download)
    result = asyncio.run(app._prepare_inbound_media(event))
    assert result.media_path.read_bytes() == b"data"
    assert app._active_native_downloads == 0


@pytest.mark.parametrize("message_type", [34, 47])
@pytest.mark.parametrize("advertised", [False, True])
def test_oversized_media_is_rejected_before_handoff(
    office_bridge, tmp_path, message_type, advertised, caplog,
):
    app, event = _inbound_app(
        office_bridge, tmp_path, message_type, expected_size=101 if advertised else 4,
    )

    async def download(payload):
        Path(payload["path"]).write_bytes(b"x" * 101)

    native_download = AsyncMock(side_effect=download)
    app.hook = SimpleNamespace(download_voice=native_download, download_image=native_download)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(app._prepare_inbound_media(event))
    assert result.media_path is None
    assert not list(tmp_path.iterdir())
    assert "inbound media larger than 100 bytes" in caplog.text
    assert native_download.await_count == (0 if advertised else 1)
    assert app._active_native_downloads == 0


@pytest.mark.parametrize("message_type", [34, 47])
@pytest.mark.parametrize("cancel_at", ["request", "disk_wait"])
def test_cancellation_releases_native_download_guard(
    office_bridge, tmp_path, message_type, cancel_at,
):
    app, event = _inbound_app(office_bridge, tmp_path, message_type)
    started = asyncio.Event()

    async def pause(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    async def download(payload):
        if cancel_at == "request":
            await pause()
        Path(payload["path"]).touch()

    native_download = AsyncMock(side_effect=download)
    app.hook = SimpleNamespace(download_voice=native_download, download_image=native_download)
    if cancel_at == "disk_wait":
        app._wait_for_media_download = pause

    async def run():
        task = asyncio.create_task(app._prepare_inbound_media(event))
        await asyncio.wait_for(started.wait(), timeout=1)
        assert app._active_native_downloads == 1
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    native_download.assert_awaited_once()
    assert app._active_native_downloads == 0 and app._native_download_generation == 1
