"""Maintenance and lifecycle actions share ownership without launching services."""
import json
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_app_shell import _api
from test_backup import _install
from wechatrebot_manager import customer_maintenance
from wechatrebot_manager.customer_maintenance import CustomerMaintenanceService


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_backup_mutation_blocks_start_until_disk_work_finishes(tmp_path, monkeypatch, operation):
    root = tmp_path / "install"
    _install(root)
    api = _api(root)
    api._check_license_start = lambda: None
    started = threading.Event()
    api._lifecycle.start = lambda: started.set()
    service = CustomerMaintenanceService(root, is_running=started.is_set)
    api._maintenance = service
    backup = service.create_backup()
    entered, finish = threading.Event(), threading.Event()
    function = "create_customer_backup" if operation == "create" else "restore_customer_backup"
    original = getattr(customer_maintenance, function)

    def disk_work(*args, **kwargs):
        entered.set()
        assert finish.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(customer_maintenance, function, disk_work)
    results = []
    worker = threading.Thread(target=lambda: results.append(
        api.create_backup() if operation == "create" else api.restore_backup(backup["id"])))
    worker.start()
    try:
        assert entered.wait(5)
        accepted = api.start_robot()
        assert accepted["ok"] and not accepted["data"]["accepted"]
        assert not started.is_set()
    finally:
        finish.set()
        worker.join(5)
    assert not worker.is_alive()
    assert results[0]["ok"], results
    assert api.start_robot()["data"]["accepted"]
    assert started.wait(5)


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_lifecycle_operation_blocks_backup_mutation_without_writes(tmp_path, operation):
    root = tmp_path / "install"
    _install(root)
    api = _api(root)
    service = CustomerMaintenanceService(root, is_running=lambda: False)
    api._maintenance = service
    backup = service.create_backup()
    config = root / "config/manager.json"
    config.write_text(json.dumps({"current": True}), encoding="utf-8")
    before = {path.name for path in (root / "backups").iterdir()}
    with api._action_lock:
        result = api.create_backup() if operation == "create" else api.restore_backup(backup["id"])
    assert not result["ok"]
    assert "正在" in result["error"]["message"]
    assert result["error"]["code"] == ("15009" if operation == "create" else "15010")
    assert {path.name for path in (root / "backups").iterdir()} == before
    assert json.loads(config.read_text("utf-8")) == {"current": True}


@pytest.mark.parametrize("operation", ["create", "restore"])
def test_installing_update_blocks_backup_without_calling_service(tmp_path, operation):
    api = _api(tmp_path)
    api._update_installing = True
    service = SimpleNamespace(create_backup=Mock(), restore_backup=Mock())
    api._maintenance = service

    result = api.create_backup() if operation == "create" else api.restore_backup("backup.zip")

    assert not result["ok"]
    assert "更新正在安装" in result["error"]["message"]
    service.create_backup.assert_not_called()
    service.restore_backup.assert_not_called()
    assert api._action_lock.acquire(blocking=False)
    api._action_lock.release()


@pytest.mark.parametrize("operation", ["create", "restore"])
@pytest.mark.parametrize("failure", [OSError("private-file-path"), ValueError("private-value")])
def test_backup_failure_keeps_operation_identity_and_releases_lock(tmp_path, operation, failure):
    api = _api(tmp_path)
    api._maintenance = SimpleNamespace(
        create_backup=Mock(side_effect=failure), restore_backup=Mock(side_effect=failure),
    )
    api._record_error = Mock(return_value=("test-event", True))

    result = api.create_backup() if operation == "create" else api.restore_backup("backup.zip")

    code = "15009" if operation == "create" else "15010"
    assert not result["ok"]
    assert result["error"]["code"] == code
    assert str(failure) not in json.dumps(result)
    api._record_error.assert_called_once_with(failure, code, operation=f"{operation}_backup")
    assert api._action_lock.acquire(blocking=False)
    api._action_lock.release()
