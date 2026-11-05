from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import sys
from typing import Any

import pytest

from wechatrebot_manager.launcher import (
    ProcessIdentity,
    ProcessSupervisor,
    ServiceLaunchError,
    ServiceStopError,
    _normalized_executable,
    build_service_specs,
)
from wechatrebot_manager import launcher, uninstaller


class _FakeProcess:
    def __init__(self, name: str, events: list[str], pid: int = 0) -> None:
        self.name = name
        self.events = events
        self.pid = pid
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def wait(self, timeout: float | None = None) -> int:
        self.code = 0
        return 0

    def terminate(self) -> None:
        self.events.append(f"stop:{self.name}")
        self.code = 0

    def kill(self) -> None:
        self.events.append(f"kill:{self.name}")
        self.code = 1

    def send_signal(self, sig: int) -> None:
        self.terminate()


def _layout(root: Path) -> None:
    (root / "runtime" / "python").mkdir(parents=True)
    (root / "runtime" / "python" / "python.exe").write_bytes(b"runtime")
    ffmpeg_bin = root / "runtime" / "ffmpeg" / "bin"
    ffmpeg_bin.mkdir(parents=True)
    (ffmpeg_bin / "ffmpeg.exe").write_bytes(b"ffmpeg")
    (ffmpeg_bin / "ffprobe.exe").write_bytes(b"ffprobe")
    (root / "app" / "astrbot").mkdir(parents=True)
    (root / "app" / "astrbot" / ".astrbot").touch()
    (root / "app" / "astrbot" / "data").mkdir()
    (root / "app" / "astrbot" / "data" / "cmd_config.json").write_text("{}")
    bridge_package = root / "app" / "bridge" / "src" / "hook_bridge"
    bridge_package.mkdir(parents=True)
    (bridge_package / "__init__.py").write_text("")
    (bridge_package / "__main__.py").write_text(
        "print('bridge-bootstrap-ok')\n", encoding="utf-8"
    )
    (root / "app" / "bridge" / "config.json").write_text("{}")
    hook = root / "app" / "bridge" / "vendor" / "hook"
    hook.mkdir(parents=True)
    (hook / "x64 inject.exe").write_bytes(b"injector")
    (hook / "libGLESv1-c2c-video-v2.dll").write_bytes(b"hook")
    legal = root / "legal"
    legal.mkdir()
    (legal / "hook-binaries.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "files": [
                    {
                        "path": "hook/x64 inject.exe",
                        "sha256": hashlib.sha256(b"injector").hexdigest().upper(),
                    },
                    {
                        "path": "hook/libGLESv1-c2c-video-v2.dll",
                        "sha256": hashlib.sha256(b"hook").hexdigest().upper(),
                    },
                ],
            }
        ),
        encoding="utf-8",
    )


def test_specs_share_one_python_and_include_both_services(tmp_path: Path) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    assert [item.name for item in specs] == ["astrbot", "new-hook-bridge"]
    assert specs[0].argv[0] == specs[1].argv[0]
    assert specs[0].ready_port == 6200
    assert specs[1].ready_port == 5000
    assert specs[1].environment["PYTHONPATH"].endswith("app\\bridge\\src")
    assert specs[1].argv[1] == "-c"
    assert specs[1].argv[3].endswith("app\\bridge\\src")
    assert specs[1].argv[4] == "hook_bridge"
    assert specs[0].environment["PATH"].split(os.pathsep)[0].endswith(
        "runtime\\ffmpeg\\bin"
    )
    assert specs[1].environment["PATH"] == specs[0].environment["PATH"]
    assert specs[0].environment["PATH"].split(os.pathsep)[1] == str(
        tmp_path / "runtime/python/Lib/site-packages/playwright/driver"
    )
    assert tmp_path / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe" in specs[
        0
    ].required_files
    assert tmp_path / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe" in specs[
        0
    ].required_files
    assert specs[0].environment["PYTHONDONTWRITEBYTECODE"] == "1"
    assert specs[1].environment["PYTHONDONTWRITEBYTECODE"] == "1"


