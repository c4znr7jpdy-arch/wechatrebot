"""Failure injection for a single durable rollback owner and renamed originals."""

import errno
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_unchanged_update_targets import make_payload, write
from test_updates import _ready_incremental_update
from test_payload import _incremental_payload, _installed_tree
from wechatrebot_manager import payload, update_rollback as rollback, updates
from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.update_errors import UpdateFailure


@pytest.fixture
def transaction(tmp_path):
    install = tmp_path / "install"
    incoming = tmp_path / "payload"
    make_payload(incoming, {
        "app/changed.py": b"new", "app/added.py": b"added",
        "runtime/same.dll": b"unchanged", "manager/WeChatReBot.exe": b"new manager",
    })
    for name, content in {
        "app/changed.py": b"old", "runtime/same.dll": b"unchanged",
        "manager/WeChatReBot.exe": b"old manager",
        "启动机器人.exe": b"old launcher", "卸载机器人.exe": b"old uninstaller",
        ".wechatrebot-install.json": b'{"schema_version":1,"state":"ready"}',
        "config/installed-payload.json": b'{"schema_version":1,"files":[]}',
    }.items():
        write(install, name, content)
    verified = payload.verify_payload(incoming)
    journal = rollback.snapshot_update_targets(
        incoming, install, install / "updates/.rollback-test",
        update_id="test", version="1.0.25", verified_payload=verified,
    )
    write_json_atomic(install / "updates/update-journal.json", journal)
    return install, incoming, journal, rollback.UpdateFileBackup(journal, install)


def entry_backup(install, journal, relative):
    entry = next(item for item in journal["files"] if item["path"] == relative)
    return install / journal["rollback_root"] / entry["backup"]


@pytest.mark.parametrize("phase", ["prepared", "moved", "replaced", "restored"])
def test_interruption_recovers_originals_repeatedly_without_copying_runtime(transaction, monkeypatch, phase):
    install, incoming, journal, owner = transaction
    target = install / "app/changed.py"
    if phase != "prepared":
        owner.retain(target, incoming / "app/changed.py")
        assert not target.exists()
        assert entry_backup(install, journal, "app/changed.py").read_bytes() == b"old"
    if phase in {"replaced", "restored"}:
        target.write_bytes(b"new")
    if phase == "restored":
        rollback.restore_update_targets(journal, install)

    original_copy = rollback.shutil.copy2

    def copy(source, destination, *args, **kwargs):
        assert Path(source) != entry_backup(install, journal, "app/changed.py")
        return original_copy(source, destination, *args, **kwargs)

    monkeypatch.setattr(rollback.shutil, "copy2", copy)
    for _ in range(2):
        persisted = json.loads((install / "updates/update-journal.json").read_text(encoding="utf-8"))
        rollback.restore_update_targets(persisted, install)
        assert target.read_bytes() == b"old"
        assert (install / "manager/WeChatReBot.exe").read_bytes() == b"old manager"


def test_copy_exceptions_remain_launchable_and_runtime_is_not_copied(transaction):
    install, incoming, journal, owner = transaction
    for relative in payload.COPY_BACKUP_PATHS:
        target = install / relative
        original = target.read_bytes()
        owner.retain(target, incoming / "manager/WeChatReBot.exe")
        assert target.read_bytes() == original
        assert entry_backup(install, journal, relative.as_posix()).read_bytes() == original
    assert not entry_backup(install, journal, "app/changed.py").exists()


@pytest.mark.parametrize("mutation", ["intact", "edited", "directory", "missing-intent", "symlink"])
def test_created_target_rollback_deletes_only_verified_bytes(transaction, mutation):
    install, incoming, journal, owner = transaction
    target = install / "app/added.py"
    owner.retain(target, incoming / "app/added.py")
    target.write_bytes(b"added")
    if mutation == "edited":
        target.write_bytes(b"customer edit")
    elif mutation in {"directory", "symlink"}:
        target.unlink()
        if mutation == "directory":
            target.mkdir()
            (target / "customer.txt").write_bytes(b"keep")
        else:
            try:
                target.symlink_to(incoming / "app/added.py")
            except OSError:
                pytest.skip("symlink privilege unavailable")
    elif mutation == "missing-intent":
        entry_backup(install, journal, "app/added.py").unlink()
    if mutation == "intact":
        rollback.restore_update_targets(journal, install)
        assert not target.exists()
    else:
        with pytest.raises(UpdateFailure) as error:
            rollback.restore_update_targets(journal, install)
        assert error.value.definition.code == "UPD-1010"
        assert target.exists()


def test_new_path_appearing_before_retain_is_preserved(transaction):
    install, incoming, journal, owner = transaction
    target = install / "app/added.py"
    target.write_bytes(b"external file")
    with pytest.raises(updates.UpdateError, match="已出现"):
        owner.retain(target, incoming / "app/added.py")
    with pytest.raises(UpdateFailure):
        rollback.restore_update_targets(journal, install)
    assert target.read_bytes() == b"external file"


