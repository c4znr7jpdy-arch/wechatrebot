"""Exercise updater rollback using isolated processes, never the real manager."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from test_updates import _ready_incremental_update
from wechatrebot_manager import updates, windows_process_job
from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.update_errors import UpdateFailure
from wechatrebot_manager.windows_process_job import OwnedProcess, ProcessTreeStopError, start_owned_process


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows process jobs and file locks")


def _wait_until(predicate):
    deadline = time.monotonic() + 5
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("isolated process did not become ready")
        time.sleep(0.02)


def _plan(tmp_path):
    root, prepared = _ready_incremental_update(tmp_path)
    manager = prepared.payload_root / "manager/WeChatReBot.exe"
    manager.parent.mkdir()
    manager.write_bytes(b"new-manager")
    manifest = prepared.payload_root / "release-manifest.json"
    value = json.loads(manifest.read_text(encoding="utf-8"))
    value["files"].append({"path": "manager/WeChatReBot.exe", "sha256": sha256_file(manager)})
    manifest.write_text(json.dumps(value), encoding="utf-8")
    return root, updates.create_update_plan(
        prepared, root, executable=root / "manager/WeChatReBot.exe", parent_pid=0,
    )


def _start_tree(target, marker, *, root_exits):
    child = "import os,sys,time; from pathlib import Path; handle=open(sys.argv[1],'rb'); Path(sys.argv[2]).write_text(str(os.getpid())); time.sleep(30)"
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',sys.argv[1],sys.argv[2],sys.argv[3]],creationflags=subprocess.CREATE_NO_WINDOW); "
    parent += "time.sleep(.2)" if root_exits else "time.sleep(30)"
    process = start_owned_process(
        [sys.executable, "-c", parent, child, str(target), str(marker)],
        cwd=target.parent, env=os.environ.copy(),
    )
    try:
        _wait_until(marker.is_file)
        if root_exits:
            _wait_until(lambda: process.poll() is not None)
        return process
    except Exception:
        process.terminate_tree()
        process.close()
        raise


@pytest.mark.parametrize("root_exits", [False, True])
def test_startup_timeout_stops_whole_tree_before_rollback_and_preserves_unrelated_process(
    tmp_path, monkeypatch, root_exits,
):
    root, plan = _plan(tmp_path)
    target = root / "manager/WeChatReBot.exe"
    before = target.read_bytes()
    processes, launches = [], []
    unrelated = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                 creationflags=subprocess.CREATE_NO_WINDOW)
    def restart(target, _root, **kwargs):
        launches.append(kwargs)
        if not kwargs.get("update_id"):
            return None  # Never launch the real manager after the test rollback.
        process = _start_tree(target, tmp_path / "child-ready", root_exits=root_exits)
        processes.append(process)
        assert process.active_processes() >= 1
        # Simulate a stale/reused PID field: stopping must follow the retained
        # kernel job, never resolve this PID and terminate the unrelated process.
        process.pid = unrelated.pid
        with pytest.raises(PermissionError):
            target.rename(target.with_suffix(".replacement"))
        return process
    def timeout(*_args):
        raise UpdateFailure("UPD-1009", "isolated startup timeout")
    monkeypatch.setattr(updates, "_restart_manager", restart)
    monkeypatch.setattr(updates, "_wait_for_startup_confirmation", timeout)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    try:
        assert updates.apply_update_plan(plan.plan_path) == 1
        assert target.read_bytes() == before
        assert (root / "app/astrbot/data/plugins/example/main.py").read_bytes() == b"old-plugin"
        assert processes[0].active_processes() == 0
        assert unrelated.poll() is None
        assert len(launches) == 2
        result = updates.consume_update_result(root)
        assert result["error_code"] == "UPD-1009"
        assert not (root / "updates/update-journal.json").exists()
    finally:
        for process in processes:
            process.terminate_tree()
            process.close()
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_unverified_stop_retains_new_files_and_backups_without_restarting_old_manager(tmp_path, monkeypatch):
    root, plan = _plan(tmp_path)
    launches, restores = [], []
    def failed_stop(_process):
        raise ProcessTreeStopError("job status unavailable")
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    monkeypatch.setattr(updates, "_restart_manager", lambda *args, **kwargs: launches.append(args) or object())
    monkeypatch.setattr(updates, "_wait_for_startup_confirmation", lambda *_:
                        (_ for _ in ()).throw(UpdateFailure("UPD-1009", "timeout")))
    monkeypatch.setattr(updates, "_stop_restarted_manager", failed_stop)
    monkeypatch.setattr(updates, "_restore_update_targets", lambda *_: restores.append(True))

    assert updates.apply_update_plan(plan.plan_path) == 1

    assert not restores and len(launches) == 1
    assert (root / "manager/WeChatReBot.exe").read_bytes() == b"new-manager"
    journal = json.loads((root / "updates/update-journal.json").read_text(encoding="utf-8"))
    assert (root / journal["rollback_root"]).is_dir()
    result = updates.consume_update_result(root)
    assert result["error_code"] == "UPD-1010" and "保留" in result["message"]


@pytest.mark.parametrize("failure", ["terminate", "query", "timeout"])
def test_job_stop_failure_is_explicit(failure, monkeypatch):
    api = SimpleNamespace(TerminateJobObject=lambda *_: failure != "terminate")
    process = OwnedProcess(0, 0, 123, api)
    if failure == "query":
        api.QueryInformationJobObject = lambda *_: False
    else:
        monkeypatch.setattr(process, "active_processes", lambda: 1)
    with pytest.raises(ProcessTreeStopError):
        process.terminate_tree(timeout_seconds=0)


def test_releasing_successful_restart_does_not_stop_client(tmp_path):
    from wechatrebot_manager.launcher import process_identity
    from wechatrebot_manager.uninstaller import _terminate_windows_process
    target = tmp_path / "客户端测试文件.bin"
    target.write_bytes(b"new")
    marker = tmp_path / "child.pid"
    process = _start_tree(target, marker, root_exits=False)
    identities = [(pid, process_identity(pid)) for pid in (int(marker.read_text()), process.pid)]
    try:
        assert all(identity is not None for _pid, identity in identities)
        process.close()
        assert all(process_identity(pid) == identity for pid, identity in identities)
        with pytest.raises(PermissionError):
            target.rename(target.with_suffix(".replacement"))
    finally:
        for pid, identity in identities:
            if identity is not None:
                assert _terminate_windows_process(pid, expected_creation_time=identity.creation_time)
        process.close()


@pytest.mark.parametrize("failure", ["assign", "resume"])
def test_job_setup_failure_ends_suspended_process_before_application_runs(tmp_path, monkeypatch, failure):
    import _winapi
    api = windows_process_job._kernel()
    proxy = SimpleNamespace(
        CreateJobObjectW=api.CreateJobObjectW,
        AssignProcessToJobObject=(lambda *_: False) if failure == "assign" else api.AssignProcessToJobObject,
        ResumeThread=(lambda *_: 0xFFFFFFFF) if failure == "resume" else api.ResumeThread,
    )
    monkeypatch.setattr(windows_process_job, "_kernel", lambda: proxy)
    create, tracked = _winapi.CreateProcess, []
    def track(*args):
        result = create(*args)
        current = _winapi.GetCurrentProcess()
        tracked.append(_winapi.DuplicateHandle(current, result[0], current, 0, False, _winapi.DUPLICATE_SAME_ACCESS))
        return result
    monkeypatch.setattr(_winapi, "CreateProcess", track)
    marker = tmp_path / "must-not-run"
    try:
        with pytest.raises(OSError):
            start_owned_process(
                [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).touch()", str(marker)],
                cwd=tmp_path, env=os.environ.copy(),
            )
        assert len(tracked) == 1
        assert _winapi.WaitForSingleObject(tracked[0], 0) == _winapi.WAIT_OBJECT_0
        assert not marker.exists()
    finally:
        for handle in tracked:
            if _winapi.WaitForSingleObject(handle, 0) != _winapi.WAIT_OBJECT_0:
                _winapi.TerminateProcess(handle, 1)
                _winapi.WaitForSingleObject(handle, 5000)
            _winapi.CloseHandle(handle)
