"""Automatic overwrite, handoff and cache consumption without a live robot."""

import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace
import zipfile

import pytest

from test_app_shell import _download_ready, _downloadable_update, _wait_update_worker
from test_unchanged_update_targets import make_payload, write
from test_updates import _channel_and_key, _ready_payload
from test_update_progress import isolated_tk_test
from wechatrebot_manager import payload, update_api, update_handoff as handoff, updates
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.update_errors import UpdateFailure


def full_plan(tmp_path, monkeypatch):
    install = tmp_path / "中文 安装"
    write(install, "manager/WeChatReBot.exe", b"old manager")
    write(install, "app/feature.txt", b"old feature")
    write(install, ".wechatrebot-install.json", b'{"schema_version":1,"state":"ready"}')
    write(install, "config/customer.json", b"customer configuration")
    working = install / "updates/.pending-full"
    incoming = _ready_payload(working / "extracted/payload")
    archive = working / "update.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        for source in incoming.rglob("*"):
            if source.is_file():
                packed.write(source, "payload/" + source.relative_to(incoming).as_posix())
    release = updates.UpdateRelease(
        "1.0.25", "update", (), "", "https://example.test/a.zip",
        archive.stat().st_size, payload.sha256_file(archive), False,
    )
    prepared = updates.PreparedUpdate(release, working, incoming, archive)
    updates.record_update_archive(working, release)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
        automatic_install=True,
    )
    return install, prepared, plan


def test_full_update_consumes_background_extraction_without_second_copy(tmp_path, monkeypatch):
    install, prepared, plan = full_plan(tmp_path, monkeypatch)
    assert plan.automatic_install
    assert plan.helper_executable.read_bytes() == b"old manager"
    original_archive = prepared.archive_path.read_bytes()
    stages = []

    def forbid_copy(source, target):
        raise AssertionError("pre-extracted full payload must not be staged again")

    monkeypatch.setattr(payload, "_copy_and_hash", forbid_copy)
    assert updates.apply_update_plan(
        plan.plan_path, restart=False, before_wait=lambda: None,
        progress_callback=lambda *event: stages.append(event),
    ) == 0
    assert (install / "app/feature.txt").read_text() == "updated"
    assert (install / "manager/WeChatReBot.exe").read_bytes() == b"new-manager"
    assert (install / "config/customer.json").read_bytes() == b"customer configuration"
    assert not (prepared.payload_root / "manager/WeChatReBot.exe").exists()
    assert prepared.archive_path.read_bytes() == original_archive
    assert (prepared.payload_root.parent / "payload-consumed.json").is_file()
    assert {event[0] for event in stages} >= {"waiting", "verify", "install", "cleanup"}
    assert not (install / "updates/update-journal.json").exists()


def test_failed_consume_restores_files_and_invalidates_prepared_cache(tmp_path, monkeypatch):
    install, prepared, plan = full_plan(tmp_path, monkeypatch)
    real_replace = payload._replace_file

    def fail_manager(source, target):
        if target == install / "manager/WeChatReBot.exe":
            raise PermissionError("manager locked")
        real_replace(source, target)

    monkeypatch.setattr(payload, "_replace_file", fail_manager)
    assert updates.apply_update_plan(plan.plan_path, restart=False, before_wait=lambda: None) == 1
    assert (install / "app/feature.txt").read_bytes() == b"old feature"
    assert (install / "manager/WeChatReBot.exe").read_bytes() == b"old manager"
    assert prepared.archive_path.is_file()
    api = object.__new__(update_api.UpdateApiMixin)
    api._prepared_update = prepared
    assert not api._prepared_matches(prepared.release)
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_kw: pytest.fail("retry must reuse retained archive"))
    channel, _key = _channel_and_key()
    rebuilt = updates.prepare_update(prepared.release, channel, install)
    assert rebuilt.payload_root != prepared.payload_root
    assert payload.verify_payload(rebuilt.payload_root)
    assert not (rebuilt.payload_root.parent / "payload-consumed.json").exists()


