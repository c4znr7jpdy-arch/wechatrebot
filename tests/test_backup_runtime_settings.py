"""Customer backups include the exact core and bridge settings they must restore."""

import hashlib
import json
from pathlib import Path
import zipfile

import pytest

from test_backup import _install
from wechatrebot_manager import backup
from wechatrebot_manager.brand import BrandProfile, install_brand_profile
from wechatrebot_manager.customer_maintenance import CustomerMaintenanceService
from wechatrebot_manager.provider import OpenAIProviderProfile
from wechatrebot_manager.runtime_config import RuntimePorts, write_runtime_configs


def write(root, relative, content):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target


def archive_with_file(target, relative, content):
    manifest = {
        "schema_version": 1, "product": "WeChat ReBot", "files": [{
            "path": relative, "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }],
    }
    with zipfile.ZipFile(target, "w") as archive:
        archive.writestr(relative, content)
        archive.writestr("backup-manifest.json", json.dumps(manifest))


def test_customer_backup_round_trip_preserves_models_admins_personas_and_connections(tmp_path):
    install = tmp_path / "install"
    _install(install)
    brand = install_brand_profile(install, BrandProfile("客户机器人", "客户", "客户人格"))
    write_runtime_configs(
        install, tmp_path / "Weixin.exe", brand_profile=brand,
        provider_profile=OpenAIProviderProfile(
            api_key="synthetic-original-key", api_base="https://example.test/v1", model="original-model",
        ), admin_id="wxid_customer_admin",
        ports=RuntimePorts(dashboard=16185, astrbot_ws=16200, bridge_callback=15000, hook_api=29088),
    )
    engine_path = install / "app/astrbot/data/cmd_config.json"
    engine = json.loads(engine_path.read_text("utf-8"))
    engine["admins_id"].append("wxid_second_admin")
    engine["persona"][0]["prompt"] = "客户独立编辑的人格"
    engine_path.write_text(json.dumps(engine), encoding="utf-8")
    originals = {name: (install / name).read_bytes() for name in backup.BACKUP_FILES}
    excluded = ["runtime/python.exe", "app/bridge/src/hook_bridge/app.py", "app/astrbot/engine.py"]
    for name in excluded:
        write(install, name, b"runtime code")
    license_path = write(install, "data/licensing/state.json", b"original license state")
    policy_path = write(install, "config/licensing.json", b"original policy")
    service = CustomerMaintenanceService(install, is_running=lambda: False)

    created = service.create_backup()
    with zipfile.ZipFile(created["path"]) as archive:
        assert all(name in archive.namelist() for name in backup.BACKUP_FILES)
        assert not any(name in archive.namelist() for name in excluded)
        assert not any("licensing" in name for name in archive.namelist())
    changed = {}
    for name in backup.BACKUP_FILES:
        changed[name] = b'{"customer_changed":true}'
        (install / name).write_bytes(changed[name])
    license_path.write_bytes(b"current license state")
    policy_path.write_bytes(b"current policy")

    validation = service.validate_restore(created["id"])
    assert validation["valid"] is True and validation["can_restore"] is True
    result = service.restore_backup(created["id"])

    for name in backup.BACKUP_FILES:
        assert (install / name).read_bytes() == originals[name]
    with zipfile.ZipFile(install / "backups" / result["recovery_point_id"]) as archive:
        for name in backup.BACKUP_FILES:
            assert archive.read(name) == changed[name]
    assert license_path.read_bytes() == b"current license state"
    assert policy_path.read_bytes() == b"current policy"
    assert all((install / name).read_bytes() == b"runtime code" for name in excluded)


def test_legacy_backup_restores_its_files_without_touching_newly_supported_settings(tmp_path):
    install = tmp_path / "install"
    _install(install)
    for name in backup.BACKUP_FILES:
        write(install, name, b'{"current":true}')
    legacy = tmp_path / "legacy.zip"
    archive_with_file(legacy, "config/manager.json", b'{"legacy":true}')

    result = backup.restore_customer_backup(install, legacy)

    assert result["restored_file_count"] == 1
    assert (install / "config/manager.json").read_bytes() == b'{"legacy":true}'
    assert all((install / name).read_bytes() == b'{"current":true}' for name in backup.BACKUP_FILES)


def test_restore_recreates_missing_runtime_settings(tmp_path):
    install = tmp_path / "install"
    _install(install)
    for name in backup.BACKUP_FILES:
        write(install, name, b'{"original":true}')
    archive = tmp_path / "backup.zip"
    backup.create_customer_backup(install, archive)
    for name in backup.BACKUP_FILES:
        (install / name).unlink()

    backup.restore_customer_backup(install, archive)

    assert all((install / name).read_bytes() == b'{"original":true}' for name in backup.BACKUP_FILES)


@pytest.mark.parametrize("relative", [
    "app/bridge/config.json/child.txt", "app/astrbot/data/cmd_config.json/child.txt",
    "app/bridge/config.json.exe", "app/bridge/src/hook_bridge/app.py", "app/astrbot/data/other.json",
])
def test_added_settings_whitelist_rejects_descendants_and_neighboring_runtime_files(tmp_path, relative):
    install = tmp_path / "install"
    _install(install)
    crafted = tmp_path / "crafted.zip"
    archive_with_file(crafted, relative, b"unwanted runtime change")

    with pytest.raises(backup.BackupError, match="不安全路径"):
        backup.restore_customer_backup(install, crafted)

    assert not (install / relative).exists()


@pytest.mark.parametrize("relative", backup.BACKUP_FILES)
@pytest.mark.parametrize("operation", ["create", "restore"])
def test_backup_and_restore_reject_redirected_runtime_settings(tmp_path, relative, operation):
    install = tmp_path / "install"
    _install(install)
    archive = tmp_path / "backup.zip"
    archive_with_file(archive, relative, b'{"restored":true}')
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside private data")
    linked = install / relative
    linked.parent.mkdir(parents=True, exist_ok=True)
    try:
        linked.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable in this environment")

    with pytest.raises(backup.BackupError, match="符号链接|目录联接"):
        if operation == "create":
            backup.create_customer_backup(install, tmp_path / "new.zip")
        else:
            backup.restore_customer_backup(install, archive)

    assert linked.is_symlink() and outside.read_bytes() == b"outside private data"


@pytest.mark.parametrize("relative", backup.BACKUP_FILES)
def test_single_runtime_setting_cannot_be_a_directory(tmp_path, relative):
    install = tmp_path / "install"
    _install(install)
    directory = install / relative
    directory.mkdir(parents=True)
    (directory / "unrelated.txt").write_bytes(b"not settings")

    with pytest.raises(backup.BackupError, match="普通文件"):
        backup.create_customer_backup(install, tmp_path / "backup.zip")


def test_runtime_settings_restore_failure_rolls_back_previous_setting(tmp_path, monkeypatch):
    install = tmp_path / "install"
    _install(install)
    for name in backup.BACKUP_FILES:
        write(install, name, b'{"original":true}')
    archive = tmp_path / "backup.zip"
    backup.create_customer_backup(install, archive)
    for name in backup.BACKUP_FILES:
        (install / name).write_bytes(b'{"current":true}')
    replace = backup.os.replace

    def fail_bridge(source, target):
        if ".restore-" in str(source) and Path(target) == install / "app/bridge/config.json":
            raise PermissionError("synthetic bridge restore failure")
        return replace(source, target)

    monkeypatch.setattr(backup.os, "replace", fail_bridge)
    with pytest.raises(PermissionError, match="synthetic bridge restore failure"):
        backup.restore_customer_backup(install, archive)

    assert all((install / name).read_bytes() == b'{"current":true}' for name in backup.BACKUP_FILES)
    assert not list((install / "updates").glob(".restore-*"))
