from __future__ import annotations

import ast
from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy/activation-service/backup.py"
spec = importlib.util.spec_from_file_location("activation_backup", SCRIPT)
backup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(backup)
SECRET = b"test-only-private-key-do-not-print"


@pytest.fixture
def source(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / backup.PRIVATE_KEY).write_bytes(SECRET)
    (data / backup.CONTACT).write_text('{"group_name":"test contact"}', encoding="utf-8")
    writer = sqlite3.connect(data / backup.DATABASE)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("PRAGMA wal_autocheckpoint=0")
    writer.execute("PRAGMA foreign_keys=ON")
    writer.execute("PRAGMA user_version=2")
    writer.execute("CREATE TABLE licenses (id INTEGER PRIMARY KEY, revision INTEGER)")
    writer.execute("CREATE TABLE codes (license INTEGER REFERENCES licenses(id), revision INTEGER)")
    writer.execute("INSERT INTO licenses VALUES (1, 1)")
    writer.execute("INSERT INTO codes VALUES (1, 1)")
    writer.commit()
    writer.execute("UPDATE licenses SET revision=2 WHERE id=1")
    writer.execute("UPDATE codes SET revision=2 WHERE license=1")
    writer.commit()
    try:
        yield data, writer
    finally:
        writer.close()


def completed(root):
    if not root.exists():
        return []
    return sorted(path for path in root.iterdir() if backup._complete_files(root, path) is not None)


def link_or_skip(link, target, *, directory=False):
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Symbolic links are unavailable for this test user")


@pytest.mark.parametrize("with_contact", [False, True])
def test_online_snapshot_contains_committed_wal_and_excludes_open_transaction(source, tmp_path, with_contact):
    data, writer = source
    if not with_contact:
        (data / backup.CONTACT).unlink()
    assert (data / (backup.DATABASE + "-wal")).stat().st_size > 0
    writer.execute("BEGIN IMMEDIATE")
    writer.execute("INSERT INTO licenses VALUES (2, 99)")
    writer.execute("UPDATE codes SET revision=99")

    result = backup.create_backup(data, tmp_path / "backups")
    snapshot = Path(result["path"])
    try:
        with sqlite3.connect(snapshot / backup.DATABASE) as db:
            assert db.execute("PRAGMA quick_check").fetchall() == [("ok",)]
            assert db.execute("PRAGMA foreign_key_check").fetchall() == []
            assert db.execute("PRAGMA user_version").fetchone() == (2,)
            assert db.execute("SELECT * FROM licenses").fetchall() == [(1, 2)]
            assert db.execute("SELECT * FROM codes").fetchall() == [(1, 2)]
            assert db.execute("PRAGMA journal_mode").fetchone() == ("delete",)
    finally:
        writer.rollback()
    assert (snapshot / backup.PRIVATE_KEY).read_bytes() == SECRET
    expected = {backup.DATABASE, backup.PRIVATE_KEY, backup.MANIFEST}
    if with_contact:
        expected.add(backup.CONTACT)
        assert (snapshot / backup.CONTACT).read_bytes() == (data / backup.CONTACT).read_bytes()
    assert {path.name for path in snapshot.iterdir()} == expected
    assert backup._complete_files(snapshot.parent, snapshot) == expected
    assert writer.execute("PRAGMA journal_mode").fetchone() == ("wal",)


