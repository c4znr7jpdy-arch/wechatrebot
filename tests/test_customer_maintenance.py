from __future__ import annotations

import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager.customer_maintenance import (
    CustomerMaintenanceService,
    MaintenanceBusyError,
    MaintenanceError,
    customer_diagnostic_summary,
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
    (root / "data" / "memory.json").write_text(
        json.dumps({"memory": "only-in-backup"}), encoding="utf-8"
    )
    (root / "logs").mkdir()


def test_preview_describes_ranges_without_exposing_file_names(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    service = CustomerMaintenanceService(install, is_running=lambda: False)

    preview = service.preview_backup()

    assert preview["can_create"] is True
    assert preview["file_count"] == 2
    assert {item["label"] for item in preview["contents"]} == {
        "机器人设置",
        "机器人数据与记忆",
    }
    assert "manager.json" not in json.dumps(preview, ensure_ascii=False)
    assert "memory.json" not in json.dumps(preview, ensure_ascii=False)


def test_create_and_list_backup_use_only_managed_backup_directory(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    _install(install)
    service = CustomerMaintenanceService(install, is_running=lambda: False)

    created = service.create_backup()
    listed = service.list_backups()

    assert Path(str(created["path"])).parent == install / "backups"
    assert created["id"].startswith("robot-backup-")
    assert [item["id"] for item in listed] == [created["id"]]
    assert listed[0]["kind"] == "customer_backup"


def test_running_robot_blocks_backup_and_restore_without_writing(
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    _install(install)
    stopped = CustomerMaintenanceService(install, is_running=lambda: False)
    backup = stopped.create_backup()
    manager = install / "config" / "manager.json"
    manager.write_text(json.dumps({"api_key": "changed"}), encoding="utf-8")
    running = CustomerMaintenanceService(install, is_running=lambda: True)

    validation = running.validate_restore(str(backup["id"]))
    assert validation["valid"] is True
    assert validation["can_restore"] is False
    with pytest.raises(MaintenanceBusyError, match="先停止机器人"):
        running.restore_backup(str(backup["id"]))
    with pytest.raises(MaintenanceBusyError, match="先停止机器人"):
        running.create_backup()
    assert json.loads(manager.read_text(encoding="utf-8"))["api_key"] == "changed"


def test_restore_validates_hash_and_creates_recovery_point(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    service = CustomerMaintenanceService(install, is_running=lambda: False)
    backup = service.create_backup()
    (install / "data" / "memory.json").write_text(
        json.dumps({"memory": "changed"}), encoding="utf-8"
    )

    validation = service.validate_restore(str(backup["id"]))
    result = service.restore_backup(str(backup["id"]))

    assert validation["file_count"] == 2
    assert validation["contents"][1]["label"] == "机器人数据与记忆"
    assert result["restored_file_count"] == 2
    assert (install / "backups" / str(result["recovery_point_id"])).is_file()
    memory = json.loads((install / "data" / "memory.json").read_text(encoding="utf-8"))
    assert memory["memory"] == "only-in-backup"


def test_restore_rejects_tampered_content_during_preview(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    service = CustomerMaintenanceService(install, is_running=lambda: False)
    created = service.create_backup()
    backup = install / "backups" / str(created["id"])
    rewritten = tmp_path / "rewritten.zip"
    with zipfile.ZipFile(backup) as source, zipfile.ZipFile(rewritten, "w") as target:
        for info in source.infolist():
            payload = source.read(info)
            if info.filename == "data/memory.json":
                payload = bytes([payload[0] ^ 1]) + payload[1:]
            target.writestr(info, payload)
    rewritten.replace(backup)

    with pytest.raises(MaintenanceError, match="校验失败"):
        service.validate_restore(str(created["id"]))


def test_restore_accepts_only_a_managed_backup_id(tmp_path: Path) -> None:
    install = tmp_path / "install"
    _install(install)
    service = CustomerMaintenanceService(install, is_running=lambda: False)

    with pytest.raises(MaintenanceError, match="备份名称无效"):
        service.validate_restore("../private.zip")
    with pytest.raises(MaintenanceError, match="备份名称无效"):
        service.validate_restore(str(tmp_path / "private.zip"))


def test_customer_diagnostics_drop_configuration_messages_secrets_and_paths() -> None:
    unsafe = {
        "ok": False,
        "summary": {"error_count": 1},
        "checks": [
            {
                "name": "engine-config",
                "ok": False,
                "severity": "error",
                "message": (
                    "AstrBot at E:\\Project\\AstrBot\\data failed; "
                    "Cookie=session-private token=private-token"
                ),
            }
        ],
        "configuration": {
            "chat_messages": ["用户昨晚说了不能导出的正文"],
            "api_key": "sk-private123456789",
            "cookie": "session-private",
            "development_root": "E:\\Project\\AstrBot",
        },
    }

    summary = customer_diagnostic_summary(unsafe)
    rendered = json.dumps(summary, ensure_ascii=False)

    assert summary["privacy"]["contains_message_content"] is False
    assert summary["privacy"]["contains_credentials"] is False
    assert "configuration" not in summary
    assert "不能导出的正文" not in rendered
    assert "session-private" not in rendered
    assert "private-token" not in rendered
    assert "E:\\Project" not in rendered
    assert "AstrBot" not in rendered
    assert "<LOCAL_PATH>" in rendered


def test_diagnostic_export_contains_only_the_safe_projection(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    install = tmp_path / "install"
    _install(install)
    service = CustomerMaintenanceService(install, is_running=lambda: False)
    monkeypatch.setattr(
        "wechatrebot_manager.customer_maintenance.collect_diagnostics",
        lambda _root: {
            "checks": [
                {
                    "name": "install-marker",
                    "ok": True,
                    "severity": "error",
                    "message": "配置可读取",
                }
            ],
            "configuration": {"cookie": "must-not-export"},
        },
    )

    exported = service.export_diagnostics()
    target = Path(str(exported["path"]))
    payload = json.loads(target.read_text(encoding="utf-8"))

    assert target.parent == install / "logs"
    assert payload["ok"] is True
    assert payload["privacy"]["contains_logs"] is False
    assert "must-not-export" not in target.read_text(encoding="utf-8")
    assert len(str(exported["sha256"])) == 64
