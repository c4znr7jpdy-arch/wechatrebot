"""Injector output stays drained, bounded, private, and cancellation safe."""

import asyncio
import importlib
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from wechatrebot_manager.licensing.bundle import install_runtime_bundle
from test_license_runtime_patches import bridge, patched  # noqa: F401
from test_bridge_task_lifecycle import application


@pytest.fixture
def output_module(bridge):
    return importlib.import_module(bridge.app.__package__ + ".injector_output")


def test_delivered_output_module_imports_from_minimal_runtime_bundle(output_module, tmp_path):
    repository = Path(__file__).resolve().parents[1]
    site_packages = tmp_path / "runtime/python/Lib/site-packages"
    install_runtime_bundle(repository, site_packages)
    code = """import importlib.util,sys
from pathlib import Path
site,source=sys.argv[1:]
sys.path.insert(0,site)
spec=importlib.util.spec_from_file_location('_delivered_injector_output',source)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
assert module.sanitize_log_text('token=private-test-value') == 'token=***'
sanitizer=sys.modules['wechatrebot_manager.customer_logs']
assert Path(sanitizer.__file__).is_relative_to(Path(site))
assert not (Path(site)/'wechatrebot_manager/runtime_config.py').exists()
print('minimal runtime injector output import: passed')
"""
    result = subprocess.run([
        sys.executable, "-I", "-c", code, str(site_packages), output_module.__file__,
    ], capture_output=True, text=True, check=True)
    assert "injector output import: passed" in result.stdout


async def wait_exit(process, timeout=10):
    async def wait():
        while process.poll() is None:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait(), timeout)


def spawn(script):
    return subprocess.Popen(
        [sys.executable, "-u", "-c", script], stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


async def collect(process, output):
    if process.poll() is None:
        process.kill()
    await wait_exit(process)
    await output.close()


def test_real_child_can_fill_both_streams_without_backpressure(output_module, caplog, capsys):
    async def run():
        process = spawn(
            "import os; "
            "os.write(1, b'STDOUT-PRIVATE-' + b'x' * (1024 * 1024)); "
            "os.write(2, b'Authorization: ' + b'a' * (1024 * 1024)); "
            "os.write(2, b'\\nfinal diagnostic\\napi_key=private-test-value\\n')"
        )
        output = output_module.InjectorOutput(process)
        output.start()
        try:
            await wait_exit(process)
            await asyncio.wait_for(output.task, 2)
            assert process.returncode == 0
            assert len(output._stderr) <= 4096
            detail = output.stderr_detail()
            assert "final diagnostic" in detail and "api_key=***" in detail
            assert "private-test-value" not in detail
            assert "a" * 100 not in detail  # Cut header value is omitted.
            assert "STDOUT-PRIVATE" not in detail
        finally:
            await collect(process, output)
        assert process.stdout.closed and process.stderr.closed

    asyncio.run(run())
    captured = capsys.readouterr()
    assert "STDOUT-PRIVATE" not in caplog.text + captured.out + captured.err
    assert "private-test-value" not in caplog.text + captured.out + captured.err


def test_continuous_stdout_does_not_starve_stderr_or_cleanup(output_module, monkeypatch):
    async def run():
        reads = []
        stdout = SimpleNamespace(fileno=lambda: 11, close=lambda: None)
        stderr = SimpleNamespace(fileno=lambda: 12, close=lambda: None)

        def read(descriptor, size):
            reads.append(descriptor)
            return b"x" * size if descriptor == 11 else b"diagnostic remains available\n"

        monkeypatch.setattr(output_module.os, "set_blocking", lambda *_: None)
        monkeypatch.setattr(output_module.os, "read", read)
        output = output_module.InjectorOutput(SimpleNamespace(stdout=stdout, stderr=stderr))
        output.start()
        try:
            await asyncio.sleep(0.01)
            assert 11 in reads and 12 in reads
            assert "diagnostic remains available" in output.stderr_detail()
            assert len(output._stderr) <= 4096
        finally:
            await asyncio.wait_for(output.close(), 1)
        assert output.task.done()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["startup", "runtime", "timeout"])
def test_failure_diagnostics_use_available_redacted_stderr(bridge, output_module, phase):
    async def run():
        # A real pipe with an open writer models a descendant retaining stderr.
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        process = SimpleNamespace(
            stdout=None, stderr=stream, returncode=7,
            poll=lambda: None if phase == "timeout" else 7,
        )
        output = output_module.InjectorOutput(process)
        output.start()
        app = application(bridge)
        app._injector = process
        app._injector_output = output
        app.settings = SimpleNamespace(
            hook_start_timeout_seconds=0 if phase == "timeout" else 1,
            hook_api_host="127.0.0.1", hook_api_port=19088,
        )
        app._hook_api_accepting_connections = AsyncMock(return_value=False)
        os.write(write_fd, b"Authorization: Bearer private-value\nfinal detail\n")
        operation = app._watch_injector if phase == "runtime" else app._wait_hook_ready
        try:
            with pytest.raises(TimeoutError if phase == "timeout" else RuntimeError) as caught:
                await asyncio.wait_for(operation(), 1)
            message = str(caught.value)
            assert "final detail" in message and "Authorization: ***" in message
            assert "private-value" not in message
            assert ("injector running" if phase == "timeout" else "code=7") in message
        finally:
            await output.close()
            os.close(write_fd)

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["startup", "runtime"])
def test_reader_failure_reaches_existing_failure_supervision(bridge, output_module, monkeypatch, phase):
    async def run():
        read_fd, write_fd = os.pipe()
        output = output_module.InjectorOutput(SimpleNamespace(stdout=os.fdopen(read_fd, "rb")))
        output.start()

        def failed_read(*_):
            raise OSError("synthetic read failure")

        monkeypatch.setattr(output_module.os, "read", failed_read)
        app = application(bridge)
        app._injector_output = output
        app._injector = SimpleNamespace(poll=lambda: None)
        app.settings = SimpleNamespace(hook_start_timeout_seconds=1)
        try:
            await asyncio.sleep(0)
            operation = (app._wait_hook_ready if phase == "startup"
                         else app._wait_for_stop_or_runtime_failure)
            with pytest.raises(RuntimeError, match="output pipe could not be read"):
                await asyncio.wait_for(operation(), 1)
        finally:
            await output.close()
            os.close(write_fd)

    asyncio.run(run())