def test_bridge_bootstrap_imports_source_with_isolated_python(tmp_path: Path) -> None:
    _layout(tmp_path)
    bridge = build_service_specs(tmp_path)[1]

    result = subprocess.run(
        [sys.executable, "-I", *bridge.argv[1:]],
        cwd=bridge.cwd,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "bridge-bootstrap-ok"


def test_supervisor_uses_ordered_readiness_and_reverse_shutdown(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    events: list[str] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        events.append(f"start:{name}")
        return _FakeProcess(name, events)

    def ready(spec, process) -> bool:
        events.append(f"ready:{spec.name}")
        return True

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(specs, popen_factory=popen, readiness_waiter=ready)
    supervisor.start()
    supervisor.stop()

    assert events[:4] == [
        "start:astrbot",
        "start:new-hook-bridge",
        "ready:astrbot",
        "ready:new-hook-bridge",
    ]
    assert events[-2:] == ["stop:new-hook-bridge", "stop:astrbot"]


def test_supervisor_reports_a_service_that_survives_all_stop_retries(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    events: list[str] = []

    class StubbornProcess(_FakeProcess):
        def send_signal(self, _sig: int) -> None:
            events.append("signal:new-hook-bridge")

        def terminate(self) -> None:
            events.append("stop:new-hook-bridge")

        def kill(self) -> None:
            events.append("kill:new-hook-bridge")

        def wait(self, timeout: float | None = None) -> int:
            raise subprocess.TimeoutExpired("bridge", timeout or 1)

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        if "astrbot.cli.__main__" in argv:
            return _FakeProcess("astrbot", events)
        return StubbornProcess("new-hook-bridge", events)

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(
        specs,
        popen_factory=popen,
        readiness_waiter=lambda _spec, _process: True,
    )
    supervisor.start()

    with pytest.raises(ServiceStopError) as error:
        supervisor.stop(timeout_seconds=0)

    assert error.value.service_name == "new-hook-bridge"
    assert [item.spec.name for item in supervisor.running] == ["new-hook-bridge"]
    assert events[:3] == [
        "signal:new-hook-bridge",
        "stop:new-hook-bridge",
        "kill:new-hook-bridge",
    ]


def test_supervisor_force_stop_kills_services_in_reverse_order(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    events: list[str] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        events.append(f"start:{name}")
        return _FakeProcess(name, events)

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(
        specs, popen_factory=popen, readiness_waiter=lambda spec, process: True
    )
    supervisor.start()
    supervisor.force_stop()

    assert events[-2:] == ["kill:new-hook-bridge", "kill:astrbot"]
    assert supervisor.running == []


def test_confirmed_update_closes_all_wechat_but_only_this_robot_runtime(tmp_path, monkeypatch):
    _layout(tmp_path)
    processes = {
        987651: ProcessIdentity(str(tmp_path / "personal/WeChat.exe"), 101),
        987652: ProcessIdentity(str(tmp_path / "other/Weixin.exe"), 102),
        987653: ProcessIdentity(str(tmp_path / "runtime/python/python.exe"), 103),
        987654: ProcessIdentity(str(tmp_path / "other/python.exe"), 104),
    }
    calls = []

    def snapshot(*, executable_names, strict):
        assert executable_names == frozenset({"weixin.exe", "wechat.exe", "python.exe"})
        assert strict is True
        return tuple(processes)

    def terminate(pid, *, expected_creation_time):
        assert expected_creation_time == processes[pid].creation_time
        calls.append(pid)
        del processes[pid]
        return True

    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", snapshot)
    monkeypatch.setattr(uninstaller, "_terminate_windows_process", terminate)
    monkeypatch.setattr(launcher, "process_identity", processes.get)
    monkeypatch.setattr(launcher, "installation_services_stopped", lambda root: True)
    monkeypatch.setattr(launcher.time, "sleep", lambda _: None)
    launcher.force_stop_update_processes(tmp_path)
    assert calls == [987651, 987652, 987653]
    assert list(processes) == [987654]


@pytest.mark.parametrize("query_error", [False, True])
def test_confirmed_update_blocks_on_unknown_live_process(tmp_path, monkeypatch, query_error):
    _layout(tmp_path)
    calls = []

    def identity(_pid):
        if query_error:
            raise OSError("access denied")
        return None

    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: (987651,))
    monkeypatch.setattr(uninstaller, "_terminate_windows_process", lambda *a, **kw: calls.append(a))
    monkeypatch.setattr(launcher, "process_identity", identity)
    with pytest.raises((ServiceStopError, OSError)):
        launcher.force_stop_update_processes(tmp_path)
    assert calls == []


def test_confirmed_update_allows_process_that_exited_during_inspection(tmp_path, monkeypatch):
    _layout(tmp_path)
    snapshots = iter(((987651,), ()))
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: next(snapshots))
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: None)
    monkeypatch.setattr(launcher, "installation_services_stopped", lambda root: True)
    launcher.force_stop_update_processes(tmp_path)


def test_confirmed_update_blocks_on_failed_termination(tmp_path, monkeypatch):
    _layout(tmp_path)
    identity = ProcessIdentity(str(tmp_path / "Weixin.exe"), 123)
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: (987651,))
    monkeypatch.setattr(launcher, "process_identity", lambda _pid: identity)
    monkeypatch.setattr(uninstaller, "_terminate_windows_process", lambda *a, **kw: False)
    with pytest.raises(ServiceStopError) as error:
        launcher.force_stop_update_processes(tmp_path)
    assert error.value.service_name == "wechat"


