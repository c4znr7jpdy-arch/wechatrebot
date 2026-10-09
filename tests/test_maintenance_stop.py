from __future__ import annotations

import ctypes
import io
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from wechatrebot_manager import launcher, lifecycle, uninstaller
from wechatrebot_manager.environment import WechatEnvironment
from wechatrebot_manager.runtime_status import RuntimeSnapshot


def _specs(root):
    return tuple(launcher.ServiceSpec(
        name=name, argv=(str(root / "runtime/python/python.exe"),), cwd=root,
        log_file=root / f"{name}.log", ready_host="127.0.0.1", ready_port=port,
        ready_timeout_seconds=1, environment={},
    ) for name, port in (("astrbot", 6200), ("new-hook-bridge", 5000)))


class _Process:
    def __init__(self, *, stubborn=False):
        self.code = None
        self.stubborn = stubborn
        self.signals = []

    def poll(self):
        return self.code

    def wait(self, timeout=None):
        if self.stubborn:
            raise subprocess.TimeoutExpired("synthetic", timeout)
        self.code = 0
        return 0

    def send_signal(self, signal):
        self.signals.append(signal)

    def terminate(self):
        self.signals.append("terminate")

    def kill(self):
        self.signals.append("kill")


def _controller(tmp_path, monkeypatch, *, process=None):
    snapshot = {"value": RuntimeSnapshot(False, False, "")}
    supervisor = launcher.ProcessSupervisor(_specs(tmp_path))
    if process is not None:
        supervisor.running = [launcher.RunningService(supervisor.specs[0], process, io.BytesIO())]
    monkeypatch.setattr(supervisor, "force_stop", lambda: pytest.fail("Maintenance must not force-stop"))
    controller = lifecycle.LifecycleController(
        tmp_path, snapshotter=lambda: snapshot["value"],
        environment_checker=lambda root: WechatEnvironment(
            ready=True, code="10000", title="Ready", message="Ready", solutions=(),
            path=str(tmp_path / "Weixin.exe"), version="4.1.8.27", source="test",
        ),
        supervisor_factory=lambda progress: supervisor,
    )
    controller._supervisor = supervisor if process is not None else None
    monkeypatch.setattr(lifecycle, "installation_services_stopped", lambda root: True)
    return controller, supervisor, snapshot


def test_maintenance_waits_for_wechat_then_uses_normal_verified_stop(tmp_path, monkeypatch):
    process = _Process()
    controller, supervisor, snapshot = _controller(tmp_path, monkeypatch, process=process)
    snapshot["value"] = RuntimeSnapshot(True, True, "wxid_bot", True, True, True)
    controller.state = lifecycle.LifecycleState.RUNNING

    assert controller.stop_for_maintenance() is False
    assert controller.state == lifecycle.LifecycleState.STOP_WAIT_WECHAT
    assert controller.maintenance_stopped() is False
    assert process.signals == []

    snapshot["value"] = RuntimeSnapshot(False, False, "")
    assert controller.refresh().state == lifecycle.LifecycleState.STOP_WAIT_WECHAT
    assert controller.refresh().state == lifecycle.LifecycleState.STOPPED
    assert supervisor.running == []
    assert process.poll() == 0
    assert process.signals
    assert controller.maintenance_stopped() is True


def test_maintenance_retains_failed_stop_and_never_reports_success(tmp_path, monkeypatch):
    process = _Process(stubborn=True)
    controller, supervisor, _ = _controller(tmp_path, monkeypatch, process=process)

    assert controller.stop_for_maintenance() is False
    assert controller.maintenance_stopped() is False
    assert supervisor.running[0].process is process
    assert process.poll() is None
    assert "kill" in process.signals  # Normal stop's existing final retry.


def test_cleared_supervisor_does_not_hide_survivors_across_maintenance_retries(tmp_path, monkeypatch):
    process = _Process()
    controller, supervisor, _ = _controller(tmp_path, monkeypatch, process=process)
    monkeypatch.setattr(supervisor, "stop", lambda: supervisor.running.clear())

    assert controller.stop_for_maintenance() is False
    assert controller._supervisor is None
    assert controller.stop_for_maintenance() is False
    assert controller.maintenance_stopped() is False
    process.code = 0
    assert controller.maintenance_stopped() is True


def test_start_in_progress_cannot_be_misreported_as_stopped(tmp_path, monkeypatch):
    controller, _, _ = _controller(tmp_path, monkeypatch)
    controller.state = lifecycle.LifecycleState.CONNECTION_STARTING
    controller._start_in_progress = True
    monkeypatch.setattr(controller, "request_stop", lambda: pytest.fail("Startup is still in progress"))

    assert controller.stop_for_maintenance() is False
    assert controller.maintenance_stopped() is False
    assert controller.state == lifecycle.LifecycleState.CONNECTION_STARTING


