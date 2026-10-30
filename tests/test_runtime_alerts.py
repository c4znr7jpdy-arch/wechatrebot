"""Run-center alert boundaries without starting services or sending requests."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from wechatrebot_manager.activity_service import ActivityService
from wechatrebot_manager.app_shell import ApplicationApi, PUBLIC_API_METHODS
from wechatrebot_manager.customer_logs import LogSource
from wechatrebot_manager.runtime_alerts import ALERT_CATEGORIES, RuntimeAlertsService
from wechatrebot_manager import runtime_alerts


NOW = datetime(2026, 10, 30, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()


def record(*, category="memory", ago=20, count=4, **values):
    at = NOW - ago
    message = (
        "[2026-10-30 09:59:40.000] [Core] [ERRO] [v4.26.6] "
        "[core.plugin_initializer:233]: 重试初始化失败: 初始化失败: "
        "向量模型连接失败或未返回有效向量，请检查模型服务；原有记忆和索引未改动"
    ) if category == "memory" else "可靠异常记录"
    return {
        "id": "rotating-log-id", "source": "engine-runtime", "level": "error",
        "code": "19000", "message": message, "detail": "脱敏技术详情",
        "report": "脱敏问题报告", "time": datetime.fromtimestamp(at, timezone.utc).isoformat(),
        "timestamp": at, "first_time": datetime.fromtimestamp(at - 10, timezone.utc).isoformat(),
        "count": count, **values,
    }


def service(tmp_path, now=None, monotonic=None):
    return RuntimeAlertsService(tmp_path, clock=lambda: now[0] if now else NOW,
                                monotonic=lambda: monotonic[0] if monotonic else 0)


def test_memory_failure_is_semantic_and_does_not_mute_generic_19000(tmp_path):
    api = service(tmp_path)
    data = api.snapshot(records=[record(), record(category="other")])
    assert len(data["items"]) == 1
    alert = data["items"][0]
    assert alert["category"] == "memory_initialization"
    assert alert["state"] == "recovery_unconfirmed"
    assert alert["count"] == 4 and alert["report"] == "脱敏问题报告"
    assert "19000" not in alert["id"]
    assert data["window_seconds"] == 3600


def test_memory_data_preservation_statement_stays_in_original_report(tmp_path):
    api = service(tmp_path)
    alert = api.snapshot(records=[record(report=record()["message"])])["items"][0]
    assert "原有记忆和索引未改动" not in alert["message"]
    assert "原有记忆和索引未改动" in alert["report"]


@pytest.mark.parametrize("values", [
    {"source": "manager"}, {"level": "info"}, {"message": "用户提到了向量模型连接失败"},
    {"message": "[2026-10-30 09:59:40] [Core] [ERRO] [core.event_bus:74]: 向量模型连接失败"},
    {"timestamp": None}, {"timestamp": NOW - 3601}, {"timestamp": NOW + 60},
])
def test_untrusted_old_or_undated_records_do_not_become_home_alerts(tmp_path, values):
    assert service(tmp_path).snapshot(records=[record(**values)])["items"] == []


def test_memory_recovery_and_robot_restart_clear_prior_run_evidence(tmp_path):
    api = service(tmp_path)
    original = api.snapshot(records=[record()])["items"][0]
    closed_at = api.set_preference(original["category"], "close", original["id"])["items"][0]["closed_at"]
    recovered = record(ago=5, level="info", message=(
        "[2026-10-30 09:59:55] [Core] [INFO] [v4.26.6] "
        "[core.plugin_initializer_finalize:346]: LivingMemory 插件初始化成功。"))
    assert api.snapshot(records=[record(), recovered])["items"] == []
    new_failure = api.snapshot(records=[record(ago=1), recovered])["items"][0]
    assert new_failure["id"] == original["id"] and new_failure["hidden"]
    assert new_failure["closed_at"] == closed_at
    restart = record(ago=2, source="manager", level="info", event="CORE_START")
    assert api.snapshot(records=[record(), restart])["items"] == []
    after_restart = api.snapshot(records=[restart, record(ago=1)])["items"][0]
    assert after_restart["id"] == original["id"] and after_restart["hidden"]
    assert after_restart["closed_at"] == closed_at
    assert not service(tmp_path).snapshot(records=[restart, record(ago=1)])["items"][0]["hidden"]


@pytest.mark.parametrize("module,message", [
    ("core.plugin_initializer", "重试初始化成功"),
    ("core.event_bus", "LivingMemory 插件初始化成功。"),
    ("core.plugin_initializer_finalize", "开始完整初始化流程..."),
])
def test_unverified_initialization_messages_do_not_clear_memory_failure(tmp_path, module, message):
    recovered = record(ago=5, level="info", message=(
        f"[2026-10-30 09:59:55] [Core] [INFO] [{module}:346]: {message}"))
    assert service(tmp_path).snapshot(records=[record(), recovered])["items"]


def test_close_survives_log_regrouping_and_ends_with_new_manager_session(tmp_path):
    now = [NOW]
    api = service(tmp_path, now)
    original = api.snapshot(records=[record()])["items"][0]
    result = api.set_preference(original["category"], "close", original["id"])
    closed_at = result["items"][0]["closed_at"]
    assert datetime.fromisoformat(closed_at).timestamp() == NOW
    assert closed_at != original["time"]
    now[0] += 900
    updated = record(id="new-log-id", timestamp=NOW + 895, count=12, time="2026-10-30T10:14:55+08:00")
    repeated = api.snapshot(records=[updated])["items"][0]
    assert repeated["hidden"] and repeated["id"] == original["id"]
    assert repeated["closed_at"] == closed_at
    fresh = service(tmp_path, now).snapshot(records=[updated])
    assert not fresh["items"][0]["hidden"] and fresh["closed"] == []
    assert not api.path.exists()


def test_mute_is_durable_per_category_and_restore_preserves_session_close(tmp_path):
    api = service(tmp_path)
    alert = api.snapshot(records=[record()])["items"][0]
    api.set_preference(alert["category"], "close", alert["id"])
    muted = api.set_preference(alert["category"], "mute")
    assert muted["items"][0]["muted_at"]
    fresh = service(tmp_path)
    rows = fresh.snapshot(records=[record(), record(category="other", code="16002")])["items"]
    assert next(item for item in rows if item["category"] == alert["category"])["hidden"]
    assert not next(item for item in rows if item["category"] == "model_service_unavailable")["hidden"]
    category = next(item for item in fresh.snapshot(records=[])["categories"] if item["id"] == alert["category"])
    assert category["muted_at"]
    restored = api.set_preference(alert["category"], "restore")
    assert restored["items"][0]["hidden"] and not restored["items"][0]["muted_at"]
    assert not api.set_preference(alert["category"], "reopen", alert["id"])["items"][0]["hidden"]


def test_failed_durable_save_does_not_hide_alert_or_modify_prior_preferences(tmp_path, monkeypatch):
    api = service(tmp_path)
    alert = api.snapshot(records=[record()])["items"][0]
    def fail(*args):
        raise OSError("permission denied")
    monkeypatch.setattr(runtime_alerts, "write_json_atomic", fail)
    with pytest.raises(OSError):
        api.set_preference(alert["category"], "mute")
    assert not api.snapshot(records=[record()])["items"][0]["hidden"]
    assert not api.path.exists()


def test_corrupt_preferences_fail_visibly_instead_of_overwriting_or_hiding(tmp_path):
    api = service(tmp_path)
    alert = api.snapshot(records=[record()])["items"][0]
    api.path.parent.mkdir(parents=True)
    api.path.write_text('{broken', encoding="utf-8")
    with pytest.raises(ValueError):
        api.set_preference(alert["category"], "mute")
    with pytest.raises(ValueError):
        api.set_preference(alert["category"], "close", alert["id"])
    assert api._closed == {} and api.path.read_text("utf-8") == '{broken'


def test_concurrent_category_mutes_preserve_each_other(tmp_path):
    categories = list(ALERT_CATEGORIES)
    services = [service(tmp_path) for _ in categories]
    with ThreadPoolExecutor(max_workers=7) as pool:
        list(pool.map(lambda pair: pair[0].set_preference(pair[1], "mute"), zip(services, categories)))
    saved = json.loads(services[0].path.read_text("utf-8"))
    assert set(saved["muted"]) == set(categories)


@pytest.mark.parametrize("category,action,identity", [
    ("19000", "mute", ""), ("../other", "mute", ""), ([], "mute", ""),
    ("memory_initialization", [], ""), ("memory_initialization", "close", "unknown"),
    ("memory_initialization", "close", []), ("memory_initialization", "reopen", "unknown"),
])
def test_preferences_reject_invalid_targets(tmp_path, category, action, identity):
    with pytest.raises(ValueError):
        service(tmp_path).set_preference(category, action, identity)


def test_repeated_model_and_delivery_failures_use_reliable_runtime_evidence(tmp_path):
    api = service(tmp_path)
    records = [record(category="other", count=1, code="16007")]
    assert api.snapshot(records=records)["items"] == []
    records[0]["count"] = 3
    assert api.snapshot(records=records)["items"][0]["category"] == "model_requests_unstable"
    assert api.snapshot(records=[record(category="other", ago=301, code="16007")])["items"] == []
    manual = record(category="other", code="16002", source="manager", operation="test_provider")
    assert api.snapshot(records=[manual])["items"] == []
    assert api.snapshot(records=[record(category="other", count=1, code="17001")])["items"] == []
    assert api.snapshot(records=[record(category="other", count=1, code="17002")])["items"][0]["category"] == "message_delivery_problem"


def test_model_instability_counts_only_certain_failures_in_last_five_minutes(tmp_path):
    api = service(tmp_path)
    old = record(category="other", ago=2400, count=2, code="16007")
    current = record(category="other", ago=1, count=1, code="16007")
    assert api.snapshot(records=[old, current])["items"] == []
    spanning = record(category="other", count=20, code="16007", first_time="2026-10-30T09:20:00+08:00")
    assert api.snapshot(records=[spanning])["items"] == []


def test_connection_requires_previous_online_and_grace_but_recovers_immediately(tmp_path):
    ticks = [0]
    api = service(tmp_path, monotonic=ticks)
    waiting = {"state": "WAITING_WECHAT", "online": False}
    assert api.snapshot(waiting, records=[])["items"] == []
    api.snapshot({"state": "RUNNING", "online": True}, records=[])
    assert api.snapshot(waiting, records=[])["items"] == []
    ticks[0] += 11
    alert = api.snapshot(waiting, records=[])["items"][0]
    assert alert["state"] == "active" and alert["category"] == "wechat_connection_interrupted"
    closed_at = api.set_preference(alert["category"], "close", alert["id"])["items"][0]["closed_at"]
    assert api.snapshot({"state": "RUNNING", "online": True}, records=[])["items"] == []
    api.snapshot(waiting, records=[])
    ticks[0] += 11
    recurrence = api.snapshot(waiting, records=[])["items"][0]
    assert recurrence["id"] == alert["id"] and recurrence["hidden"]
    assert recurrence["closed_at"] == closed_at
    assert api.snapshot({"state": "STOPPED", "online": False}, records=[])["items"] == []


def test_robot_restart_clears_observed_online_but_keeps_session_close(tmp_path):
    ticks = [0]
    api = service(tmp_path, monotonic=ticks)
    api.snapshot({"state": "RUNNING", "online": True}, records=[])
    waiting = {"state": "WAITING_WECHAT", "online": False}
    api.snapshot(waiting, records=[])
    ticks[0] = 11
    alert = api.snapshot(waiting, records=[])["items"][0]
    api.set_preference(alert["category"], "close", alert["id"])
    restart = record(ago=1, source="manager", level="info", event="CORE_START")
    assert api.snapshot(waiting, records=[restart])["items"] == []
    ticks[0] = 30
    assert api.snapshot(waiting, records=[restart])["items"] == []
    assert api._closed[alert["id"]]["closed_at"]


def test_crash_requires_current_error_not_historical_record(tmp_path):
    api = service(tmp_path)
    crash = record(category="other", source="manager", code="13002", message="托管服务意外退出。")
    assert api.snapshot({"state": "RUNNING", "online": True}, records=[crash])["items"] == []
    error = {"state": "ERROR", "online": False, "error": {"code": "13002"}}
    assert api.snapshot(error, records=[crash])["items"][0]["category"] == "robot_service_stopped"
    assert api.snapshot({"state": "STOPPED"}, records=[crash])["items"] == []


def test_enabled_task_dependency_issue_is_current_and_disabled_task_is_not(tmp_path):
    api = service(tmp_path)
    task = {"enabled": True, "status": "blocked", "definition_version": runtime_alerts.SCHEDULED_COMMAND_VERSION}
    alert = api.snapshot(records=[], tasks=[task])["items"][0]
    assert alert["category"] == "scheduled_task_blocked" and alert["state"] == "active"
    assert api.snapshot(records=[], tasks=[{**task, "enabled": False}])["items"] == []


def test_actual_activity_redaction_remains_the_diagnostic_boundary(tmp_path):
    raw = record()["message"] + '\nAuthorization: Bearer private-key\nC:\\Users\\PrivatePerson\\memory.db'
    item = ActivityService(tmp_path)._item(LogSource("engine-runtime", "运行核心", tmp_path / "sample.log"), raw)
    item["timestamp"] = NOW - 20
    item["report"] = ActivityService(tmp_path).report(item)
    data = service(tmp_path).snapshot(records=[item])
    rendered = json.dumps(data, ensure_ascii=False)
    assert "private-key" not in rendered and "PrivatePerson" not in rendered
    assert len(data["items"]) == 1


def test_api_exposes_only_explicit_alert_operations_and_does_not_refresh_services(tmp_path):
    api = ApplicationApi.__new__(ApplicationApi)
    api._install_root = tmp_path
    api._build_number = 0
    api._catalog = runtime_alerts.ActivityService(tmp_path).catalog
    api._lifecycle = SimpleNamespace(view=lambda: SimpleNamespace(to_dict=lambda: {"state": "STOPPED", "runtime": {}}))
    api._runtime_alerts = service(tmp_path)
    api._runtime_alerts.activity.recent = lambda: [record()]
    result = api.get_runtime_alerts()
    assert result["ok"] and len(result["data"]["items"]) == 1
    assert {"get_runtime_alerts", "set_runtime_alert_preference"} <= set(PUBLIC_API_METHODS)
    alert = result["data"]["items"][0]
    assert api.set_runtime_alert_preference(alert["category"], "close", alert["id"])["data"]["items"][0]["hidden"]
