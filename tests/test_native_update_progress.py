import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from wechatrebot_manager import native_update_progress as native, update_api, update_handoff
from wechatrebot_manager.update_errors import UpdateFailure
from test_automatic_update import full_plan
from test_unchanged_update_targets import make_payload, write
from wechatrebot_manager import payload, update_rollback
from wechatrebot_manager import updates
from test_updates import _ready_incremental_update


def test_native_progress_throttles_disk_writes_and_keeps_exact_final_counts(tmp_path, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(native.time, "monotonic", lambda: clock[0])
    writes = []
    monkeypatch.setattr(native, "write_text_atomic", lambda path, text: writes.append(text))
    progress = native.NativeProgress(tmp_path / "progress.txt", "a" * 32)
    for count in range(40_001):
        progress.report("backup", count, 40_000)
    assert len(writes) == 2
    assert writes[-1].splitlines()[2] == "100"
    assert "40000 / 40000" in writes[-1]
    clock[0] += .3
    progress.report("awaiting_startup", 0, 0)
    assert writes[-1].splitlines()[2] == "-1"


@pytest.fixture
def display(tmp_path, monkeypatch):
    executable = tmp_path / "中文 窗口.exe"
    executable.write_bytes(b"MZ")
    monkeypatch.setattr(native, "native_progress_executable", lambda: executable)
    events = []
    process = SimpleNamespace(
        poll=lambda: None, terminate=lambda: events.append("terminated"),
        wait=lambda **kwargs: events.append("waited"),
    )
    monkeypatch.setattr(native.subprocess, "Popen", lambda argv, **kwargs: process)
    monkeypatch.setattr(native, "_wait_for_visible_window", lambda *_: events.append("visible"))
    writes = []
    monkeypatch.setattr(native, "write_text_atomic", lambda path, text: writes.append(text))
    return events, writes


def test_native_operation_starts_only_after_window_ready_and_reports_success(display):
    events, writes = display
    def operation(progress):
        assert events == ["visible"]
        progress("install", 2, 3)
        return 0
    assert native.run_native_update_progress(operation) == 0
    assert writes[-1].splitlines()[1] == "success"


def test_failed_display_never_runs_operation(display, monkeypatch):
    def fail(*_):
        raise UpdateFailure("UPD-1007")
    monkeypatch.setattr(native, "_wait_for_visible_window", fail)
    with pytest.raises(UpdateFailure):
        native.run_native_update_progress(lambda _: pytest.fail("must not install"))
    assert display[0] == ["waited"]
    assert display[1][-1].splitlines()[1] == "cancelled"


def test_native_failure_preserves_public_recovery_message(display):
    def fail(_):
        raise UpdateFailure("UPD-1010", "private", public_message="原版本文件已恢复。")
    assert native.run_native_update_progress(fail) == 1
    result = display[1][-1]
    assert result.splitlines()[1] == "failed"
    assert "原版本文件已恢复" in result and "private" not in result


def test_lost_progress_file_does_not_abort_running_transaction(display, monkeypatch):
    writes = native.write_text_atomic
    def operation(progress):
        def unavailable(*_):
            raise PermissionError("locked")
        monkeypatch.setattr(native, "write_text_atomic", unavailable)
        progress("install", 1, 2)
        monkeypatch.setattr(native, "write_text_atomic", writes)
        return 0
    assert native.run_native_update_progress(operation) == 0


@pytest.mark.skipif(os.name != "nt", reason="Windows frozen manager")
def test_frozen_automatic_update_uses_native_display(tmp_path, monkeypatch):
    _root, _prepared, plan = full_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(update_handoff.sys, "frozen", True, raising=False)
    monkeypatch.setattr(native, "run_native_update_progress", lambda operation: 17)
    assert update_handoff.run_automatic_update(plan.plan_path) == 17


def test_component_update_uses_same_visible_handoff_without_consuming_payload(tmp_path, monkeypatch):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
        automatic_install=True,
    )
    saved = json.loads(plan.plan_path.read_text("utf-8"))
    assert plan.automatic_install and saved["automatic_install"] == 1
    assert saved["consume_prepared_payload"] is False
    assert updates.apply_update_plan(plan.plan_path, restart=False, before_wait=lambda: None) == 0
    assert (install / "app/astrbot/data/plugins/example/main.py").read_bytes() == b"new-plugin"
    assert (install / "runtime/unchanged.txt").read_bytes() == b"shared-runtime"