@pytest.mark.parametrize("failure", ["locked", "cross-volume"])
def test_failed_old_file_move_preserves_original(transaction, monkeypatch, failure):
    install, incoming, journal, owner = transaction

    def fail(*_args):
        if failure == "locked":
            raise PermissionError("locked")
        raise OSError(errno.EXDEV, "cross-volume")

    monkeypatch.setattr(rollback, "_replace_file", fail)
    with pytest.raises(OSError):
        owner.retain(install / "app/changed.py", incoming / "app/changed.py")
    rollback.restore_update_targets(journal, install)
    assert (install / "app/changed.py").read_bytes() == b"old"


@pytest.mark.parametrize("damage", ["missing", "corrupt"])
def test_moved_backup_damage_never_overwrites_target(transaction, damage):
    install, incoming, journal, owner = transaction
    target = install / "app/changed.py"
    owner.retain(target, incoming / "app/changed.py")
    target.write_bytes(b"new")
    backup = entry_backup(install, journal, "app/changed.py")
    backup.unlink() if damage == "missing" else backup.write_bytes(b"corrupt")
    with pytest.raises(UpdateFailure):
        rollback.restore_update_targets(journal, install)
    assert target.read_bytes() == b"new"


def test_duplicate_retain_does_not_replace_original_backup(transaction):
    install, incoming, journal, owner = transaction
    target = install / "app/changed.py"
    owner.retain(target, incoming / "app/changed.py")
    target.write_bytes(b"new")
    with pytest.raises(updates.UpdateError):
        owner.retain(target, incoming / "app/changed.py")
    assert entry_backup(install, journal, "app/changed.py").read_bytes() == b"old"


def test_apply_crash_after_move_uses_persisted_journal(tmp_path, monkeypatch):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
    )
    original_inventory = (install / "config/installed-payload.json").read_bytes()
    changed = install / "app/astrbot/data/plugins/example/main.py"
    original_replace = payload._replace_file

    class Interrupted(BaseException):
        pass

    def replace(source, target):
        if target == changed:
            assert not target.exists()
            journal = json.loads((install / "updates/update-journal.json").read_text(encoding="utf-8"))
            assert entry_backup(install, journal, changed.relative_to(install).as_posix()).read_bytes() == b"old-plugin"
            assert (install / ".wechatrebot-install.json").is_file()
            raise Interrupted()
        original_replace(source, target)

    monkeypatch.setattr(payload, "_replace_file", replace)
    with pytest.raises(Interrupted):
        updates.apply_update_plan(plan.plan_path, restart=False)
    journal = json.loads((install / "updates/update-journal.json").read_text(encoding="utf-8"))
    rollback.restore_update_targets(journal, install)
    assert changed.read_bytes() == b"old-plugin"
    assert (install / "config/installed-payload.json").read_bytes() == original_inventory


def test_outer_transaction_is_only_backup_owner_and_waits_for_startup(tmp_path, monkeypatch):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
    )
    copied = []
    original_copy = rollback.shutil.copy2

    def copy(source, destination, *args, **kwargs):
        copied.append(Path(source).relative_to(install).as_posix())
        return original_copy(source, destination, *args, **kwargs)

    def confirm(*_args):
        journal = json.loads((install / "updates/update-journal.json").read_text(encoding="utf-8"))
        assert len(list((install / "updates").glob(".rollback-*"))) == 1
        assert entry_backup(install, journal, "app/astrbot/data/plugins/example/main.py").read_bytes() == b"old-plugin"
        assert entry_backup(install, journal, "app/astrbot/data/plugins/example/removed.png").read_bytes() == b"unused-image"

    monkeypatch.setattr(rollback.shutil, "copy2", copy)
    monkeypatch.setattr(updates, "_restart_manager", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(updates, "_wait_for_startup_confirmation", confirm)
    assert updates.apply_update_plan(plan.plan_path) == 0
    assert sorted(copied) == [".wechatrebot-install.json", "config/installed-payload.json"]
    assert not list((install / "updates").glob(".rollback-*"))
    assert not (install / "updates/update-journal.json").exists()


def test_standalone_install_restores_moved_file_when_new_file_replace_fails(tmp_path, monkeypatch):
    incoming = tmp_path / "payload"
    make_payload(incoming, {"app/changed.py": b"new"})
    install = tmp_path / "install"
    target = write(install, "app/changed.py", b"old")
    original_replace = payload._replace_file

    def replace(source, destination):
        if destination == target and ".install-" in str(source):
            assert not target.exists()
            raise PermissionError("new file locked")
        original_replace(source, destination)

    monkeypatch.setattr(payload, "_replace_file", replace)
    with pytest.raises(PermissionError):
        payload.install_payload(incoming, install)
    assert target.read_bytes() == b"old"
    assert not list((install / "updates").glob(".rollback-*"))


@pytest.mark.parametrize("capability", [None, 1, 3, True, "2"])
def test_old_plan_keeps_complete_legacy_snapshot_with_one_backup_owner(tmp_path, monkeypatch, capability):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
    )
    data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
    assert data.pop("rollback_journal_schema") == 2
    if capability is not None:
        data["rollback_journal_schema"] = capability
    write_json_atomic(plan.plan_path, data)
    real_install = updates.install_payload

    def install_with_legacy_check(*args, **kwargs):
        journal = json.loads((install / "updates/update-journal.json").read_text(encoding="utf-8"))
        assert journal["schema_version"] == 1
        assert callable(kwargs["before_replace"])
        for item in journal["files"]:
            if item["existed"]:
                assert (install / journal["rollback_root"] / item["backup"]).read_bytes() == (install / item["path"]).read_bytes()
        return real_install(*args, **kwargs)

    monkeypatch.setattr(updates, "install_payload", install_with_legacy_check)
    assert updates.apply_update_plan(plan.plan_path, restart=False) == 0


