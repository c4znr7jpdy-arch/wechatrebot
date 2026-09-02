import json
from pathlib import Path

from wechatrebot_manager.diagnostics import collect_diagnostics, redact_sensitive


def test_redaction_handles_nested_keys_and_secret_shaped_values() -> None:
    payload = {
        "api_key": "private-value",
        "nested": {"password": "private-value", "note": "key sk-example12345"},
    }
    redacted = redact_sensitive(payload)
    assert redacted["api_key"] == "***REDACTED***"
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