@pytest.mark.parametrize("failure", ["poll", "snapshot", "probe", "missing_poll"])
def test_maintenance_probe_errors_keep_the_gate_closed(tmp_path, monkeypatch, failure):
    controller, _, _ = _controller(tmp_path, monkeypatch)

    def unavailable(*_args):
        raise OSError("synthetic unavailable state")

    if failure == "poll":
        controller._maintenance_processes.append(SimpleNamespace(poll=unavailable))
    elif failure == "snapshot":
        controller._snapshotter = unavailable
    elif failure == "probe":
        monkeypatch.setattr(lifecycle, "installation_services_stopped", unavailable)
    else:
        controller._maintenance_processes.append(object())
    assert controller.maintenance_stopped() is False
    assert controller.stop_for_maintenance() is False


def test_maintenance_checks_real_installation_probe_after_successful_stop(tmp_path, monkeypatch):
    process = _Process()
    controller, _, _ = _controller(tmp_path, monkeypatch, process=process)
    monkeypatch.setattr(lifecycle, "installation_services_stopped", lambda root: False)

    assert controller.stop_for_maintenance() is False
    assert process.poll() == 0
    assert controller.maintenance_stopped() is False
    monkeypatch.setattr(lifecycle, "installation_services_stopped", lambda root: True)
    assert controller.maintenance_stopped() is True


def test_online_runtime_cannot_pass_without_a_supervisor(tmp_path, monkeypatch):
    controller, _, snapshot = _controller(tmp_path, monkeypatch)
    snapshot["value"] = RuntimeSnapshot(True, True, "wxid_bot", True, True, True)

    assert controller.stop_for_maintenance() is False
    assert controller.maintenance_stopped() is False


def test_manager_restart_still_detects_an_unadopted_offline_runtime(tmp_path, monkeypatch):
    controller, _, _ = _controller(tmp_path, monkeypatch)
    specs = _specs(tmp_path)
    pids = [91]
    monkeypatch.setattr(lifecycle, "installation_services_stopped", launcher.installation_services_stopped)
    monkeypatch.setattr(launcher, "build_service_specs", lambda root: specs)
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **kwargs: tuple(pids))
    monkeypatch.setattr(launcher, "process_identity", lambda pid: launcher.ProcessIdentity(specs[0].argv[0], 1001))
    monkeypatch.setattr(launcher, "port_accepting", lambda *args, **kwargs: False)

    assert controller._supervisor is None
    assert controller.maintenance_stopped() is False
    assert controller.stop_for_maintenance() is False
    pids.clear()
    assert controller.maintenance_stopped() is True


@pytest.mark.parametrize("identity_kind,expected", [
    ("own", False), ("own_uppercase", False), ("foreign", True),
    ("missing", False), ("denied", False),
])
def test_installation_probe_requires_confirmed_process_absence(
    tmp_path, monkeypatch, identity_kind, expected,
):
    specs = _specs(tmp_path / "install")
    executable = specs[0].argv[0]
    if identity_kind == "foreign":
        executable = str(tmp_path / "install-neighbor/runtime/python/python.exe")
    elif identity_kind == "own_uppercase":
        executable = executable.upper()
    identities = {91: launcher.ProcessIdentity(executable, 1001)}
    enumerations = []

    def enumerate_ids(**kwargs):
        enumerations.append(kwargs)
        return (91,)

    def identity(pid):
        if identity_kind == "denied":
            raise PermissionError("synthetic protected process")
        return None if identity_kind == "missing" else identities[pid]

    monkeypatch.setattr(launcher, "build_service_specs", lambda root: specs)
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", enumerate_ids)
    monkeypatch.setattr(launcher, "process_identity", identity)
    ports = []
    monkeypatch.setattr(launcher, "port_accepting", lambda host, port, **kwargs: ports.append((host, port, kwargs)) or False)

    assert launcher.installation_services_stopped(tmp_path / "install") is expected
    assert enumerations == [{"executable_names": frozenset({"python.exe"}), "strict": True}]
    if expected:
        assert {(host, port) for host, port, _ in ports} == {("127.0.0.1", 6200), ("127.0.0.1", 5000)}
        assert all(kwargs == {"strict": True} for _, _, kwargs in ports)
    else:
        assert ports == []