def test_confirmed_update_requires_final_runtime_stop_verification(tmp_path, monkeypatch):
    _layout(tmp_path)
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: ())
    monkeypatch.setattr(launcher, "installation_services_stopped", lambda root: False)
    with pytest.raises(ServiceStopError) as error:
        launcher.force_stop_update_processes(tmp_path)
    assert error.value.service_name == "new-hook-bridge"


@pytest.mark.parametrize("port_state,expected", [
    ("delayed_refusal", True), ("listening", False), ("timeout", False),
])
def test_maintenance_waits_for_windows_refusal_but_rejects_unknown_ports(
    tmp_path, monkeypatch, port_state, expected,
):
    from contextlib import nullcontext

    _layout(tmp_path)
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: ())

    def connect(address, timeout):
        if port_state == "listening":
            return nullcontext()
        if port_state == "timeout" or timeout < 2:
            raise TimeoutError("Windows has not returned the loopback refusal yet")
        raise ConnectionRefusedError("No listener after Windows completed its probe")

    monkeypatch.setattr(launcher.socket, "create_connection", connect)
    assert launcher.installation_services_stopped(tmp_path) is expected


@pytest.mark.parametrize("identity", [None, "owned"])
def test_maintenance_still_blocks_owned_or_unidentified_process(tmp_path, monkeypatch, identity):
    _layout(tmp_path)
    value = ProcessIdentity(str(tmp_path / "runtime/python/python.exe"), 123) if identity else None
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: (987651,))
    monkeypatch.setattr(launcher, "process_identity", lambda _: value)
    assert launcher.installation_services_stopped(tmp_path) is False


