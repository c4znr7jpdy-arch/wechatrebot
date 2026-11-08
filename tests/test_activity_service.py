import json
from datetime import datetime
import os
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

from wechatrebot_manager.activity_service import ActivityService, diagnostic_text, classify_log_code
from wechatrebot_manager.app_shell import ApplicationApi
from wechatrebot_manager.customer_logs import LogSource
from wechatrebot_manager.error_catalog import classify_error, load_error_catalog
from wechatrebot_manager.events import EventStore


@pytest.fixture(autouse=True)
def activity_clock(monkeypatch):
    # The dated sample logs must stay inside the rolling retention window.
    class FrozenDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 10, 10, 12).astimezone(tz)
    monkeypatch.setattr("wechatrebot_manager.events.datetime", FrozenDateTime)
    monkeypatch.setattr("wechatrebot_manager.activity_service.time.time", lambda: datetime(2026, 10, 10, 12).timestamp())


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


def test_colored_service_logs_keep_timeout_and_adjacent_records_separate(tmp_path):
    path = tmp_path / "logs/astrbot-service.log"
    path.parent.mkdir()
    path.write_text(
        '\x1b[32m[12:00:00.100]\x1b[0m [Core] \x1b[1m[INFO]\x1b[0m preparing\n'
        '\x1b[31m[12:00:01.200]\x1b[0m [Core] \x1b[1m[ERRO]\x1b[0m model failed\n'
        'Traceback (most recent call last):\n'
        '  File "provider.py", line 42\n'
        'openai.APITimeoutError: Request timed out.\n'
        'Authorization: Bearer timeout-secret\n'
        '\x1b[32m[12:00:02.300]\x1b[0m [Core] \x1b[1m[INFO]\x1b[0m finished\n',
        encoding="utf-8",
    )
    items = ActivityService(tmp_path).recent()
    assert len(items) == 3
    timeout = next(item for item in items if item["code"] == "16007")
    assert timeout["level"] == "warning"
    assert "APITimeoutError" in timeout["detail"]
    assert "preparing" not in timeout["detail"] and "finished" not in timeout["detail"]
    assert "preparing" in timeout["context"] and "finished" in timeout["context"]
    assert "timeout-secret" not in json.dumps(items)
    assert "\\u001b" not in json.dumps(items)


def test_model_timeout_classification_requires_exception_evidence():
    assert classify_log_code("openai._exceptions.APITimeoutError: Request timed out.") == "16007"
    assert classify_log_code("WARNING 在场超时(60秒)，转为离场") == ""
    assert classify_log_code("ERROR 用户提到了 openai.APITimeoutError: Request timed out.") == ""
    assert classify_log_code("ERROR model output: 模型超时") == ""


@pytest.mark.parametrize("error_type,status,code", [
    ("APITimeoutError", "none", "16007"),
    ("ReadTimeout", "none", "16007"),
    ("APIConnectionError", "none", "16006"),
    ("RateLimitError", "429", "16004"),
    ("RuntimeError", "503", "16008"),
    ("RuntimeError", "none", ""),
])
def test_model_failure_marker_requires_runtime_logger_prefix(error_type, status, code):
    marker = f"MODEL_REQUEST_FAILED error_type={error_type} status_code={status}"
    prefix = "[12:00:01.200] [Core] [WARN] [v4.26.6] [sources.request_retry:44]: "
    assert classify_log_code(prefix + marker) == code
    assert classify_log_code(marker) == ""
    assert classify_log_code(f"[12:00:01.200] [Core] [INFO] [core.event_bus:74]: {marker}") == ""
    assert classify_log_code(f"[12:00:01.200] [Core] [ERRO] [other.module:74]: {marker}") == ""


def test_activity_reads_application_runtime_log_with_legacy_fallback(tmp_path):
    legacy = tmp_path / "logs/astrbot-core.log"
    legacy.parent.mkdir()
    legacy.write_text("2026-10-10 10:00:00 ERROR legacy-runtime-error\n", encoding="utf-8")
    assert any("legacy-runtime-error" in item["detail"] for item in ActivityService(tmp_path).recent())
    runtime = tmp_path / "app/logs/astrbot-core.log"
    runtime.parent.mkdir(parents=True)
    runtime.write_text(
        "2026-10-10 11:00:00 ERROR model failed\n"
        "openai.APITimeoutError: Request timed out.\n", encoding="utf-8",
    )
    items = ActivityService(tmp_path).recent()
    assert any(item["source"] == "engine-runtime" and item["code"] == "16007" for item in items)
    assert not any("legacy-runtime-error" in item["detail"] for item in items)


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