@pytest.mark.parametrize("failure", ["enumeration", "port", "metadata"])
def test_installation_probe_never_treats_failed_checks_as_stopped(tmp_path, monkeypatch, failure):
    def unavailable(*_args, **_kwargs):
        raise OSError("synthetic probe failure")

    if failure != "metadata":
        monkeypatch.setattr(launcher, "build_service_specs", lambda root: _specs(root))
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", unavailable if failure == "enumeration" else lambda **kwargs: ())
    monkeypatch.setattr(launcher, "port_accepting", unavailable if failure == "port" else lambda *args, **kwargs: False)

    assert launcher.installation_services_stopped(tmp_path) is False


def test_open_service_port_blocks_maintenance_without_known_processes(tmp_path, monkeypatch):
    monkeypatch.setattr(launcher, "build_service_specs", lambda root: _specs(root))
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **kwargs: ())
    monkeypatch.setattr(launcher, "port_accepting", lambda host, port, **kwargs: port == 6200)
    assert launcher.installation_services_stopped(tmp_path) is False


@pytest.mark.parametrize("error", [TimeoutError("timeout"), PermissionError("denied"), socket.gaierror("dns")])
def test_strict_port_probe_distinguishes_errors_from_refused_connections(monkeypatch, error):
    def unavailable(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(launcher.socket, "create_connection", unavailable)
    assert launcher.port_accepting("127.0.0.1", 6200) is False
    with pytest.raises(OSError):
        launcher.port_accepting("127.0.0.1", 6200, strict=True)
    monkeypatch.setattr(launcher.socket, "create_connection", Mock(side_effect=ConnectionRefusedError()))
    assert launcher.port_accepting("127.0.0.1", 6200, strict=True) is False


def _kernel(monkeypatch, *, entries=(), snapshot_error=False, first_error=None, end_error=18):
    # Mock the OS surface too, so this never calls a real Windows API and the
    # enumeration/error handling tests also work on other development hosts.
    monkeypatch.setattr(uninstaller, "os", SimpleNamespace(name="nt"))
    error = [0]
    monkeypatch.setattr(ctypes, "get_last_error", lambda: error[0], raising=False)
    monkeypatch.setattr(ctypes, "set_last_error", lambda value: error.__setitem__(0, value), raising=False)
    position = iter(entries)

    def advance(_handle, pointer):
        try:
            pid, name = next(position)
        except StopIteration:
            ctypes.set_last_error(end_error)
            return False
        pointer._obj.th32ProcessID = pid
        pointer._obj.szExeFile = name
        return True

    def first(handle, pointer):
        if first_error is not None:
            ctypes.set_last_error(first_error)
            return False
        return advance(handle, pointer)

    kernel = SimpleNamespace(
        CreateToolhelp32Snapshot=Mock(return_value=ctypes.c_void_p(-1).value if snapshot_error else 42),
        Process32FirstW=Mock(side_effect=first),
        Process32NextW=Mock(side_effect=advance),
        CloseHandle=Mock(return_value=True),
    )
    ctypes.set_last_error(5 if snapshot_error else 0)
    monkeypatch.setattr(ctypes, "WinDLL", lambda *args, **kwargs: kernel, raising=False)
    return kernel


def test_windows_process_enumeration_filters_names_and_closes_its_snapshot(monkeypatch):
    kernel = _kernel(monkeypatch, entries=((1, "System"), (91, "PYTHON.EXE"), (92, "other.exe")))
    assert uninstaller._iter_windows_process_ids(executable_names=frozenset({"python.exe"}), strict=True) == (91,)
    kernel.CloseHandle.assert_called_once_with(42)


@pytest.mark.parametrize("failure", ["snapshot", "first", "partial"])
def test_strict_windows_enumeration_rejects_failed_or_partial_snapshots(monkeypatch, failure):
    kernel = _kernel(
        monkeypatch, entries=((91, "python.exe"),), snapshot_error=failure == "snapshot",
        first_error=5 if failure == "first" else None, end_error=5,
    )
    with pytest.raises(OSError):
        uninstaller._iter_windows_process_ids(strict=True)
    if failure != "snapshot":
        kernel.CloseHandle.assert_called_once_with(42)


def test_windows_empty_snapshot_is_valid_only_on_normal_completion(monkeypatch):
    _kernel(monkeypatch)
    assert uninstaller._iter_windows_process_ids(strict=True) == ()
    _kernel(monkeypatch, first_error=5)
    assert uninstaller._iter_windows_process_ids() == ()  # Existing uninstall behavior.


def test_strict_process_enumeration_rejects_unsupported_platforms(monkeypatch):
    monkeypatch.setattr(uninstaller, "os", SimpleNamespace(name="unsupported"))
    with pytest.raises(OSError):
        uninstaller._iter_windows_process_ids(strict=True)
    assert uninstaller._iter_windows_process_ids() == ()