def test_preflight_rejects_missing_hook_before_any_process_starts(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    (tmp_path / "app" / "bridge" / "vendor" / "hook" / "x64 inject.exe").unlink()
    starts: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        return _FakeProcess("unexpected", [])

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(build_service_specs(tmp_path), popen_factory=popen)

    with pytest.raises(ServiceLaunchError, match="x64 inject.exe"):
        supervisor.start()
    assert starts == []


@pytest.mark.parametrize("executable", ["ffmpeg.exe", "ffprobe.exe"])
def test_preflight_rejects_missing_bundled_media_tool_before_start(
    tmp_path: Path, monkeypatch, executable: str,
) -> None:
    _layout(tmp_path)
    (tmp_path / "runtime" / "ffmpeg" / "bin" / executable).unlink()
    starts: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        return _FakeProcess("unexpected", [])

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(build_service_specs(tmp_path), popen_factory=popen)

    with pytest.raises(ServiceLaunchError, match=executable):
        supervisor.start()
    assert starts == []


def test_preflight_allows_occupied_hook_port_for_dedicated_bot_wechat(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    starts: list[list[str]] = []
    specs = build_service_specs(tmp_path)
    assert specs[1].required_free_ports == ()

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        return _FakeProcess(name, [])

    monkeypatch.setattr(
        "wechatrebot_manager.launcher.port_accepting",
        lambda host, port: port == 19088,
    )
    supervisor = ProcessSupervisor(
        specs,
        popen_factory=popen,
        readiness_waiter=lambda spec, process: True,
    )

    supervisor.start()
    supervisor.stop()

    assert len(starts) == 2


def test_preflight_rejects_tampered_hook_before_any_process_starts(
    tmp_path: Path, monkeypatch
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    (tmp_path / "app" / "bridge" / "vendor" / "hook" / "x64 inject.exe").write_bytes(
        b"tampered"
    )
    starts: list[list[str]] = []

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        starts.append(argv)
        return _FakeProcess("unexpected", [])

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(specs, popen_factory=popen)

    with pytest.raises(ServiceLaunchError, match="完整性校验失败"):
        supervisor.start()
    assert starts == []


@pytest.mark.parametrize("stop_method", ["stop", "force_stop"])
def test_supervisor_persists_and_readopts_owned_processes(
    tmp_path: Path, monkeypatch, stop_method
) -> None:
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    state_path = tmp_path / "data" / "manager" / "service-state.json"
    events: list[str] = []
    identities = {
        101: ProcessIdentity(_normalized_executable(specs[0].argv[0]), 1001),
        102: ProcessIdentity(_normalized_executable(specs[1].argv[0]), 1002),
    }
    pids = iter((101, 102))

    def popen(argv: list[str], **kwargs: Any) -> _FakeProcess:
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        return _FakeProcess(name, events, next(pids))

    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *args: False)
    supervisor = ProcessSupervisor(
        specs,
        popen_factory=popen,
        readiness_waiter=lambda spec, process: True,
        state_path=state_path,
        identity_probe=identities.get,
    )
    supervisor.start()
    assert json.loads(state_path.read_text(encoding="utf-8"))["complete"] is True

    for running in supervisor.running:
        running.log_handle.close()
    supervisor.running.clear()
    adopted = ProcessSupervisor(
        specs,
        state_path=state_path,
        identity_probe=identities.get,
        readiness_probe=lambda host, port: True,
    )

    assert adopted.adopt() is True
    assert adopted.adoption_complete is True
    assert [running.process.pid for running in adopted.running] == [101, 102]

    terminated = []

    def terminate(pid, *, expected_creation_time):
        assert expected_creation_time == identities[pid].creation_time
        terminated.append(pid)
        identities.pop(pid)
        return True

    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("unsafe PID-only signal"))
    monkeypatch.setattr(uninstaller, "_terminate_windows_process", terminate)
    getattr(adopted, stop_method)()
    assert not state_path.exists()
    assert terminated == [102, 101]


@pytest.mark.parametrize("method", ["terminate", "kill", "send_signal"])
def test_adopted_process_does_not_kill_reused_pid(monkeypatch, method):
    original = ProcessIdentity("owned/python.exe", 1001)
    current = {987651: original}
    process = launcher.AdoptedProcess(987651, original, current.get)
    assert process.poll() is None
    # Exit and PID reuse happen after the supervisor's poll, before termination.
    current[987651] = ProcessIdentity("other/program.exe", 2002)
    checked = []
    terminated = []

    def guarded_terminate(pid, *, expected_creation_time):
        checked.append((pid, expected_creation_time))
        if current[pid].creation_time != expected_creation_time:
            return False
        terminated.append(pid)
        return True

    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("unsafe PID-only signal"))
    monkeypatch.setattr(uninstaller, "_terminate_windows_process", guarded_terminate)
    getattr(process, method)(*([signal.SIGTERM] if method == "send_signal" else []))
    assert checked == [(987651, 1001)]
    assert terminated == []
    assert process.poll() is not None


def test_adopted_process_reports_failed_termination_of_original_process(monkeypatch):
    identity = ProcessIdentity("owned/python.exe", 1001)
    process = launcher.AdoptedProcess(987651, identity, lambda _: identity)
    monkeypatch.setattr(uninstaller, "_terminate_windows_process", lambda *a, **kw: False)
    with pytest.raises(OSError, match="无法停止"):
        process.kill()
    assert process.poll() is None