def test_continuous_duplicates_remain_one_record_across_multiple_windows(tmp_path):
    path = tmp_path / "logs/astrbot-service.log"
    path.parent.mkdir()
    path.write_text("\n".join(f"2026-10-10 10:{minute:02d}:00 ERROR same failure" for minute in range(20)), encoding="utf-8")
    items = ActivityService(tmp_path).recent()
    assert len(items) == 1
    assert items[0]["count"] == 20
    assert items[0]["first_time"] == "2026-10-10 10:00:00"
    assert items[0]["time"] == "2026-10-10 10:19:00"


def test_activity_filters_old_sources_and_keeps_exact_retention_boundary(tmp_path):
    log = tmp_path / "logs/astrbot-service.log"
    log.parent.mkdir()
    log.write_text("2026-10-08 11:59:59 ERROR expired\n2026-10-08 12:00:00 ERROR boundary\n2026-10-10 11:00:00 ERROR current", encoding="utf-8")
    undated = tmp_path / "logs/astrbot-core.log"
    undated.write_text("[12:00:00] ERROR expired undated", encoding="utf-8")
    stale = datetime(2026, 10, 8, 11, 59).timestamp()
    os.utime(undated, (stale, stale))
    items = ActivityService(tmp_path).recent()
    assert len(items) == 2
    assert all("expired" not in item["report"] for item in items)
    assert any("boundary" in item["message"] for item in items)


def test_feed_preserves_persisted_repeat_evidence(tmp_path):
    store = EventStore(tmp_path)
    first = store.emit("APP_ERROR", "failed", code="19001", message="repeating error")
    for _ in range(30):
        store.emit("APP_ERROR", "failed", code="19001", message="repeating error")
    item = ActivityService(tmp_path).recent()[0]
    assert item["id"] == first.event_id
    assert item["count"] == 31
    assert item["first_time"] == first.time
    assert "31 次" in item["report"]


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


@pytest.mark.parametrize("code", ["13002", "13004"])
def test_service_exit_report_includes_nearby_traceback_once_and_redacts_it(tmp_path, code, monkeypatch):
    import wechatrebot_manager.activity_service as activity

    event = EventStore(tmp_path).emit("APP_ERROR", "failed", code=code, message="托管服务意外退出。")
    block = (
        "2026-10-10 11:59:58 ERROR API request failed\n"
        "Traceback (most recent call last):\n"
        '  File "client.py", line 42\n'
        "TimeoutError: API request timed out after 30s\n"
        "Authorization: Bearer near-secret\n"
        "wxid=private-user\n"
    )
    for relative in ("logs/new-hook-bridge.log", "app/bridge/var/log/bridge.log"):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            "2026-10-10 11:59:57 INFO unrelated-private-chat\n" + block
            + "2026-10-10 11:59:59 INFO unrelated-completion\n", encoding="utf-8",
        )
    reads = []
    original = activity.read_log_tail

    def count_read(path, **kwargs):
        reads.append(path)
        return original(path, **kwargs)

    monkeypatch.setattr(activity, "read_log_tail", count_read)
    service = ActivityService(tmp_path)
    items = service.recent()
    item = next(item for item in items if item["id"] == event.event_id)
    assert item["context"].count("Traceback (most recent call last)") == 1
    assert "11:59:58" in item["context"] and "微信连接" in item["context"]
    assert "line 42" in item["context"] and "30s" in item["context"]
    assert "不代表因果关系" in item["context"]
    assert item["report"] == service.report(item)
    for secret in ("near-secret", "private-user", "unrelated-private-chat", "unrelated-completion"):
        assert secret not in item["report"]
    assert len(reads) == len(set(reads))


def test_service_exit_context_includes_nearby_warning_without_traceback(tmp_path):
    event = EventStore(tmp_path).emit("APP_ERROR", "failed", code="13002", message="service exited")
    (tmp_path / "logs/new-hook-bridge.log").write_text(
        "2026-10-10 11:59:58 WARNING reconnect-exhausted\n", encoding="utf-8",
    )
    items = ActivityService(tmp_path).recent()
    assert next(item for item in items if "reconnect-exhausted" in item["message"])["level"] == "warning"
    item = next(item for item in items if item["id"] == event.event_id)
    assert "reconnect-exhausted" in item["context"]