def test_source_drift_after_preparation_rolls_back_preceding_replacement(tmp_path):
    install = tmp_path / "install"
    incoming = install / "updates/.pending/payload"
    make_payload(incoming, {"app/first": b"new first", "app/last": b"new last"})
    write(install, "app/first", b"old first")
    write(install, "app/last", b"old last")
    from wechatrebot_manager.update_rollback import UpdateFileBackup, snapshot_update_targets, restore_update_targets
    journal = snapshot_update_targets(incoming, install, install / "updates/.rollback-test", update_id="test", version="1")
    write_json_atomic(install / "updates/update-journal.json", journal)
    owner = UpdateFileBackup(journal, install)

    def drift(phase, current, _total):
        if phase == "install" and current == 1:
            (incoming / "app/last").write_bytes(b"tampered")

    with pytest.raises(payload.PayloadError, match="已改变"):
        payload.install_payload(incoming, install, consume_prepared=True, before_replace=owner.retain, progress_callback=drift)
    restore_update_targets(journal, install)
    assert (install / "app/first").read_bytes() == b"old first"
    assert (install / "app/last").read_bytes() == b"old last"


def test_source_edit_inside_backup_callback_is_detected_and_recovered(tmp_path, monkeypatch):
    install, prepared, plan = full_plan(tmp_path, monkeypatch)
    original = updates.UpdateFileBackup.retain

    def tamper(owner, target, incoming=None):
        original(owner, target, incoming)
        if target == install / "app/feature.txt":
            incoming.write_bytes(b"tampered during retain")

    monkeypatch.setattr(updates.UpdateFileBackup, "retain", tamper)
    assert updates.apply_update_plan(plan.plan_path, restart=False, before_wait=lambda: None) == 1
    assert (install / "app/feature.txt").read_bytes() == b"old feature"


@pytest.mark.parametrize("outside,owner", [(True, True), (False, False)])
def test_consume_requires_owned_update_directory_and_durable_owner(tmp_path, outside, owner):
    install = tmp_path / "install"
    install.mkdir()
    incoming = tmp_path / "outside" if outside else install / "updates/.pending/payload"
    make_payload(incoming, {"app/first": b"new"})
    with pytest.raises(payload.PayloadError):
        payload.install_payload(incoming, install, consume_prepared=True, before_replace=(lambda *_: None) if owner else None)
    assert not (install / "app/first").exists()


def test_automatic_plan_without_handoff_never_changes_files(tmp_path, monkeypatch):
    install, _prepared, plan = full_plan(tmp_path, monkeypatch)
    assert updates.apply_update_plan(plan.plan_path, restart=False) == 2
    assert (install / "app/feature.txt").read_bytes() == b"old feature"
    assert not (install / "updates/update-journal.json").exists()


def test_cancelled_handoff_never_changes_files_or_restarts_manager(tmp_path, monkeypatch):
    install, _prepared, plan = full_plan(tmp_path, monkeypatch)
    handoff.signal_handoff(plan.plan_path, plan.update_id, "cancel")
    monkeypatch.setattr(updates, "_restart_manager", lambda *_a, **_kw: pytest.fail("old manager is still running"))
    assert updates.apply_update_plan(
        plan.plan_path, before_wait=lambda: handoff.wait_for_commit(plan.plan_path, plan.update_id, timeout=.5),
    ) == 0
    assert (install / "app/feature.txt").read_bytes() == b"old feature"
    assert not (install / "updates/update-journal.json").exists()