@pytest.mark.parametrize("fail_manifest", [False, True])
def test_copy_backup_removal_is_deleted_or_restored(tmp_path, monkeypatch, fail_manifest):
    install = tmp_path / "install"
    hashes = _installed_tree(install, {"manager/WeChatReBot.exe": b"manager"})
    delta = _incremental_payload(tmp_path / "delta", install, remove=hashes)
    original_write = payload.write_json_atomic

    def write_failure(path, data):
        original_write(path, data)
        raise OSError("manifest failure")

    if fail_manifest:
        monkeypatch.setattr(payload, "write_json_atomic", write_failure)
        with pytest.raises(OSError):
            payload.install_payload(delta, install)
        assert (install / "manager/WeChatReBot.exe").read_bytes() == b"manager"
    else:
        payload.install_payload(delta, install)
        assert not (install / "manager/WeChatReBot.exe").exists()


def test_journal_write_failure_precedes_any_installed_file_move(tmp_path, monkeypatch):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
    )
    original_write = updates.write_json_atomic
    writes = 0

    def fail_first_journal(path, data):
        nonlocal writes
        if path.name == "update-journal.json":
            writes += 1
            if writes == 1:
                raise OSError("journal unavailable")
        original_write(path, data)

    def forbidden_move(*_args):
        raise AssertionError("installed files must remain untouched")

    monkeypatch.setattr(updates, "write_json_atomic", fail_first_journal)
    monkeypatch.setattr(rollback, "_replace_file", forbidden_move)
    assert updates.apply_update_plan(plan.plan_path, restart=False) == 1
    assert (install / "app/astrbot/data/plugins/example/main.py").read_bytes() == b"old-plugin"


def test_legacy_plan_checks_full_copy_space_before_touching_install(tmp_path, monkeypatch):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
    )
    data = json.loads(plan.plan_path.read_text(encoding="utf-8"))
    data.pop("rollback_journal_schema")
    write_json_atomic(plan.plan_path, data)
    copied_bytes = sum(
        target.stat().st_size for relative in rollback.update_target_paths(prepared.payload_root)
        if (target := install / relative).is_file()
    )
    new_bytes = sum(item.source_path.stat().st_size for item in payload.verify_payload(prepared.payload_root).files)
    monkeypatch.setattr(updates.shutil, "disk_usage", lambda _: SimpleNamespace(
        free=copied_bytes + new_bytes + 64 * 1024**2 - 1,
    ))
    assert updates.apply_update_plan(plan.plan_path, restart=False) == 1
    result = json.loads((install / "updates/update-result.json").read_text(encoding="utf-8"))
    assert result["error_code"] == "UPD-1004"
    assert not list((install / "updates").glob(".rollback-*"))
    assert (install / "app/astrbot/data/plugins/example/main.py").read_bytes() == b"old-plugin"


def test_failure_after_manifest_replacement_restores_exact_inventory(tmp_path, monkeypatch):
    install, prepared = _ready_incremental_update(tmp_path)
    monkeypatch.setattr(updates, "assert_update_runtime_stopped", lambda *_: None)
    plan = updates.create_update_plan(
        prepared, install, executable=install / "manager/WeChatReBot.exe", parent_pid=0,
    )
    manifest = install / "config/installed-payload.json"
    old_manifest = manifest.read_bytes()
    real_replace = payload._replace_file

    def replace_then_fail(source, target):
        real_replace(source, target)
        if target == manifest:
            raise OSError("failed after inventory replacement")

    monkeypatch.setattr(payload, "_replace_file", replace_then_fail)
    assert updates.apply_update_plan(plan.plan_path, restart=False) == 1
    assert manifest.read_bytes() == old_manifest
    assert (install / "app/astrbot/data/plugins/example/main.py").read_bytes() == b"old-plugin"
    assert (install / "app/astrbot/data/plugins/example/removed.png").read_bytes() == b"unused-image"
