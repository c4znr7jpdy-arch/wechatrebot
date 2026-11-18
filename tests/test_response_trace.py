import json

import pytest

from wechatrebot_manager.response_trace import ResponseTraceService


FIRST = "a" * 32
SECOND = "b" * 32


def record(identifier, sequence, stage, phase, elapsed, *, operation=0, status=None, duration=None, kind="group", timestamp="2026-11-18T00:00:00+00:00", **extra):
    value = {"schema": 1, "trace_id": identifier, "sequence": sequence, "stage": stage,
             "phase": phase, "elapsed_ms": elapsed, "operation": operation, "kind": kind,
             "status": status or ("pending" if phase == "begin" else "success"), "time": timestamp, **extra}
    if duration is not None:
        value["duration_ms"] = duration
    return "[00:00:00.000] [Core] [INFO] [core.wechat_reply_trace:80]: WRB_REPLY_TRACE " + json.dumps(value)


def log(tmp_path, lines, *, current_path=False):
    path = tmp_path / ("app/logs" if current_path else "logs") / "astrbot-core.log"
    path.parent.mkdir(parents=True)
    path.write_text("\n".join(lines), encoding="utf-8")
    return ResponseTraceService(tmp_path).recent()


def completed(identifier, *, kind="group", end=11000):
    return [record(identifier, 1, "received", "point", 0, kind=kind),
            record(identifier, 2, "pipeline", "begin", 0, operation=2, kind=kind),
            record(identifier, 3, "model", "begin", 1000, operation=3, kind=kind),
            record(identifier, 4, "model", "end", end - 1000, operation=3, duration=end - 2000, kind=kind),
            record(identifier, 5, "send", "begin", end - 1000, operation=5, kind=kind),
            record(identifier, 6, "send", "end", end, operation=5, duration=1000, kind=kind),
            record(identifier, 7, "pipeline", "end", end, operation=2, duration=end, kind=kind)]


@pytest.mark.parametrize("kind", ["group", "private"])
def test_interleaved_replies_are_correlated_even_with_same_chat_kind(tmp_path, kind):
    first, second = completed(FIRST), completed(SECOND, kind=kind)
    lines = [line for pair in zip(first, second) for line in pair]
    result = log(tmp_path, lines)
    assert {item["trace_id"] for item in result} == {FIRST, SECOND}
    assert all(item["duration_seconds"] == 11 and item["complete"] for item in result)
    assert all([stage["code"] for stage in item["stages"]] == ["received", "model", "send"] for item in result)
    assert all(item["status"] == "success" for item in result)


def test_unknown_legacy_logs_never_borrow_nearby_completion_or_timing(tmp_path):
    result = log(tmp_path, [
        "[23:59:59.000] [Core] [INFO] [core.event_bus:60]: secret first message",
        "[00:00:00.000] [Core] [INFO] [core.event_bus:60]: secret second message",
        "[00:00:05.000] [Core] [INFO] [respond.stage:206]: Prepare to send - wxid_secret: secret reply",
        "[00:00:06.000] [Core] [INFO] [angel_heart_context:173]: AI回复完成",
        "[00:00:10.000] [Core] [INFO] [respond.stage:206]: Prepare to send - secret reply",
    ])
    assert len(result) == 2
    assert all(item["duration_seconds"] is None and item["status"] == "pending" for item in result)
    assert all([stage["code"] for stage in item["stages"]] == ["send"] for item in result)
    assert "secret" not in str(result) and "AI回复完成" not in str(result)


def test_partial_trace_has_unknown_total_and_no_invented_stage_duration(tmp_path):
    lines = completed(FIRST)[3:]
    result = log(tmp_path, lines)[0]
    assert result["complete"] is False and result["duration_seconds"] is None
    assert result["status"] == "pending"
    model = next(stage for stage in result["stages"] if stage["code"] == "model")
    assert "duration_seconds" not in model and model["duration_text"] == "耗时未知"
    send = next(stage for stage in result["stages"] if stage["code"] == "send")
    assert send["duration_seconds"] == 1


def test_failed_send_is_not_success_even_when_pipeline_returns_normally(tmp_path):
    lines = completed(FIRST)
    lines[5] = record(FIRST, 6, "send", "end", 11000, operation=5, duration=1000, status="failed")
    result = log(tmp_path, lines)[0]
    assert result["status"] == "failed"
    assert next(stage for stage in result["stages"] if stage["code"] == "send")["status"] == "failed"


def test_exclusive_command_can_stop_after_successful_send(tmp_path):
    lines = completed(FIRST)
    lines[-1] = record(FIRST, 7, "pipeline", "end", 11000, operation=2, duration=11000, status="skipped")
    assert log(tmp_path, lines)[0]["status"] == "success"


def test_cancelled_pipeline_and_model_are_explicit(tmp_path):
    result = log(tmp_path, [record(FIRST, 1, "received", "point", 0),
        record(FIRST, 2, "pipeline", "begin", 0, operation=2),
        record(FIRST, 3, "model", "begin", 10, operation=3),
        record(FIRST, 4, "model", "end", 100, operation=3, duration=90, status="cancelled"),
        record(FIRST, 5, "pipeline", "end", 100, operation=2, duration=100, status="cancelled")])[0]
    assert result["status"] == "cancelled" and result["complete"]
    assert result["stages"][-1]["detail"] == "任务已取消"


def test_monotonic_duration_survives_wall_clock_rollback_and_midnight(tmp_path):
    lines = completed(FIRST)
    lines[0] = record(FIRST, 1, "received", "point", 0, timestamp="2026-11-18T00:00:00+00:00")
    lines[-1] = record(FIRST, 7, "pipeline", "end", 11000, operation=2, duration=11000, timestamp="2026-11-17T23:59:55+00:00")
    assert log(tmp_path, lines)[0]["duration_seconds"] == 11


def test_runtime_log_location_and_privacy_allowlist(tmp_path):
    lines = completed(FIRST)
    lines[0] = record(FIRST, 1, "received", "point", 0, content="private body", token="secret key", user_id="wxid_private")
    result = log(tmp_path, lines, current_path=True)
    assert len(result) == 1
    assert all(word not in str(result) for word in ("private body", "secret key", "wxid_private", "content", "token"))


def test_chat_text_cannot_inject_structured_trace(tmp_path):
    fake = record(FIRST, 1, "received", "point", 0)
    result = log(tmp_path, ["[00:00:00] [Core] [INFO] [core.event_bus:60]: " + fake,
                            fake.replace("[core.wechat_reply_trace:80]", "[roles.front_desk:80]"),
                            fake.replace('"elapsed_ms": 0', '"elapsed_ms": NaN')])
    assert result == []


def test_missing_or_empty_log_is_empty(tmp_path):
    assert ResponseTraceService(tmp_path).recent() == []
    assert ResponseTraceService(tmp_path).recent(0) == []


def test_existing_service_finds_runtime_log_created_after_manager_start(tmp_path):
    service = ResponseTraceService(tmp_path)
    assert service.recent() == []
    log(tmp_path, completed(FIRST), current_path=True)
    assert service.recent()[0]["trace_id"] == FIRST


def test_secondary_cleanup_error_is_safe_and_visible(tmp_path):
    lines = completed(FIRST)
    lines[3] = record(FIRST, 4, "model", "end", 10000, operation=3, duration=9000,
                      status="failed", cleanup_status="cancelled")
    model = next(stage for stage in log(tmp_path, lines)[0]["stages"] if stage["code"] == "model")
    assert model["status"] == "failed" and model["cleanup_status"] == "cancelled"
    assert model["detail"] == "模型流清理被取消"
