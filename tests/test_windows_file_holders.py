"""Exact file evidence survives mappings missing from the ToolHelp module list."""

import ctypes
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from wechatrebot_manager import launcher, uninstaller, updates, windows_file_holders as holders
from wechatrebot_manager.launcher import ProcessIdentity, ServiceStopError
from wechatrebot_manager.update_errors import UpdateFailure


class NativeFunction:
    def __init__(self, callback):
        self.callback = callback

    def __call__(self, *args):
        return self.callback(*args)


@pytest.fixture
def native(monkeypatch):
    calls = []
    state = SimpleNamespace(failure=None, rows=((123, 456),), growth=0, queries=0, reboot=0, invalid_count=False)

    def start(session, flags, key):
        calls.append("start")
        assert flags == 0 and len(key) == 33
        session._obj.value = 17
        return 5 if state.failure == "start" else 0

    def register(session, count, files, apps_count, apps, services_count, services):
        calls.append(("register", tuple(files), count))
        assert session.value == 17 and apps_count == services_count == 0
        assert apps is None and services is None
        return 5 if state.failure == "register" else 0

    def get_list(session, needed, count, buffer, reboot):
        calls.append("query")
        state.queries += 1
        reboot._obj.value = state.reboot
        if state.invalid_count:
            count._obj.value = 1
            return 0
        if state.failure == "query":
            return 5
        required = len(state.rows) + (state.queries if state.growth else 0)
        needed._obj.value = required
        if required and (buffer is None or count._obj.value < required):
            return 234
        count._obj.value = len(state.rows)
        for item, (pid, created) in zip(buffer or (), state.rows):
            item.process.pid = pid
            item.process.started.dwLowDateTime = created & 0xFFFFFFFF
            item.process.started.dwHighDateTime = created >> 32
        return 0

    def end(session):
        calls.append("end")
        return 5 if state.failure == "end" else 0

    api = SimpleNamespace(**{name: NativeFunction(callback) for name, callback in (
        ("RmStartSession", start), ("RmRegisterResources", register),
        ("RmGetList", get_list), ("RmEndSession", end),
    )})
    monkeypatch.setattr(holders, "os", SimpleNamespace(name="nt", path=os.path))
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **_: api, raising=False)
    return state, calls


def test_native_query_registers_only_exact_files_and_preserves_filetime(tmp_path, native):
    state, calls = native
    state.rows = ((123, 134384320288863997), (456, 987))
    files = (tmp_path / "hook.dll", tmp_path / "engine.pyd")

    assert holders._restart_manager_processes(files) == state.rows

    assert calls == ["start", ("register", tuple(map(str, files)), 2), "query", "query", "end"]


@pytest.mark.parametrize("failure", ["start", "register", "query", "end"])
def test_native_errors_fail_closed_and_end_started_sessions(tmp_path, native, failure):
    state, calls = native
    state.failure = failure
    with pytest.raises(OSError):
        holders._restart_manager_processes((tmp_path / "hook.dll",))
    assert ("end" in calls) is (failure != "start")


def test_continuously_growing_list_has_bounded_retries(tmp_path, native):
    state, calls = native
    state.growth = 1
    with pytest.raises(OSError, match="持续变化"):
        holders._restart_manager_processes((tmp_path / "hook.dll",))
    assert state.queries == 5 and calls[-1] == "end"


def test_success_with_count_exceeding_buffer_is_rejected(tmp_path, native):
    state, calls = native
    state.invalid_count = True
    with pytest.raises(OSError, match="不完整列表"):
        holders._restart_manager_processes((tmp_path / "hook.dll",))
    assert calls[-1] == "end"


def test_reboot_reason_keeps_complete_holders_but_cannot_explain_empty_list(tmp_path, native):
    state, calls = native
    state.reboot = 16  # DetectedSelf constrains RM shutdown, not list validity.
    assert holders._restart_manager_processes((tmp_path / "hook.dll",)) == state.rows
    state.rows = ()
    with pytest.raises(OSError, match="是否可释放"):
        holders._restart_manager_processes((tmp_path / "hook.dll",))
    assert calls[-1] == "end"


def test_holder_identity_is_rechecked_and_duplicate_paths_registered_once(tmp_path, monkeypatch):
    file = tmp_path / "hook.dll"
    calls = []
    identity = ProcessIdentity(str(tmp_path / "Weixin.exe"), 456)
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: calls.append(files) or ((123, 456),))
    monkeypatch.setattr(launcher, "process_identity", lambda pid: identity)

    assert holders.file_holders((file, file)) == ((123, identity),)
    assert calls == [(file,)]


