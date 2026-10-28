"""Native WeChat file-download regressions without starting the robot."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import ClientConnectionError
import pytest

from test_office_integration import office_bridge


def _native_app(office_bridge, tmp_path, *, max_bytes=100, timeout=0.2):
    app = object.__new__(office_bridge.app.BridgeApplication)
    app._active_native_downloads = 0
    app._native_download_generation = 0
    app.settings = SimpleNamespace(
        media_root=tmp_path,
        media_max_bytes=max_bytes,
        request_timeout_seconds=0.02,
        file_download_timeout_seconds=timeout,
    )
    return app


def test_file_disconnect_waits_for_existing_download_and_calls_native_once(
    office_bridge, tmp_path
):
    app = _native_app(office_bridge, tmp_path)
    destination = tmp_path / "inbound-file.xlsx"
    calls = 0

    async def download(_payload):
        nonlocal calls
        calls += 1
        destination.write_bytes(b"da")

        async def finish_write():
            await asyncio.sleep(0.01)
            destination.write_bytes(b"data")

        asyncio.create_task(finish_write())
        raise ClientConnectionError("listener closed after accepting download")

    response, complete, actual_size = asyncio.run(
        app._download_native_media(
            download,
            {"path": str(destination)},
            destination,
            4,
            "message-1",
            timeout_seconds=0.2,
        )
    )

    assert calls == 1
    assert response["status"] == "download_in_progress_after_disconnect"
    assert complete and actual_size == 4
    assert destination.read_bytes() == b"data"
    assert app._active_native_downloads == 0
    assert app._native_download_generation == 1


def test_native_file_timeout_releases_overlap_guard(office_bridge, tmp_path):
    app = _native_app(office_bridge, tmp_path, timeout=0.01)

    async def download(_payload):
        await asyncio.sleep(10)

    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(
            app._download_native_media(
                download,
                {},
                Path("missing-file"),
                4,
                "message-timeout",
                timeout_seconds=0.01,
            )
        )
    assert app._active_native_downloads == 0
    assert app._native_download_generation == 1


def test_native_file_cancellation_releases_overlap_guard(office_bridge, tmp_path):
    app = _native_app(office_bridge, tmp_path, timeout=1)
    started = asyncio.Event()

    async def download(_payload):
        started.set()
        await asyncio.sleep(10)

    async def run():
        task = asyncio.create_task(
            app._download_native_media(
                download,
                {},
                Path("missing-file"),
                4,
                "message-cancel",
                timeout_seconds=1,
            )
        )
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert app._active_native_downloads == 0
    assert app._native_download_generation == 1


def test_real_disconnect_without_destination_is_still_a_failure(office_bridge, tmp_path):
    app = _native_app(office_bridge, tmp_path, timeout=0.05)

    async def download(_payload):
        raise ClientConnectionError("listener refused connection")

    with pytest.raises(ClientConnectionError):
        asyncio.run(
            app._download_native_media(
                download,
                {},
                Path("missing-file"),
                4,
                "message-failed",
                timeout_seconds=0.05,
            )
        )
    assert app._active_native_downloads == 0


def test_file_download_client_does_not_retry_native_request(office_bridge):
    hook_client = __import__("_office_bridge_test.hook_client", fromlist=["HookClient"])
    client = hook_client.HookClient.__new__(hook_client.HookClient)
    client.file_download_timeout = object()
    seen = {}

    async def post(path, payload, **kwargs):
        seen.update(path=path, payload=payload, **kwargs)
        return {"status": "accepted"}

    client._post = post
    assert asyncio.run(client.download_file({"path": "x"})) == {"status": "accepted"}
    assert seen["path"] == "/api/download_file"
    assert seen["retry_read"] is False
    assert seen["timeout"] is client.file_download_timeout


def test_attachment_over_media_limit_is_rejected_before_native_download(
    office_bridge, tmp_path
):
    app = _native_app(office_bridge, tmp_path, max_bytes=3)
    app.hook = SimpleNamespace(download_file=AsyncMock())
    event = office_bridge.models.NormalizedEvent(
        kind="message",
        message_type=49,
        message_id="message-large",
        sender_id="sender",
        conversation_id="room@chatroom",
        raw={"data": {"content": (
            "<msg><appmsg><title>large.pdf</title><type>6</type><appattach>"
            "<totallen>4</totallen><attachid>opaque</attachid><fileext>pdf</fileext>"
            "</appattach></appmsg></msg>"
        )}},
    )
    result = asyncio.run(app._prepare_inbound_media(event))
    assert result.media_path is None
    app.hook.download_file.assert_not_awaited()


def test_discard_media_swallows_locked_file_cleanup_error(
    office_bridge, tmp_path, monkeypatch, caplog
):
    target = tmp_path / "locked.pdf"
    target.write_bytes(b"data")

    def fail_unlink(_self, **_kwargs):
        raise PermissionError("file is locked")

    monkeypatch.setattr(Path, "unlink", fail_unlink)
    office_bridge.app.BridgeApplication._discard_media(target, "message-locked")
    assert "could not remove incomplete inbound media" in caplog.text
