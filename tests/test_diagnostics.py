import json
from pathlib import Path

from wechatrebot_manager.diagnostics import (
    collect_diagnostics,
    diagnostic_error_messages,
    redact_local_paths,
    redact_sensitive,
)


def test_redaction_handles_nested_keys_and_secret_shaped_values() -> None:
    payload = {
        "api_key": "private-value",
        "admins_id": ["wxid_private"],
        "nested": {"password": "private-value", "note": "key sk-example12345"},
    }
    redacted = redact_sensitive(payload)
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["admins_id"] == "***REDACTED***"
    assert redacted["nested"]["password"] == "***REDACTED***"
    assert "sk-example12345" not in redacted["nested"]["note"]


def test_diagnostics_report_missing_runtime_without_exposing_secrets(
    tmp_path: Path,
) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    (tmp_path / "config" / "manager.json").write_text(
        json.dumps({"ports": {"astrbot_ws": 6200}, "api_token": "private-value"}),
        encoding="utf-8",
    )
    report = collect_diagnostics(tmp_path)
    assert not report["ok"]
    assert report["configuration"]["manager_config"]["api_token"] == "***REDACTED***"
    assert any(item["name"] == "python-runtime" for item in report["checks"])
    assert report["summary"]["warning_count"] == 2
    assert diagnostic_error_messages(report)


def test_diagnostics_redact_install_wechat_and_user_paths() -> None:
    payload = {
        "install": "D:\\客户\\机器人\\config",
        "wechat": "D:\\Tools\\Weixin\\Weixin.exe",
        "home": "C:\\Users\\Customer\\Desktop",
    }
    redacted = redact_local_paths(
        payload,
        {
            "D:\\客户\\机器人": "<INSTALL_ROOT>",
            "D:\\Tools\\Weixin\\Weixin.exe": "<WECHAT_EXE>",
            "C:\\Users\\Customer": "<USER_HOME>",
        },
    )
    text = json.dumps(redacted, ensure_ascii=False)
    assert "客户" not in text
    assert "Customer" not in text
    assert redacted["wechat"] == "<WECHAT_EXE>"


def test_diagnostics_summarize_large_installed_payload(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / ".wechatrebot-install.json").write_text(
        json.dumps({"schema_version": 1, "state": "ready"}), encoding="utf-8"
    )
    (tmp_path / "config" / "manager.json").write_text(
        json.dumps({"ports": {}}), encoding="utf-8"
    )
    files = [
        {"path": f"runtime/private/path-{index}.py", "sha256": "A" * 64}
        for index in range(100)
    ]
    (tmp_path / "config" / "installed-payload.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_manifest_status": "phase-1-incomplete",
                "files": files,
            }
        ),
        encoding="utf-8",
    )

    report = collect_diagnostics(tmp_path)

    summary = report["configuration"]["installed_payload"]
    assert summary["file_count"] == 100
    assert summary["file_list_valid"] is True
    assert len(summary["manifest_sha256"]) == 64
    assert "files" not in summary
    assert "private/path" not in json.dumps(report)