@pytest.mark.parametrize("identity", [None, ProcessIdentity("", 456), ProcessIdentity("Weixin.exe", 789)])
def test_missing_inaccessible_or_reused_holder_identity_is_rejected(tmp_path, monkeypatch, identity):
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: ((123, 456),))
    monkeypatch.setattr(launcher, "process_identity", lambda pid: identity)
    with pytest.raises(OSError, match="身份无法确认"):
        holders.file_holders((tmp_path / "hook.dll",))


def test_empty_query_does_not_create_native_session(monkeypatch):
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: pytest.fail("no files"))
    assert holders.file_holders(()) == ()


@pytest.mark.parametrize("kind", ["relative", "directory", "link", "parent-link"])
def test_file_query_rejects_nonexact_or_redirected_paths(tmp_path, monkeypatch, kind):
    if kind == "relative":
        file = Path("relative.dll")
    elif kind == "directory":
        file = tmp_path
    else:
        outside = tmp_path / "outside.dll"
        outside.write_bytes(b"library")
        file = tmp_path / "hook.dll"
        try:
            if kind == "parent-link":
                directory = tmp_path / "real"
                directory.mkdir()
                linked = tmp_path / "linked"
                linked.symlink_to(directory, target_is_directory=True)
                file = linked / "hook.dll"
            else:
                file.symlink_to(outside)
        except OSError:
            pytest.skip("symlinks unavailable")
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: pytest.fail("unsafe file"))
    with pytest.raises(OSError):
        holders.file_holders((file,))


def test_non_windows_query_refuses_unknown_file_usage(tmp_path, monkeypatch):
    monkeypatch.setattr(holders, "os", SimpleNamespace(name="posix", path=os.path))
    with pytest.raises(OSError, match="当前平台"):
        holders.file_holders((tmp_path / "hook.dll",))


@pytest.mark.skipif(os.name != "nt", reason="Windows Restart Manager read-only integration")
def test_native_restart_manager_finds_current_executable_identity():
    assert ctypes.sizeof(holders._UniqueProcess) == 12
    assert ctypes.sizeof(holders._ProcessInfo) == 668
    identity = launcher.process_identity(os.getpid())
    assert identity is not None
    assert (os.getpid(), identity) in holders.file_holders((Path(sys.executable),))


@pytest.fixture
def capture(monkeypatch, tmp_path):
    processes = {123: ProcessIdentity(str(tmp_path / "Weixin.exe"), 456)}
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda **_: tuple(processes))
    monkeypatch.setattr(uninstaller, "_process_module_paths", lambda pid, **_: ())
    monkeypatch.setattr(launcher, "process_identity", processes.get)
    return processes


def test_capture_hidden_hook_uses_exact_file_and_wechat_identity(tmp_path, monkeypatch, capture):
    calls = []
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: calls.append(files) or ((123, 456),))
    assert launcher.capture_update_wechat_process(tmp_path, required=True) == (123, capture[123])
    assert calls == [(tmp_path / "app/bridge/vendor/hook/libGLESv1-c2c-video-v2.dll",)]


@pytest.mark.parametrize("failure", ["query", "reused", "unknown", "multiple", "other-app"])
def test_capture_fallback_cannot_turn_ambiguous_evidence_into_ownership(tmp_path, monkeypatch, capture, failure):
    def query(files):
        if failure == "query":
            raise OSError("query failed")
        if failure == "reused":
            return ((123, 789),)
        if failure == "unknown":
            return ((999, 456),)
        return ((123, 456), (456, 987)) if failure == "multiple" else ((123, 456),)

    if failure == "multiple":
        capture[456] = ProcessIdentity(str(tmp_path / "WeChat.exe"), 987)
    if failure == "other-app":
        capture[123] = ProcessIdentity(str(tmp_path / "personal.exe"), 456)
    monkeypatch.setattr(holders, "_restart_manager_processes", query)
    with pytest.raises(ServiceStopError):
        launcher.capture_update_wechat_process(tmp_path, required=True)