def test_backup_progress_counts_unchanged_user_config_and_missing_targets(tmp_path):
    root, incoming = tmp_path / "install", tmp_path / "payload"
    files = {"app/same": b"same", "app/new": b"new", "config/settings.json": b"{}"}
    make_payload(incoming, files)
    write(root, "app/same", b"same")
    write(root, "config/settings.json", b'{"customer":true}')
    verified = payload.verify_payload(incoming)
    events = []
    journal = update_rollback.snapshot_update_targets(
        incoming, root, root / "updates/.rollback-test", update_id="test", version="1",
        verified_payload=verified, progress_callback=lambda *event: events.append(event),
    )
    assert events[0] == ("backup", 0, 5)
    assert events[-1] == ("backup", 5, 5)
    assert sorted(set(event[1] for event in events)) == list(range(6))
    assert "app/same" in journal["unchanged_files"]
    assert "config/settings.json" not in {entry["path"] for entry in journal["files"]}


def test_progress_logging_is_bounded_for_large_updates():
    api = object.__new__(update_api.UpdateApiMixin)
    api._set_update_status = lambda **kwargs: None
    entries = []
    api._update_logger = SimpleNamespace(write=lambda *args, **kwargs: entries.append(kwargs))
    api._available_update = None
    for phase in ("extract", "verify"):
        for index in range(1, 36_160):
            api._update_progress(phase, index, 36_159)
    assert len(entries) == 22
    assert entries[-1]["details"]["completed"] == 36_159


def test_target_boundary_uses_one_link_query_per_ancestor_and_rejects_escape(tmp_path, monkeypatch):
    target = write(tmp_path, "app/deep/file", b"data")
    original = Path.lstat
    calls = []
    def counted(path, *args, **kwargs):
        calls.append(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", counted)
    assert payload._checked_target(tmp_path, Path("app/deep/file")) == target.resolve()
    assert calls == [target, target.parent, target.parent.parent, tmp_path]
    with pytest.raises(payload.PayloadError, match="越过根目录"):
        payload._checked_target(tmp_path, Path("../outside"))


@pytest.mark.skipif(not os.environ.get("WECHATREBOT_TEST_NATIVE_UI"), reason="compiled Inno display fixture required")
@pytest.mark.parametrize("attempt", range(3))
def test_compiled_inno_handoff_completes_transaction_without_clicks(tmp_path, monkeypatch, attempt):
    install, prepared, plan = full_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(native, "native_progress_executable", lambda: Path(os.environ["WECHATREBOT_TEST_NATIVE_UI"]))
    monkeypatch.setattr(update_handoff.sys, "frozen", True, raising=False)
    monkeypatch.setattr(updates, "_restart_manager", lambda *_a, **_kw: object())
    real_popen = native.subprocess.Popen
    terminated = []
    def launch(*args, **kwargs):
        process = real_popen(*args, **kwargs)
        terminate = process.terminate
        def record_termination():
            terminated.append(True)
            terminate()
        process.terminate = record_termination
        return process
    monkeypatch.setattr(native.subprocess, "Popen", launch)
    def confirm(flag, update_id, _process):
        assert (install / "updates/update-journal.json").is_file()
        updates.write_json_atomic(flag, {"update_id": update_id, "ok": True})
    monkeypatch.setattr(updates, "_wait_for_startup_confirmation", confirm)
    errors = []
    def parent():
        try:
            update_handoff.wait_for_installer(plan.plan_path, plan.update_id, SimpleNamespace(poll=lambda: None), timeout=25)
            update_handoff.signal_handoff(plan.plan_path, plan.update_id, "commit")
        except Exception as exc:
            errors.append(exc)
            update_handoff.signal_handoff(plan.plan_path, plan.update_id, "cancel")
    thread = threading.Thread(target=parent)
    thread.start()
    try:
        assert update_handoff.run_automatic_update(plan.plan_path) == 0
    finally:
        thread.join(30)
    assert not errors and not thread.is_alive()
    assert not terminated, "successful Inno window must close itself, without force termination"
    assert (install / "manager/WeChatReBot.exe").read_bytes() == b"new-manager"
    assert (install / "config/customer.json").read_bytes() == b"customer configuration"
    assert not (install / "updates/update-journal.json").exists()
    assert prepared.archive_path.is_file()
