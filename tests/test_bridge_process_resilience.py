"""Media subprocess and malformed frame regressions on the delivery patch stack."""

import asyncio
import importlib
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401


@pytest.fixture
def media(bridge):
    return importlib.import_module(bridge.app.__package__ + ".media")


class MediaProcess:
    def __init__(self, *, blocked=False, stuck_output=False, exited_during_kill=False):
        self.returncode = None if blocked else 0
        self.entered = asyncio.Event()
        self.released = asyncio.Event()
        self.blocked = blocked
        self.stuck_output = stuck_output
        self.exited_during_kill = exited_during_kill
        self.kills = 0
        self.communications = 0

    async def communicate(self):
        self.communications += 1
        self.entered.set()
        if self.blocked and (self.returncode is None or self.stuck_output):
            await self.released.wait()
        return b'{"format":{"duration":"1.2"}}', b""

    def kill(self):
        self.kills += 1
        self.returncode = -9
        if self.exited_during_kill:
            raise ProcessLookupError()
        if not self.stuck_output:
            self.released.set()


def mock_process(monkeypatch, media, process):
    monkeypatch.setattr(media.shutil, "which", lambda executable: executable)
    spawn = AsyncMock(return_value=process)
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", spawn)
    return spawn


@pytest.mark.parametrize("executable", ["ffmpeg", "ffprobe"])
def test_media_tool_success_keeps_output_and_arguments(media, monkeypatch, executable):
    process = MediaProcess()
    spawn = mock_process(monkeypatch, media, process)

    result = asyncio.run(media.MediaStore._run_media_tool(executable, "-i", "video.mp4"))

    assert result == '{"format":{"duration":"1.2"}}'
    assert process.kills == 0 and process.communications == 1
    assert spawn.await_args.args == (executable, "-i", "video.mp4")


@pytest.mark.parametrize("executable", ["ffmpeg", "ffprobe"])
@pytest.mark.parametrize("exited_during_kill", [False, True])
def test_stuck_media_tool_is_killed_and_reaped(media, monkeypatch, executable, exited_during_kill):
    process = MediaProcess(blocked=True, exited_during_kill=exited_during_kill)
    mock_process(monkeypatch, media, process)
    monkeypatch.setattr(media, "_MEDIA_TOOL_TIMEOUT_SECONDS", 0.01)

    with pytest.raises(media.UnsupportedMediaSource, match="timed out"):
        asyncio.run(media.MediaStore._run_media_tool(executable))

    assert process.kills == 1 and process.communications == 2
    assert process.returncode is not None


def test_cancelling_media_tool_reaps_child_and_preserves_cancellation(media, monkeypatch):
    process = MediaProcess(blocked=True)
    mock_process(monkeypatch, media, process)

    async def cancel():
        task = asyncio.create_task(media.MediaStore._run_media_tool("ffmpeg"))
        await asyncio.wait_for(process.entered.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=1)
        assert task.cancelled()

    asyncio.run(cancel())
    assert process.kills == 1 and process.communications == 2
    assert process.returncode is not None