def test_toolhelp_and_restart_manager_merge_identical_holder(tmp_path, monkeypatch, capture):
    hook = tmp_path / "app/bridge/vendor/hook/libGLESv1-c2c-video-v2.dll"
    monkeypatch.setattr(uninstaller, "_process_module_paths", lambda pid, **_: (hook,))
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: ((123, 456),))
    assert launcher.capture_update_wechat_process(tmp_path) == (123, capture[123])


def test_visible_and_hidden_holders_cannot_be_misclassified_as_unique(tmp_path, monkeypatch, capture):
    capture[456] = ProcessIdentity(str(tmp_path / "second/Weixin.exe"), 987)
    hook = tmp_path / "app/bridge/vendor/hook/libGLESv1-c2c-video-v2.dll"
    monkeypatch.setattr(uninstaller, "_process_module_paths", lambda pid, **_: (hook,) if pid == 123 else ())
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: ((456, 987),))
    with pytest.raises(ServiceStopError):
        launcher.capture_update_wechat_process(tmp_path, required=True)


def test_same_pid_with_changed_identity_between_sources_is_rejected(tmp_path, monkeypatch, capture):
    hook = tmp_path / "app/bridge/vendor/hook/libGLESv1-c2c-video-v2.dll"
    monkeypatch.setattr(uninstaller, "_process_module_paths", lambda pid, **_: (hook,))
    monkeypatch.setattr(holders, "file_holders", lambda files: ((123, ProcessIdentity(capture[123].executable, 789)),))
    with pytest.raises(ServiceStopError):
        launcher.capture_update_wechat_process(tmp_path)


def test_toolhelp_cannot_capture_pid_reused_by_another_program(tmp_path, monkeypatch, capture):
    capture[123] = ProcessIdentity(str(tmp_path / "other.exe"), 456)
    monkeypatch.setattr(uninstaller, "_process_module_paths", lambda pid, **_: (
        tmp_path / "app/bridge/vendor/hook/libGLESv1-c2c-video-v2.dll",
    ))
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: ())
    assert launcher.capture_update_wechat_process(tmp_path) is None


def configure_guard(tmp_path, monkeypatch):
    root = tmp_path / "install"
    monkeypatch.setattr(updates, "os", SimpleNamespace(name="nt", path=os.path, getpid=os.getpid))
    monkeypatch.setattr(updates, "payload_requires_runtime_stop", lambda _: True)
    monkeypatch.setattr(updates, "_update_target_paths", lambda _: {
        Path("app/bridge/vendor/hook/hook.dll"), Path("runtime/python/engine.pyd"),
        Path("app/bridge/code.py"), Path("manager/manager.exe"),
    })
    monkeypatch.setattr(uninstaller, "_iter_windows_process_ids", lambda: (123,))
    monkeypatch.setattr(uninstaller, "_process_module_paths", lambda pid: ())
    monkeypatch.setattr(launcher, "process_identity", lambda pid: ProcessIdentity(str(tmp_path / "Weixin.exe"), 456))
    return root


def test_update_guard_includes_hidden_image_holders_for_exact_binaries(tmp_path, monkeypatch):
    root = configure_guard(tmp_path, monkeypatch)
    calls = []
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: calls.append(set(files)) or ((123, 456),))
    with pytest.raises(UpdateFailure) as error:
        updates.assert_update_runtime_stopped(tmp_path, root)
    assert error.value.definition.code == "UPD-1005"
    assert calls == [{root / "app/bridge/vendor/hook/hook.dll", root / "runtime/python/engine.pyd"}]


@pytest.mark.parametrize("failure", ["query", "unknown", "reused"])
def test_update_guard_rejects_unknown_file_usage(tmp_path, monkeypatch, failure):
    root = configure_guard(tmp_path, monkeypatch)
    def query(files):
        if failure == "query":
            raise OSError("unavailable")
        return ((123, 789),) if failure == "reused" else ((999, 456),)
    if failure == "unknown":
        monkeypatch.setattr(launcher, "process_identity", lambda pid: None)
    monkeypatch.setattr(holders, "_restart_manager_processes", query)
    with pytest.raises(updates.UpdateError, match="占用状态"):
        updates.assert_update_runtime_stopped(tmp_path, root)


def test_update_guard_accepts_confirmed_empty_file_usage(tmp_path, monkeypatch):
    root = configure_guard(tmp_path, monkeypatch)
    monkeypatch.setattr(holders, "_restart_manager_processes", lambda files: ())
    updates.assert_update_runtime_stopped(tmp_path, root)
