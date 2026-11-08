"""Injector output stays drained, bounded, private, and cancellation safe."""

import asyncio
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_license_runtime_patches import bridge, patched  # noqa: F401
from test_bridge_task_lifecycle import application


@pytest.fixture
def output_module(bridge):
    return importlib.import_module(bridge.app.__package__ + ".injector_output")


def test_delivered_output_module_imports_without_manager_runtime(output_module, tmp_path):
    repository = Path(__file__).resolve().parents[1]
    source = repository / "src/wechatrebot_manager/customer_logs.py"
    target = "app/bridge/src/hook_bridge/customer_logs.py"
    manifest = json.loads((repository / "release/source-manifest.json").read_text("utf-8"))
    assert {"origin": "commercial", "source": "src/wechatrebot_manager/customer_logs.py",
            "target": target} in manifest["entries"]
    package = tmp_path / "app/bridge/src/hook_bridge"
    package.mkdir(parents=True)
    shutil.copy2(source, package / "customer_logs.py")
    shutil.copy2(output_module.__file__, package / "injector_output.py")
    shutil.copy2(Path(output_module.__file__).with_name("__init__.py"), package / "__init__.py")
    assert (package / "customer_logs.py").read_bytes() == source.read_bytes()
    code = """import importlib,sys
from pathlib import Path
source=sys.argv[1]
sys.path.insert(0,source)
module=importlib.import_module('hook_bridge.injector_output')
assert module.sanitize_log_text('token=private-test-value') == 'token=***'
sanitizer=sys.modules['hook_bridge.customer_logs']
assert Path(sanitizer.__file__).is_relative_to(Path(source))
assert not any(name.startswith('wechatrebot_manager') for name in sys.modules)
print('component injector output import: passed')
"""
    result = subprocess.run([
        sys.executable, "-I", "-c", code, str(package.parent),
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
            "os.write(1, b'\\nstdout stage detail\\ntoken=private-stdout-value\\n'); "
            "os.write(2, b'Authorization: ' + b'a' * (1024 * 1024)); "
            "os.write(2, b'\\nfinal diagnostic\\napi_key=private-test-value\\n')"
        )
        output = output_module.InjectorOutput(process)
        output.start()
        try:
            await wait_exit(process)
            await asyncio.wait_for(output.task, 2)
            assert process.returncode == 0
            assert all(len(tail) <= 4096 for tail in output._tails.values())
            detail = output.detail()
            assert "final diagnostic" in detail and "api_key=***" in detail
            assert "stdout=stdout stage detail\ntoken=***" in detail
            assert "; stderr=final diagnostic" in detail
            assert "private-test-value" not in detail
            assert "private-stdout-value" not in detail
            assert "a" * 100 not in detail  # Cut header value is omitted.
            assert "STDOUT-PRIVATE" not in detail
        finally:
            await collect(process, output)
        assert process.stdout.closed and process.stderr.closed

    asyncio.run(run())
    captured = capsys.readouterr()
    assert "STDOUT-PRIVATE" not in caplog.text + captured.out + captured.err
    assert "private-test-value" not in caplog.text + captured.out + captured.err
    assert "private-stdout-value" not in caplog.text + captured.out + captured.err


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
            assert "diagnostic remains available" in output.detail()
            assert all(len(tail) <= 4096 for tail in output._tails.values())
        finally:
            await asyncio.wait_for(output.close(), 1)
        assert output.task.done()

    asyncio.run(run())


@pytest.mark.parametrize("phase", ["startup", "runtime", "timeout"])
def test_failure_diagnostics_label_available_redacted_streams(bridge, output_module, phase):
    async def run():
        # Open writers model a descendant retaining both inherited pipe handles.
        pipes = [os.pipe(), os.pipe()]
        stdout, stderr = [os.fdopen(read_fd, "rb") for read_fd, _ in pipes]
        process = SimpleNamespace(
            stdout=stdout, stderr=stderr, returncode=7,
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
        os.write(pipes[0][1], b"stdout stage\ntoken=private-stdout-value\n")
        os.write(pipes[1][1], b"Authorization: Bearer private-value\nfinal detail\n")
        operation = app._watch_injector if phase == "runtime" else app._wait_hook_ready
        try:
            with pytest.raises(TimeoutError if phase == "timeout" else RuntimeError) as caught:
                await asyncio.wait_for(operation(), 1)
            message = str(caught.value)
            assert "stdout=stdout stage\ntoken=***" in message
            assert "; stderr=Authorization: ***" in message
            assert "final detail" in message and "Authorization: ***" in message
            assert "private-value" not in message
            assert "private-stdout-value" not in message
            assert ("injector running" if phase == "timeout" else "code=7") in message
        finally:
            await output.close()
            for _, write_fd in pipes:
                os.close(write_fd)

    asyncio.run(run())


@pytest.mark.parametrize("ready", [True, False])
def test_exit_zero_stdout_error_keeps_hook_readiness_authoritative(bridge, output_module, ready):
    async def run():
        process = spawn("print('DLL injection failed; token=private-native-value')")
        output = output_module.InjectorOutput(process)
        output.start()
        app = application(bridge)
        app._injector, app._injector_output = process, output
        app.settings = SimpleNamespace(
            hook_start_timeout_seconds=0.01, hook_api_host="127.0.0.1", hook_api_port=19088,
        )
        app._hook_api_accepting_connections = AsyncMock(return_value=ready)
        try:
            await wait_exit(process)
            assert process.returncode == 0
            if ready:
                await app._wait_hook_ready()
            else:
                with pytest.raises(TimeoutError) as caught:
                    await asyncio.wait_for(app._wait_hook_ready(), 1)
                message = str(caught.value)
                assert "injector exited with code 0" in message
                assert "stdout=DLL injection failed; token=***" in message
                assert "; stderr=" in message and "private-native-value" not in message
        finally:
            await collect(process, output)

    asyncio.run(run())


@pytest.mark.parametrize("name", ["stdout", "stderr"])
def test_truncated_stream_omits_partial_credential_line(output_module, monkeypatch, name):
    pipe = SimpleNamespace(close=lambda: None)
    output = output_module.InjectorOutput(SimpleNamespace(**{name: pipe}))
    output._active[name] = 11
    chunks = iter((b"token=" + b"private-cut-value" * 400 + b"\nsafe stage\n", b""))
    monkeypatch.setattr(output_module.os, "read", lambda *_: next(chunks))
    detail = output.detail()
    assert f"{name}=safe stage" in detail
    assert "private-cut-value" not in detail
    assert len(output._tails[name]) == 4096


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
