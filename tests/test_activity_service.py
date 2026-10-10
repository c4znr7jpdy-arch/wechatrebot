import json
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

from wechatrebot_manager.activity_service import ActivityService, diagnostic_text, classify_log_code
from wechatrebot_manager.app_shell import ApplicationApi
from wechatrebot_manager.customer_logs import LogSource
from wechatrebot_manager.error_catalog import classify_error, load_error_catalog
from wechatrebot_manager.events import EventStore


def test_feed_keeps_multiline_errors_context_and_legacy_states(tmp_path):
    store = EventStore(tmp_path)
    store.emit("APP_ERROR", "failed", code="15001", message="配置写入被拒绝", details={"operation": "save_config"})
    store.emit("APP_ERROR", "failed", code="12003", message="请先关闭微信")
    path = tmp_path / "logs/astrbot-service.log"
    path.write_text('2026-10-10 10:00:00 INFO preparing\n2026-10-10 10:00:01 ERROR model failed\nTraceback (most recent call last):\n  File "provider.py", line 42\nopenai.AuthenticationError: Error code: 401\n2026-10-10 10:00:02 INFO finished\n', encoding="utf-8")
    items = ActivityService(tmp_path).recent()
    model = next(i for i in items if i["code"] == "16002")
    assert "line 42" in model["detail"] and "AuthenticationError" in model["report"]
    assert "preparing" in model["context"] and "finished" in model["context"]
    assert next(i for i in items if i["code"] == "15001")["level"] == "error"
    assert next(i for i in items if i["code"] == "12003")["level"] == "info"
    assert "已恢复" not in json.dumps(items, ensure_ascii=False)


def test_sdk_evidence_does_not_guess_quota_from_http_status():
    assert classify_log_code("ERROR user said HTTP 401") == ""
    assert classify_log_code("openai.RateLimitError: Error code: 429") == "16004"
    assert classify_log_code("openai.RateLimitError: {'code': 'insufficient_quota'}") == "16005"
    error = HTTPError("https://example.test", 401, "unauthorized", {}, None)
    assert classify_error(error, "15001", "discover_provider_models") == "16002"
    assert classify_error(ValueError("some 401 text"), "15001", "test_provider") == "19000"


def test_feed_redacts_secrets_and_private_content_in_every_field(tmp_path):
    path = tmp_path / "logs/astrbot-core.log"
    path.parent.mkdir()
    path.write_text('2026-10-10 10:00:01 ERROR failed\nAuthorization: Bearer full-secret\nCookie: session=cookie-secret; other=secret-too\n{"api_key": "json-secret", "content": "private chat", "wxid": "private-user"}\nhttps://user:password@example.test/api?token=url-secret\nC:\\Users\\PersonalName\\file.py\n', encoding="utf-8")
    text = json.dumps(ActivityService(tmp_path).recent(), ensure_ascii=False)
    for secret in ("full-secret", "cookie-secret", "secret-too", "json-secret", "private chat", "private-user", "url-secret", "PersonalName", "user:password"):
        assert secret not in text
    value = diagnostic_text({"messages": [{"content": "nested-secret"}], "password": "sensitive"}, tmp_path)
    assert "nested-secret" not in value and "sensitive" not in value


def test_duplicates_merge_only_within_five_minutes_and_ids_survive_refresh(tmp_path):
    path = tmp_path / "logs/astrbot-service.log"
    path.parent.mkdir()
    path.write_text("\n".join(f"2026-10-10 {t} ERROR same failure" for t in ("10:00:00", "10:09:00", "10:10:00")), encoding="utf-8")
    service = ActivityService(tmp_path)
    first = service.recent()
    assert [i["count"] for i in first] == [2, 1]
    assert first[0]["id"] == service.recent()[0]["id"]