def test_adopted_process_rejects_console_signal_without_pid_only_call(monkeypatch):
    identity = ProcessIdentity("owned/python.exe", 1001)
    process = launcher.AdoptedProcess(987651, identity, lambda _: identity)
    monkeypatch.setattr(os, "kill", lambda *args: pytest.fail("unsafe PID-only signal"))
    with pytest.raises(OSError, match="控制台信号"):
        process.send_signal(signal.SIGINT)


def test_partial_start_failure_and_core_stop_keep_owned_bridge(tmp_path, monkeypatch):
    _layout(tmp_path)
    specs = build_service_specs(tmp_path)
    events = []
    fail_core = [True]
    pids = iter((102, 101, 103))
    identities = {pid: ProcessIdentity(_normalized_executable(specs[0].argv[0]), pid * 10)
                  for pid in (101, 102, 103)}
    def popen(argv, **kwargs):
        name = "astrbot" if "astrbot.cli.__main__" in argv else "new-hook-bridge"
        events.append(f"start:{name}")
        return _FakeProcess(name, events, next(pids))
    state_path = tmp_path / "data/service-state.json"
    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *a: False)
    supervisor = ProcessSupervisor(specs, popen_factory=popen,
        readiness_waiter=lambda spec, _: spec.name != "astrbot" or not fail_core[0],
        state_path=state_path, identity_probe=identities.get)
    supervisor.start_missing(("new-hook-bridge",))
    assert json.loads(state_path.read_text())["complete"] is False
    with pytest.raises(ServiceLaunchError):
        supervisor.start_missing(("astrbot",))
    assert [item.spec.name for item in supervisor.running] == ["new-hook-bridge"]
    assert json.loads(state_path.read_text())["services"][0]["pid"] == 102
    fail_core[0] = False
    supervisor.start_missing(("astrbot", "new-hook-bridge"))
    assert json.loads(state_path.read_text())["complete"] is True
    supervisor.stop_names(("astrbot",))
    assert supervisor.running[0].process.poll() is None
    assert events.count("start:new-hook-bridge") == 1
    supervisor.stop()
    assert not state_path.exists()


def test_supervisor_rejects_unlicensed_legacy_entry_before_any_launch(tmp_path, monkeypatch):
    from types import SimpleNamespace
    _layout(tmp_path)
    starts = []
    client = SimpleNamespace(snapshot=lambda *a: {
        "enabled": True, "can_run": False, "can_connect": False, "message": "尚未激活"})
    monkeypatch.setattr("wechatrebot_manager.licensing.client.LicenseClient", lambda _: client)
    monkeypatch.setattr("wechatrebot_manager.runtime_status.probe_bridge_health", lambda **kw:
        SimpleNamespace(status_available=False, hook_ready=False, wechat_identity=""))
    supervisor = ProcessSupervisor(build_service_specs(tmp_path), popen_factory=lambda *a, **kw: starts.append(a))
    with pytest.raises(ServiceLaunchError, match="尚未激活"):
        supervisor.start()
    assert starts == []


def test_force_stop_retains_service_ownership_if_process_survives(tmp_path, monkeypatch):
    _layout(tmp_path)
    class SurvivingProcess(_FakeProcess):
        def kill(self):
            raise PermissionError("still alive")
        def wait(self, timeout=None):
            raise subprocess.TimeoutExpired("bridge", timeout)
    process = SurvivingProcess("new-hook-bridge", [])
    monkeypatch.setattr("wechatrebot_manager.launcher.port_accepting", lambda *a: False)
    supervisor = ProcessSupervisor(build_service_specs(tmp_path),
        popen_factory=lambda *a, **kw: process, readiness_waiter=lambda *a: True)
    supervisor.start_missing(("new-hook-bridge",))
    with pytest.raises(ServiceStopError):
        supervisor.force_stop()
    assert supervisor.running and not supervisor.running[0].log_handle.closed
    process.code = 0
    supervisor.force_stop()
    assert not supervisor.running