@pytest.mark.parametrize("damage", ["missing_database", "empty_database", "corrupt_database", "missing_key", "empty_key"])
def test_backup_failure_returns_error_without_success_directory_or_secret(tmp_path, capsys, damage):
    data, root = tmp_path / "data", tmp_path / "backups"
    data.mkdir()
    key = data / backup.PRIVATE_KEY
    key.write_bytes(SECRET)
    database = data / backup.DATABASE
    if damage != "missing_database":
        if damage == "empty_database":
            database.write_bytes(b"")
        elif damage == "corrupt_database":
            database.write_bytes(b"not-a-sqlite-file")
        else:
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE example (id INTEGER)")
    if damage == "missing_key":
        key.unlink()
    elif damage == "empty_key":
        key.write_bytes(b"")

    assert backup.main(["--data-dir", str(data), "--backup-dir", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err)["ok"] is False
    assert SECRET.decode() not in captured.out + captured.err
    assert not completed(root)
    if root.exists():
        assert not any(path.name.endswith(".partial") for path in root.iterdir())


@pytest.mark.parametrize("failure", ["copy", "publish", "rotation"])
def test_later_error_keeps_previous_backup_and_removes_failed_run(source, tmp_path, monkeypatch, capsys, failure):
    data, _ = source
    root = tmp_path / "backups"
    previous = Path(backup.create_backup(data, root)["path"])
    retained = {path.name: path.read_bytes() for path in previous.iterdir()}

    def fail(*args, **kwargs):
        raise OSError("test-only-private-key-do-not-print")

    if failure == "copy":
        original_copy = backup._copy_file

        def copy_then_fail(source, target):
            original_copy(source, target)
            if source.name == backup.CONTACT:
                fail()

        monkeypatch.setattr(backup, "_copy_file", copy_then_fail)
    elif failure == "publish":
        monkeypatch.setattr(Path, "rename", fail)
    else:
        monkeypatch.setattr(backup, "_rotate", fail)

    assert backup.main(["--data-dir", str(data), "--backup-dir", str(root)]) == 1
    captured = capsys.readouterr()
    assert captured.out == "" and json.loads(captured.err)["ok"] is False
    assert SECRET.decode() not in captured.err
    assert completed(root) == [previous]
    assert not any(path.name.endswith(".partial") for path in root.iterdir())
    assert {path.name: path.read_bytes() for path in previous.iterdir()} == retained


def test_rotation_keeps_fourteen_complete_snapshots_and_never_unrelated_material(source, tmp_path, monkeypatch):
    data, _ = source
    root = tmp_path / "backups"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("must survive", encoding="utf-8")
    names = ["unrelated", "backup-20000101T000000000000Z-aaaaaaaaaaaa", ".backup-20000101T000000000000Z-bbbbbbbbbbbb.partial"]
    for name in names:
        (root / name).mkdir()
        (root / name / "keep.txt").write_text("must survive", encoding="utf-8")
    linked = root / "backup-20000101T000000000000Z-cccccccccccc"
    link_or_skip(linked, outside, directory=True)
    linked_file_backup = Path(backup.create_backup(data, root)["path"])
    (linked_file_backup / backup.PRIVATE_KEY).unlink()
    (outside / backup.PRIVATE_KEY).write_bytes(SECRET)
    link_or_skip(linked_file_backup / backup.PRIVATE_KEY, outside / backup.PRIVATE_KEY)
    results = [backup.create_backup(data, root) for _ in range(16)]
    assert completed(root) == sorted(Path(result["path"]) for result in results[-14:])
    assert sum(result["removed_backups"] for result in results) == 2
    for name in names:
        assert (root / name / "keep.txt").read_text("utf-8") == "must survive"
    assert linked.is_symlink() and (outside / "keep.txt").read_text("utf-8") == "must survive"
    assert (linked_file_backup / backup.PRIVATE_KEY).is_symlink()
    assert (outside / backup.PRIVATE_KEY).read_bytes() == SECRET

    class EarlierTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(1999, 1, 1, tzinfo=timezone.utc)

    monkeypatch.setattr(backup, "datetime", EarlierTime)
    latest = Path(backup.create_backup(data, root)["path"])
    assert latest in completed(root) and len(completed(root)) == 14


def test_permissions_are_requested_and_enforced_on_posix(source, tmp_path, monkeypatch):
    data, _ = source
    calls = []
    chmod = os.chmod

    def record(path, mode):
        calls.append((Path(path), mode))
        chmod(path, mode)

    monkeypatch.setattr(backup.os, "chmod", record)
    root = tmp_path / "backups"
    root.mkdir(mode=0o755)
    snapshot = Path(backup.create_backup(data, root)["path"])
    assert (root, 0o700) in calls
    assert {path.name for path, mode in calls if mode == 0o600} >= {backup.DATABASE, backup.PRIVATE_KEY, backup.CONTACT, backup.MANIFEST, ".backup.lock"}
    if os.name == "posix":
        assert stat.S_IMODE(root.stat().st_mode) == stat.S_IMODE(snapshot.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(path.stat().st_mode) == 0o600 for path in snapshot.iterdir())


@pytest.mark.parametrize("location", ["source_directory", "backup_directory", "source_key", "source_contact", "source_wal", "lock"])
def test_source_or_backup_links_are_rejected(source, tmp_path, location):
    data, _ = source
    root = tmp_path / "backups"
    if location == "source_directory":
        linked = tmp_path / "linked-data"
        link_or_skip(linked, data, directory=True)
        data = linked
    elif location == "backup_directory":
        outside = tmp_path / "outside"
        outside.mkdir()
        link_or_skip(root, outside, directory=True)
    elif location == "lock":
        root.mkdir()
        link_or_skip(root / ".backup.lock", data / backup.PRIVATE_KEY)
    else:
        name = {"source_key": backup.PRIVATE_KEY, "source_contact": backup.CONTACT, "source_wal": backup.DATABASE + "-wal"}[location]
        original = data / name
        target = tmp_path / ("real-" + name)
        # Keep the live WAL handle on Windows; swap only before SQLite backup.
        if location == "source_wal":
            target.write_bytes(b"sqlite-sidecar-placeholder")
            data = tmp_path / "linked-source"
            data.mkdir()
            (data / backup.PRIVATE_KEY).write_bytes(SECRET)
            with sqlite3.connect(data / backup.DATABASE) as db:
                db.execute("CREATE TABLE example (id INTEGER)")
            original = data / name
        else:
            original.rename(target)
        link_or_skip(original, target)
    with pytest.raises((backup.BackupError, OSError)):
        backup.create_backup(data, root)
    if root.exists():
        assert not any(backup._NAME.fullmatch(path.name) for path in root.iterdir())


def test_cleanup_refuses_other_roots_and_unmatched_names(tmp_path):
    root = tmp_path / "backups"
    root.mkdir()
    for path in (tmp_path / "outside", root / "unrelated"):
        path.mkdir()
        (path / backup.PRIVATE_KEY).write_bytes(SECRET)
        with pytest.raises(backup.BackupError):
            backup._remove_flat(root, path, {backup.PRIVATE_KEY})
        assert (path / backup.PRIVATE_KEY).read_bytes() == SECRET


def test_concurrent_backup_is_rejected_without_disturbing_completed_snapshot(source, tmp_path):
    data, _ = source
    root = tmp_path / "backups"
    previous = Path(backup.create_backup(data, root)["path"])
    with backup._backup_lock(root):
        with pytest.raises(OSError):
            backup.create_backup(data, root)
    assert completed(root) == [previous]


@pytest.mark.skipif(os.name != "posix", reason="POSIX owner boundary is tested on Linux")
def test_wrong_effective_user_is_rejected_before_creating_backups_or_sqlite_sidecars(source, tmp_path, monkeypatch):
    data, writer = source
    writer.close()
    root = tmp_path / "backups"
    before = {path.name for path in data.iterdir()}
    owner = (data / backup.DATABASE).stat().st_uid
    monkeypatch.setattr(backup.os, "geteuid", lambda: owner + 1)
    with pytest.raises(backup.BackupError, match="database owner"):
        backup.create_backup(data, root)
    assert not root.exists() and {path.name for path in data.iterdir()} == before


def test_script_runs_without_project_imports_and_supports_python310_syntax(source, tmp_path):
    data, _ = source
    ast.parse(SCRIPT.read_text("utf-8"), feature_version=(3, 10))
    result = subprocess.run(
        [sys.executable, "-I", str(SCRIPT), "--data-dir", str(data), "--backup-dir", str(tmp_path / "backups")],
        capture_output=True, text=True, timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["ok"] is True
    assert SECRET.decode() not in result.stdout + result.stderr