def test_missing_and_unreadable_sources_are_distinct(tmp_path, monkeypatch):
    service = ActivityService(tmp_path)
    assert service.recent() == []
    directory = tmp_path / "unreadable"
    directory.mkdir()
    monkeypatch.setattr(service, "sources", lambda: (LogSource("engine-runtime", "运行核心", directory),))
    items = service.recent()
    assert len(items) == 1 and items[0]["code"] == "19003"


def test_short_clock_format_and_huge_tail_stay_bounded(tmp_path):
    path = tmp_path / "logs/astrbot-core.log"
    path.parent.mkdir()
    path.write_text("old " * 100000 + "\n[12:00:01] [Core] [ERRO] broken\nTraceback (most recent call last):\nTypeError: missing value\n[12:00:02] [Core] [INFO] next", encoding="utf-8")
    items = ActivityService(tmp_path).recent()
    error = next(i for i in items if i["level"] == "error")
    assert error["timestamp"] is None and "TypeError" in error["detail"]
    assert error["truncated"] is True
    assert len(json.dumps(items)) < 50000


def test_ui_errors_persist_safely_and_original_failure_survives_io_error(tmp_path, monkeypatch):
    api = ApplicationApi.__new__(ApplicationApi)
    api._install_root = tmp_path
    api._catalog = load_error_catalog()
    api._lifecycle = SimpleNamespace(events=EventStore(tmp_path))
    payload = {"message": "TypeError: missing element", "operation": "运行记录", "stack": "test.js:42 token=secret-value"}
    assert api.record_ui_error(payload)["data"]["saved"] is True
    item = api.get_activity()["data"][0]
    assert item["code"] == "19001" and "test.js:42" in item["report"]
    assert "secret-value" not in api._lifecycle.events.path.read_text("utf-8")
    for _ in range(19):
        assert api.record_ui_error(payload)["ok"]
    assert api.record_ui_error(payload)["ok"] is False
    def fail(*args, **kwargs):
        raise PermissionError("denied")
    monkeypatch.setattr(api._lifecycle.events, "emit", fail)
    result = api._failed(ValueError("original failure"))
    assert result["error"]["recorded"] is False


def test_corrupt_structured_log_does_not_hide_valid_records(tmp_path):
    store = EventStore(tmp_path)
    store.emit("APP_ERROR", "failed", code="19001", message="render failed")
    with store.path.open("a", encoding="utf-8") as f:
        f.write('{"bad json"\n')
    assert any(i["code"] == "19001" for i in ActivityService(tmp_path).recent())


def test_activity_export_contains_same_safe_report_and_preserves_summary_export(tmp_path, monkeypatch):
    from wechatrebot_manager.customer_maintenance import CustomerMaintenanceService
    service = CustomerMaintenanceService(tmp_path, is_running=lambda: False)
    monkeypatch.setattr(service, "diagnostic_summary", lambda: {"summary": {}, "privacy": {"contains_logs": False}})
    EventStore(tmp_path).emit("APP_ERROR", "failed", code="19001", message="TypeError token=private-value", details={"technical": "view.js:22"})
    plain = service.export_diagnostics()
    assert "activity" not in json.loads(Path(plain["path"]).read_text("utf-8"))
    exported = service.export_diagnostics(include_activity=True)
    text = Path(exported["path"]).read_text("utf-8")
    payload = json.loads(text)
    assert payload["privacy"]["contains_logs"] is True
    assert payload["activity"][0]["report"] == ActivityService(tmp_path).recent()[0]["report"]
    assert "private-value" not in text


def test_runtime_chat_log_formats_and_nested_payloads_are_redacted(tmp_path):
    for text in (
        '[12:00:01] [Core] [INFO] [core.event_bus:74]: [default] [wechat] Alice/user123: private-message',
        'INFO completion: private-message',
        'INFO Tool `search` Result: private-message',
        'ERROR request_body={"messages": [{"content": [{"text": "private-message"}]}]}',
        'ERROR Cookie: foo=private-message; bar=other-private',
    ):
        result = diagnostic_text(text, tmp_path)
        assert 'private-message' not in result and 'other-private' not in result