def test_cancellation_during_final_process_wait_never_starts_install(tmp_path, monkeypatch):
    install, _prepared, plan = full_plan(tmp_path, monkeypatch)

    def cancel_then_exit(_pid, **_kwargs):
        handoff.signal_handoff(plan.plan_path, plan.update_id, "cancel")

    monkeypatch.setattr(updates, "_wait_for_process_exit", cancel_then_exit)
    monkeypatch.setattr(updates, "_restart_manager", lambda *_a, **_kw: pytest.fail("cancelled update must not restart"))
    assert updates.apply_update_plan(plan.plan_path, before_wait=lambda: None) == 0
    assert (install / "app/feature.txt").read_bytes() == b"old feature"
    assert not (install / "updates/update-journal.json").exists()


def test_old_process_exit_timeout_does_not_launch_duplicate_manager(tmp_path, monkeypatch):
    install, _prepared, plan = full_plan(tmp_path, monkeypatch)
    def waiting(_pid, **_kwargs):
        raise updates.UpdateError("等待旧版本退出超时")
    monkeypatch.setattr(updates, "_wait_for_process_exit", waiting)
    monkeypatch.setattr(updates, "_restart_manager", lambda *_a, **_kw: pytest.fail("old manager still alive"))
    assert updates.apply_update_plan(plan.plan_path, before_wait=lambda: None) == 1
    assert (install / "app/feature.txt").read_bytes() == b"old feature"


def test_ready_receipt_precedes_commit_and_rejects_wrong_transaction(tmp_path):
    plan_path = tmp_path / "plan.json"
    update_id = "a" * 32
    ready = handoff._signal_path(plan_path, update_id, "ready")
    write_json_atomic(ready, {"update_id": "b" * 32})
    process = SimpleNamespace(poll=lambda: None)
    with pytest.raises(UpdateFailure):
        handoff.wait_for_installer(plan_path, update_id, process, timeout=.01)
    errors = []

    def worker():
        try:
            handoff.wait_for_commit(plan_path, update_id, timeout=2)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        handoff.wait_for_installer(plan_path, update_id, process, timeout=1)
        assert thread.is_alive()
        handoff.commit_handoff(plan_path, update_id, process)
    finally:
        thread.join(3)
    assert not errors and not thread.is_alive()


def test_dead_installer_cannot_accept_commit(tmp_path):
    plan_path, update_id = tmp_path / "plan.json", "a" * 32
    handoff.signal_handoff(plan_path, update_id, "ready")
    with pytest.raises(UpdateFailure):
        handoff.commit_handoff(plan_path, update_id, SimpleNamespace(poll=lambda: 0))
    assert not handoff._signal_path(plan_path, update_id, "commit").exists()


def automatic_api(monkeypatch, tmp_path):
    api, prepared, events = _downloadable_update(monkeypatch, tmp_path)
    plan = updates.UpdatePlan(tmp_path / "plan.json", tmp_path / "helper.exe", True, "a" * 32)
    monkeypatch.setattr(update_api, "create_update_plan", lambda *_a, **_kw: events.append("verify_plan") or plan)
    monkeypatch.setattr(update_api, "wait_for_installer", lambda *_: events.append("ready"))
    monkeypatch.setattr(update_api, "commit_handoff", lambda *_: events.append("commit"))
    def cancel(*_args):
        events.append("cancel")
        api._update_helper.poll = lambda: 0
    monkeypatch.setattr(update_api, "cancel_handoff", cancel)
    from wechatrebot_manager import windows_file_holders
    monkeypatch.setattr(windows_file_holders, "file_holders", lambda _: ())
    return api, prepared, events


def test_visible_installer_ready_before_services_stop_and_commit(monkeypatch, tmp_path):
    api, prepared, events = automatic_api(monkeypatch, tmp_path)
    ready = _download_ready(api)
    api.install_update(prepared.release.version, ready["confirmation_token"])
    assert _wait_update_worker(api)["state"] == "restarting"
    assert events == ["download", "verify_plan", "launch", "ready", "stop_all", "verify_stopped", "commit"]


