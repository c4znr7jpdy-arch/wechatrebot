"""A backup manifest describes the archived snapshot, even if a source changes."""
import hashlib
import json
import zipfile

import pytest

from test_backup import _install
from wechatrebot_manager.backup import create_customer_backup, restore_customer_backup


@pytest.mark.parametrize("new_content", [b'{"memory":"new"}', b'{"memory":"a longer newer value"}'])
def test_source_changed_after_archive_write_keeps_manifest_restorable(tmp_path, monkeypatch, new_content):
    root = tmp_path / "install"
    _install(root)
    source = root / "data/memory.json"
    original = b'{"memory":"old"}'
    source.write_bytes(original)
    archive_path = tmp_path / "backup.zip"
    close = zipfile._ZipWriteFile.close
    changed = False

    def close_member(member):
        nonlocal changed
        close(member)
        if member._zinfo.filename == "data/memory.json" and not changed:
            source.write_bytes(new_content)
            changed = True

    monkeypatch.setattr(zipfile._ZipWriteFile, "close", close_member)
    create_customer_backup(root, archive_path)

    assert changed and source.read_bytes() == new_content
    with zipfile.ZipFile(archive_path) as archive:
        manifest = json.loads(archive.read("backup-manifest.json"))
        for item in manifest["files"]:
            content = archive.read(item["path"])
            assert item["size"] == len(content)
            assert item["sha256"] == hashlib.sha256(content).hexdigest().upper()
        assert archive.read("data/memory.json") == original

    result = restore_customer_backup(root, archive_path)
    assert result["restored_file_count"] == 2
    assert source.read_bytes() == original
    with zipfile.ZipFile(result["recovery_point"]) as recovery:
        assert recovery.read("data/memory.json") == new_content


@pytest.mark.parametrize("content", [b"", bytes(range(256)) * 8193], ids=["empty", "multichunk"])
def test_streamed_backup_restores_empty_and_multichunk_binary_data(tmp_path, content):
    root = tmp_path / "install"
    _install(root)
    source = root / "data/memory.json"
    source.write_bytes(content)
    archive_path = tmp_path / "backup.zip"

    create_customer_backup(root, archive_path)
    source.write_bytes(b"later")
    restore_customer_backup(root, archive_path)

    assert source.read_bytes() == content


def test_failed_stream_write_keeps_previous_backup_and_removes_partial_file(tmp_path, monkeypatch):
    root = tmp_path / "install"
    _install(root)
    (root / "data/memory.json").write_bytes(b"x" * (1024 * 1024 + 5))
    archive_path = tmp_path / "backup.zip"
    create_customer_backup(root, archive_path)
    previous = archive_path.read_bytes()
    write = zipfile._ZipWriteFile.write

    def fail_mid_member(member, content):
        if member._zinfo.filename == "data/memory.json" and member._file_size:
            raise OSError("test full disk")
        return write(member, content)

    monkeypatch.setattr(zipfile._ZipWriteFile, "write", fail_mid_member)
    with pytest.raises(OSError, match="test full disk"):
        create_customer_backup(root, archive_path)

    assert archive_path.read_bytes() == previous
    assert not list(tmp_path.glob("*.partial"))
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