@pytest.mark.parametrize("offset,included", [(-6, False), (-5, True), (5, True), (6, False)])
def test_service_exit_context_uses_exact_five_second_window(tmp_path, offset, included):
    event = EventStore(tmp_path).emit("APP_ERROR", "failed", code="13002", message="service exited")
    timestamp = datetime(2026, 10, 10, 12).timestamp() + offset
    stamp = datetime.fromtimestamp(timestamp).isoformat(sep=" ")
    (tmp_path / "logs/new-hook-bridge.log").write_text(f"{stamp} ERROR boundary-failure\n", encoding="utf-8")
    item = next(item for item in ActivityService(tmp_path).recent() if item["id"] == event.event_id)
    assert ("boundary-failure" in item["context"]) is included


@pytest.mark.parametrize("code,event", [("19001", "APP_ERROR"), ("13002", "CORE_STOP")])
def test_service_exit_context_does_not_attach_to_unrelated_manager_events(tmp_path, code, event):
    record = EventStore(tmp_path).emit(event, "failed", code=code, message="some event")
    (tmp_path / "logs/new-hook-bridge.log").write_text("2026-10-10 11:59:58 ERROR nearby-failure\n", encoding="utf-8")
    item = next(item for item in ActivityService(tmp_path).recent() if item["id"] == record.event_id)
    assert item["context"] == ""


def test_service_exit_context_does_not_infer_undated_log_time_or_include_other_installations(tmp_path):
    root = tmp_path / "selected"
    event = EventStore(root).emit("APP_ERROR", "failed", code="13002", message="service exited")
    (root / "logs/new-hook-bridge.log").write_text("[11:59:58] ERROR undated-failure\n", encoding="utf-8")
    other = tmp_path / "another/logs/new-hook-bridge.log"
    other.parent.mkdir(parents=True)
    other.write_text("2026-10-10 11:59:58 ERROR other-installation-failure\n", encoding="utf-8")
    item = next(item for item in ActivityService(root).recent() if item["id"] == event.event_id)
    assert item["context"] == ""


def test_service_exit_context_uses_original_occurrences_before_repeat_grouping(tmp_path):
    event = EventStore(tmp_path).emit("APP_ERROR", "failed", code="13002", message="service exited")
    (tmp_path / "logs/new-hook-bridge.log").write_text(
        "2026-10-10 11:59:58 ERROR repeating-failure\n"
        "2026-10-10 12:01:00 ERROR repeating-failure\n", encoding="utf-8",
    )
    item = next(item for item in ActivityService(tmp_path).recent() if item["id"] == event.event_id)
    assert "11:59:58" in item["context"] and "12:01:00" not in item["context"]


def test_service_exit_context_limits_records_to_three_and_text_to_existing_cap(tmp_path):
    event = EventStore(tmp_path).emit("APP_ERROR", "failed", code="13002", message="service exited")
    path = tmp_path / "logs/new-hook-bridge.log"
    path.write_text("\n".join(f"2026-10-10 12:00:0{index} ERROR nearby-{index}" for index in range(5)), encoding="utf-8")
    service = ActivityService(tmp_path)
    item = next(item for item in service.recent() if item["id"] == event.event_id)
    assert all(f"nearby-{index}" in item["context"] for index in range(3))
    assert "nearby-3" not in item["context"] and "nearby-4" not in item["context"]
    path.write_text("2026-10-10 12:00:00 ERROR " + "large-failure " * 1000, encoding="utf-8")
    item = next(item for item in service.recent() if item["id"] == event.event_id)
    assert len(item["context"]) <= 6000 + len("\n[内容超出上限，已截断]")
    assert item["context"].endswith("[内容超出上限，已截断]")


def test_service_exit_export_uses_the_same_context_as_copy_report(tmp_path, monkeypatch):
    from wechatrebot_manager.customer_maintenance import CustomerMaintenanceService

    event = EventStore(tmp_path).emit("APP_ERROR", "failed", code="13002", message="service exited")
    (tmp_path / "logs/new-hook-bridge.log").write_text(
        "2026-10-10 11:59:58 ERROR nearby-failure token=export-secret\n", encoding="utf-8",
    )
    item = next(item for item in ActivityService(tmp_path).recent() if item["id"] == event.event_id)
    service = CustomerMaintenanceService(tmp_path, is_running=lambda: False)
    monkeypatch.setattr(service, "diagnostic_summary", lambda: {"summary": {}, "privacy": {"contains_logs": False}})
    exported = service.export_diagnostics(include_activity=True)
    payload = json.loads(Path(exported["path"]).read_text("utf-8"))
    copied = next(record["report"] for record in payload["activity"] if record["id"] == event.event_id)
    assert "nearby-failure" in copied and "export-secret" not in copied
    assert copied == item["report"]