@pytest.mark.parametrize("phase", ["window", "stop", "commit"])
def test_handoff_failure_cancels_helper_and_never_commits(monkeypatch, tmp_path, phase):
    api, prepared, events = automatic_api(monkeypatch, tmp_path)

    def fail(*_args):
        raise UpdateFailure("UPD-1007", "synthetic failure")

    if phase == "window":
        monkeypatch.setattr(update_api, "wait_for_installer", fail)
    elif phase == "stop":
        api._lifecycle.force_stop_for_update = fail
    else:
        monkeypatch.setattr(update_api, "commit_handoff", fail)
    ready = _download_ready(api)
    api.install_update(prepared.release.version, ready["confirmation_token"])
    assert _wait_update_worker(api)["state"] == "failed"
    assert "commit" not in events and events[-1] == "cancel"
    if phase == "window":
        assert "stop_all" not in events
    assert api._update_helper is None


def test_cancelled_parent_with_live_helper_child_keeps_guard(monkeypatch, tmp_path):
    api, prepared, _events = automatic_api(monkeypatch, tmp_path)
    monkeypatch.setattr(update_api, "wait_for_installer", lambda *_: (_ for _ in ()).throw(UpdateFailure("UPD-1007")))
    from wechatrebot_manager import windows_file_holders
    monkeypatch.setattr(windows_file_holders, "file_holders", lambda _: ((123, object()),))
    ready = _download_ready(api)
    api.install_update(prepared.release.version, ready["confirmation_token"])
    _wait_update_worker(api)
    assert api._update_installing and api._update_helper is not None


def test_cancel_timeout_blocks_overlapping_installers(monkeypatch, tmp_path):
    api, prepared, _events = automatic_api(monkeypatch, tmp_path)
    monkeypatch.setattr(update_api, "wait_for_installer", lambda *_: (_ for _ in ()).throw(UpdateFailure("UPD-1007")))
    monkeypatch.setattr(update_api, "cancel_handoff", lambda *_: (_ for _ in ()).throw(UpdateFailure("UPD-1007")))
    ready = _download_ready(api)
    api.install_update(prepared.release.version, ready["confirmation_token"])
    _wait_update_worker(api)
    assert api._update_installing and api._update_helper is not None
    assert not api.download_update(prepared.release.version)["data"]["accepted"]


@pytest.mark.skipif(os.name != "nt", reason="Windows installation progress window")
@pytest.mark.parametrize("attempt", range(3))
def test_visible_full_install_completes_without_clicks(tmp_path, monkeypatch, attempt, request):
    if isolated_tk_test(request):
        return
    import tkinter as tk
    root = tk.Tk()
    timed_out = []
    def timeout():
        timed_out.append(True)
        root.destroy()
    root.after(20_000, timeout)
    monkeypatch.setattr(tk, "Tk", lambda: root)
    install, prepared, plan = full_plan(tmp_path, monkeypatch)
    monkeypatch.setattr(updates, "_restart_manager", lambda *_a, **_kw: object())

    def confirm(flag, update_id, _process):
        assert (install / "manager/WeChatReBot.exe").read_bytes() == b"new-manager"
        assert (install / "updates/update-journal.json").is_file()
        write_json_atomic(flag, {"update_id": update_id, "ok": True})

    monkeypatch.setattr(updates, "_wait_for_startup_confirmation", confirm)
    errors = []

    def parent():
        try:
            handoff.wait_for_installer(plan.plan_path, plan.update_id, SimpleNamespace(poll=lambda: None), timeout=10)
            handoff.signal_handoff(plan.plan_path, plan.update_id, "commit")
        except Exception as exc:
            errors.append(exc)
            handoff.signal_handoff(plan.plan_path, plan.update_id, "cancel")

    thread = threading.Thread(target=parent)
    thread.start()
    try:
        assert handoff.run_automatic_update(plan.plan_path) == 0
    finally:
        thread.join(12)
    assert not errors
    assert not timed_out
    assert (install / "config/customer.json").read_bytes() == b"customer configuration"
    assert prepared.archive_path.is_file()
    assert not (install / "updates/update-journal.json").exists()