def test_stuck_output_cleanup_has_its_own_deadline(media, monkeypatch, caplog):
    process = MediaProcess(blocked=True, stuck_output=True)
    mock_process(monkeypatch, media, process)
    monkeypatch.setattr(media, "_MEDIA_TOOL_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(media, "_MEDIA_TOOL_CLEANUP_TIMEOUT_SECONDS", 0.01)

    async def run():
        with pytest.raises(media.UnsupportedMediaSource, match="timed out"):
            await asyncio.wait_for(media.MediaStore._run_media_tool("ffmpeg"), timeout=1)

    asyncio.run(run())
    assert process.kills == 1 and process.communications == 2
    assert "output did not close" in caplog.text


def test_media_tool_start_failure_is_a_media_error(media, monkeypatch):
    monkeypatch.setattr(media.shutil, "which", lambda executable: executable)
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", AsyncMock(side_effect=OSError("start failed")))

    with pytest.raises(media.UnsupportedMediaSource, match="failed to start"):
        asyncio.run(media.MediaStore._run_media_tool("ffprobe"))


def test_nonzero_media_tool_exit_keeps_error_detail(media, monkeypatch):
    process = MediaProcess()
    process.returncode = 7
    process.communicate = AsyncMock(return_value=(b"", b"invalid input"))
    mock_process(monkeypatch, media, process)

    with pytest.raises(media.UnsupportedMediaSource, match="code 7: invalid input"):
        asyncio.run(media.MediaStore._run_media_tool("ffmpeg"))
    assert process.kills == 0


@pytest.mark.parametrize("cancel", [False, True])
def test_real_subprocess_is_reaped_after_timeout_or_cancellation(media, monkeypatch, cancel):
    """Exercise Windows pipe/process cleanup using a harmless Python child."""
    create_process = asyncio.create_subprocess_exec
    monkeypatch.setattr(media.shutil, "which", lambda executable: sys.executable)
    monkeypatch.setattr(media, "_MEDIA_TOOL_TIMEOUT_SECONDS", 5 if cancel else 0.2)
    monkeypatch.setattr(media, "_MEDIA_TOOL_CLEANUP_TIMEOUT_SECONDS", 1)

    async def run():
        spawned = asyncio.Event()
        processes = []

        async def capture(*args, **kwargs):
            process = await create_process(*args, **kwargs)
            processes.append(process)
            spawned.set()
            return process

        monkeypatch.setattr(media.asyncio, "create_subprocess_exec", capture)
        task = asyncio.create_task(media.MediaStore._run_media_tool(
            "ffmpeg", "-u", "-c", "import time; print('ready', flush=True); time.sleep(30)"
        ))
        try:
            await asyncio.wait_for(spawned.wait(), timeout=3)
            if cancel:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, timeout=3)
                assert task.cancelled()
            else:
                with pytest.raises(media.UnsupportedMediaSource, match="timed out"):
                    await asyncio.wait_for(task, timeout=3)
            assert len(processes) == 1
            assert processes[0].returncode is not None
            await asyncio.wait_for(processes[0].wait(), timeout=1)
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            for process in processes:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.wait(), timeout=1)

    asyncio.run(run())


def test_invalid_frames_do_not_interrupt_following_actions(bridge, caplog):
    frames = [b"\xff", "{bad json", "[]", b'{"action":"get_status","echo":"binary"}',
              '{"action":"get_status","echo":"text"}']

    class Connection:
        async def __aiter__(self):
            for frame in frames:
                yield frame

    router = SimpleNamespace(handle=AsyncMock(return_value={"status": "ok"}))
    client = bridge.onebot.OneBotClient("ws://127.0.0.1:6200/ws", router)
    client._send_json = AsyncMock()

    asyncio.run(client._receive_loop(Connection()))

    assert [call.args[0]["echo"] for call in router.handle.await_args_list] == ["binary", "text"]
    assert client._send_json.await_count == 2
    assert caplog.text.count("ignored invalid JSON from AstrBot") == 2


def test_invalid_binary_frame_does_not_terminate_websocket_client(bridge, monkeypatch):
    class Connection:
        async def __aiter__(self):
            yield b"\xff"
            yield '{"action":"get_status"}'

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    router = SimpleNamespace()
    client = bridge.onebot.OneBotClient("ws://127.0.0.1:6200/ws", router)

    async def handle(request):
        client._closed = True
        return {"status": "ok"}

    router.handle = AsyncMock(side_effect=handle)
    client._send_json = AsyncMock()
    monkeypatch.setattr(bridge.onebot.websockets, "connect", lambda *args, **kwargs: Connection())

    asyncio.run(client.run())
    router.handle.assert_awaited_once_with({"action": "get_status"})
    client._send_json.assert_awaited_once_with({"status": "ok"})