def test_partial_nonblocking_setup_failure_closes_both_handles(output_module, monkeypatch):
    async def run():
        pipes = [os.pipe(), os.pipe()]
        streams = [os.fdopen(read_fd, "rb") for read_fd, _ in pipes]
        output = output_module.InjectorOutput(SimpleNamespace(stdout=streams[0], stderr=streams[1]))
        set_blocking = output_module.os.set_blocking
        calls = []

        def fail_second(descriptor, blocking):
            calls.append(descriptor)
            if len(calls) == 2:
                raise OSError("synthetic unsupported pipe")
            return set_blocking(descriptor, blocking)

        monkeypatch.setattr(output_module.os, "set_blocking", fail_second)
        try:
            with pytest.raises(RuntimeError, match="cannot be made nonblocking"):
                output.start()
            await asyncio.wait_for(output.close(), 1)
            await output.close()  # Repeated cleanup remains harmless.
            assert all(stream.closed for stream in streams)
            assert output.task is None
        finally:
            await output.close()
            for _, write_fd in pipes:
                os.close(write_fd)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["error", "cancel"])
def test_shutdown_finally_collects_reader_with_writer_still_open(bridge, output_module, failure):
    async def run():
        read_fd, write_fd = os.pipe()
        stream = os.fdopen(read_fd, "rb")
        output = output_module.InjectorOutput(SimpleNamespace(stdout=stream))
        output.start()
        app = application(bridge)
        app._injector_output = output
        entered = asyncio.Event()

        async def shutdown_components():
            entered.set()
            if failure == "error":
                raise OSError("synthetic component shutdown failure")
            await asyncio.Event().wait()

        app._shutdown_components = shutdown_components
        task = asyncio.create_task(app._shutdown())
        try:
            await entered.wait()
            if failure == "cancel":
                task.cancel()
            with pytest.raises(OSError if failure == "error" else asyncio.CancelledError):
                await asyncio.wait_for(task, 1)
            assert stream.closed and output.task.done()
            assert app._injector_output is None
        finally:
            os.close(write_fd)
            await output.close()

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["cancel", "setup-failure"])
def test_run_failure_reaps_child_and_collects_output(bridge, output_module, monkeypatch, outcome):
    async def run():
        process = spawn("import time; print('synthetic ready', flush=True); time.sleep(30)")
        app = application(bridge)
        app.settings = SimpleNamespace()
        app.launch_injector = True
        app.injector_factory = lambda _: process
        app.group_member_poll_interval_seconds = 0
        app._group_member_watch_task = None
        app._owns_session = False
        app._session = None
        app._injector = None
        app._select_dedicated_hook_api_port = AsyncMock()
        app._ensure_components = AsyncMock()
        app._start_callback_server = AsyncMock()
        app._stop_callback_server = AsyncMock()
        app._hook_api_accepting_connections = AsyncMock(return_value=False)
        app._consume_events = lambda: asyncio.Event().wait()
        app._periodic_media_cleanup = lambda: asyncio.Event().wait()
        closed, waiting = asyncio.Event(), asyncio.Event()
        app.onebot = SimpleNamespace(run=closed.wait, close=AsyncMock(side_effect=closed.set))
        app.router = SimpleNamespace(close=AsyncMock())

        async def wait_hook():
            assert isinstance(app._injector_output, output_module.InjectorOutput)
            assert app._injector_output.task is not None
            waiting.set()
            await asyncio.Event().wait()

        app._wait_hook_ready = wait_hook
        monkeypatch.setattr(bridge.app, "configure_logging", lambda _: None)
        monkeypatch.setattr(bridge.app, "runtime_guard", lambda: SimpleNamespace(
            start_refresh=lambda: None, stop_refresh=lambda: None,
        ))
        outputs = []
        start = output_module.InjectorOutput.start

        def record_start(output):
            outputs.append(output)
            start(output)

        monkeypatch.setattr(output_module.InjectorOutput, "start", record_start)
        if outcome == "setup-failure":
            def fail_setup(*_):
                raise OSError("synthetic pipe setup failure")
            monkeypatch.setattr(output_module.os, "set_blocking", fail_setup)
        before = asyncio.all_tasks()
        task = asyncio.create_task(app.run())
        output = None
        try:
            if outcome == "cancel":
                await asyncio.wait_for(waiting.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 5)
            else:
                assert await asyncio.wait_for(task, 5) == 1
                assert not waiting.is_set()
            output, = outputs
            assert process.poll() is not None
            assert process.stdout.closed and process.stderr.closed
            assert output.task is None or output.task.done()
            assert app._injector_output is None
            assert not [item for item in asyncio.all_tasks() - before if not item.done()]
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            if output is not None:
                await collect(process, output)
            elif process.poll() is None:
                process.kill()
                await wait_exit(process)
                process.stdout.close()
                process.stderr.close()

    asyncio.run(run())
