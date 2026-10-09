import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager.backup import (
    BackupError,
    create_customer_backup,
    restore_customer_backup,
)


def _install(root: Path) -> None:
    root.mkdir()
    (root / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    (root / "config").mkdir()
    (root / "config" / "manager.json").write_text(
        json.dumps({"api_key": "customer-secret"}), encoding="utf-8"
    )
    (root / "data").mkdir()
    (root / "data" / "memory.json").write_text("{}", encoding="utf-8")
    (root / "logs").mkdir()
    (root / "logs" / "private.log").write_text("private", encoding="utf-8")
    (root / "cache").mkdir()
    (root / "cache" / "download.bin").write_bytes(b"cache")


def test_backup_contains_only_config_data_and_hash_manifest(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    target = tmp_path / "customer-backup.zip"

    result = create_customer_backup(install, target)

    assert result["file_count"] == 2
    assert result["contains_sensitive_configuration"] is True
    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
        assert names == {
            "config/manager.json",
            "data/memory.json",
            "backup-manifest.json",
        }
        manifest = json.loads(archive.read("backup-manifest.json"))
        assert len(manifest["files"]) == 2
        assert all(len(item["sha256"]) == 64 for item in manifest["files"])


def test_backup_includes_active_long_term_memory_but_not_nested_snapshots(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    _install(install)
    memory = (
        install
        / "app"
        / "astrbot"
        / "data"
        / "plugin_data"
        / "astrbot_plugin_livingmemory"
    )
    memory.mkdir(parents=True)
    (memory / "livingmemory.db").write_bytes(b"active-memory")
    (memory / "backups").mkdir()
    (memory / "backups" / "old.db").write_bytes(b"old-snapshot")
    target = tmp_path / "customer-backup.zip"

    create_customer_backup(install, target)

    with zipfile.ZipFile(target) as archive:
        names = set(archive.namelist())
    assert (
        "app/astrbot/data/plugin_data/astrbot_plugin_livingmemory/livingmemory.db"
        in names
    )
    assert not any("/backups/" in name for name in names)


def test_backup_refuses_destination_inside_customer_data(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    with pytest.raises(BackupError, match="不能保存在"):
        create_customer_backup(install, install / "data" / "backup.zip")


def test_authorization_identity_and_policy_are_excluded_from_backups(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    private = install / "data/licensing"
    private.mkdir()
    (private / "device.json").write_bytes(b"private identity")
    (private / "state.json").write_bytes(b"entitlement")
    (install / "config/licensing.json").write_bytes(b"deployment policy")
    target = tmp_path / "backup.zip"
    create_customer_backup(install, target)
    with zipfile.ZipFile(target) as archive:
        assert not any("licensing" in name.casefold() for name in archive.namelist())


@pytest.mark.parametrize("protected", ["data/licensing/device.json", "DATA/LICENSING/state.json", "config/licensing.json"])
def test_restore_rejects_authorization_paths_even_with_valid_hash(tmp_path, protected):
    import hashlib
    install = tmp_path / "install"
    _install(install)
    target = tmp_path / "crafted.zip"
    content = b"forged authorization state"
    manifest = {"schema_version": 1, "product": "WeChat ReBot", "files": [
        {"path": protected, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}]}
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(protected, content)
        archive.writestr("backup-manifest.json", json.dumps(manifest))
    with pytest.raises(BackupError, match="不安全路径"):
        restore_customer_backup(install, target)
    assert not (install / protected).exists()


def test_backup_refuses_links(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    outside = tmp_path / "outside.txt"
    outside.write_text("private", encoding="utf-8")
    link = install / "data" / "outside-link"
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(BackupError, match="符号链接"):
        create_customer_backup(install, tmp_path / "backup.zip")


def test_restore_verifies_backup_and_creates_recovery_point(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    backup = tmp_path / "backup.zip"
    create_customer_backup(install, backup)
    (install / "config" / "manager.json").write_text(
        json.dumps({"api_key": "changed"}), encoding="utf-8"
    )
    (install / "logs" / "private.log").write_text("new-log", encoding="utf-8")

    result = restore_customer_backup(install, backup)

    restored = json.loads(
        (install / "config" / "manager.json").read_text(encoding="utf-8")
    )
    assert restored["api_key"] == "customer-secret"
    assert (install / "logs" / "private.log").read_text() == "new-log"
    recovery = Path(str(result["recovery_point"]))
    assert recovery.is_file()
    with zipfile.ZipFile(recovery) as archive:
        current_before_restore = json.loads(archive.read("config/manager.json"))
        assert current_before_restore["api_key"] == "changed"


def test_restore_rejects_tampered_backup_before_writing(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    backup = tmp_path / "tampered.zip"
    create_customer_backup(install, backup)
    with zipfile.ZipFile(backup, "a") as archive:
        archive.writestr("data/extra.txt", "unlisted")
    original = (install / "config" / "manager.json").read_bytes()

    with pytest.raises(BackupError, match="未登记"):
        restore_customer_backup(install, backup)
    assert (install / "config" / "manager.json").read_bytes() == original


def test_restore_rejects_path_traversal(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    backup = tmp_path / "unsafe.zip"
    manifest = {
        "schema_version": 1,
        "product": "WeChat ReBot",
        "files": [
            {"path": "../outside.txt", "size": 4, "sha256": "0" * 64}
        ],
    }
    with zipfile.ZipFile(backup, "w") as archive:
        archive.writestr("../outside.txt", "evil")
        archive.writestr("backup-manifest.json", json.dumps(manifest))

    with pytest.raises(BackupError, match="不安全"):
        restore_customer_backup(install, backup)
    assert not (tmp_path / "outside.txt").exists()


def test_restore_refuses_backup_stored_inside_customer_data(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    backup = install / "data" / "manually-copied-backup.zip"
    create_customer_backup(install, tmp_path / "source.zip")
    backup.write_bytes((tmp_path / "source.zip").read_bytes())

    with pytest.raises(BackupError, match="恢复文件不能存放"):
        restore_customer_backup(install, backup)


@pytest.mark.parametrize("rollback_fails", [False, True])
def test_restore_preserves_recovery_material_when_rollback_fails(
    tmp_path: Path, monkeypatch, rollback_fails: bool,
) -> None:
    from wechatrebot_manager import backup as module

    install = tmp_path / "install"
    _install(install)
    archive = tmp_path / "backup.zip"
    create_customer_backup(install, archive)
    current = install / "config/manager.json"
    current.write_text('{"api_key": "latest"}', encoding="utf-8")
    before = current.read_bytes()
    replace = module.os.replace

    def fail_replace(source, target):
        source = Path(source)
        if ".restore-" in str(source) and Path(target) == install / "data/memory.json":
            raise PermissionError("simulated restore failure")
        if rollback_fails and ".restore-rollback-" in str(source):
            raise PermissionError("simulated rollback failure")
        return replace(source, target)

    monkeypatch.setattr(module.os, "replace", fail_replace)
    if rollback_fails:
        with pytest.raises(BackupError, match="回滚失败"):
            restore_customer_backup(install, archive)
        recoveries = list((install / "backups").glob("pre-restore-*.zip"))
        assert len(recoveries) == 1
        with zipfile.ZipFile(recoveries[0]) as saved:
            assert saved.read("config/manager.json") == before
        rollback = list((install / "updates").glob(".restore-rollback-*"))
        assert len(rollback) == 1
        assert (rollback[0] / "config/manager.json").read_bytes() == before
    else:
        with pytest.raises(PermissionError, match="restore failure"):
            restore_customer_backup(install, archive)
        assert current.read_bytes() == before
        assert not list((install / "updates").glob(".restore-*"))


def test_backup_rejects_link_in_parent_of_nested_data_root(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    outside = tmp_path / "outside"
    (outside / "astrbot/data/config").mkdir(parents=True)
    (outside / "astrbot/data/config/secret.json").write_text("private")
    try:
        (install / "app").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    with pytest.raises(BackupError, match="符号链接|目录联接"):
        create_customer_backup(install, tmp_path / "backup.zip")


@pytest.mark.parametrize("directory", ["updates", "backups"])
def test_restore_rejects_redirected_transaction_directory(tmp_path, directory):
    install = tmp_path / "install"
    _install(install)
    archive = tmp_path / "backup.zip"
    create_customer_backup(install, archive)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (install / directory).symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")
    before = (install / "config/manager.json").read_bytes()
    with pytest.raises(BackupError, match="符号链接|目录联接"):
        restore_customer_backup(install, archive)
    assert not list(outside.iterdir())
    assert (install / "config/manager.json").read_bytes() == before
