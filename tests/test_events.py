import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from wechatrebot_manager.events import EventStore
from wechatrebot_manager.customer_logs import ACTIVITY_RETENTION_SECONDS


def test_event_store_appends_structured_redacted_records(tmp_path: Path) -> None:
    store = EventStore(tmp_path)
    store.emit(
        "APP_ERROR",
        "failed",
        code="12002",
        message="AstrBot token=customer-secret",
        details={"api_key": "private", "phase": "hook_bridge"},
    )

    record = json.loads(store.path.read_text(encoding="utf-8"))
    assert record["event"] == "APP_ERROR"
    assert record["code"] == "12002"
    assert "AstrBot" not in record["message"]
    assert "customer-secret" not in record["message"]
    assert record["details"]["api_key"] == "***"
    assert "hook_bridge" not in record["details"]["phase"]
    assert store.recent(1) == (record,)


def test_event_store_rejects_unknown_event(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="未知事件"):
        EventStore(tmp_path).emit("UNKNOWN", "failed")


def test_repeated_errors_update_one_record_and_survive_new_store(tmp_path):
    store = EventStore(tmp_path)
    first = store.emit("APP_ERROR", "failed", code="19001", message="same failure", details={"technical": "trace"})
    for _ in range(40):
        last = EventStore(tmp_path).emit("APP_ERROR", "failed", code="19001", message="same failure", details={"technical": "trace"})
    rows = store.recent()
    assert len(rows) == 1
    assert rows[0]["count"] == 41
    assert rows[0]["first_time"] == first.time
    assert last.event_id == first.event_id
    assert len(store.path.read_text("utf-8").splitlines()) == 1


def test_repeat_merge_keeps_distinct_failure_details(tmp_path):
    store = EventStore(tmp_path)
    for operation, technical in (("save", "trace"), ("read", "trace"), ("read", "other trace")):
        store.emit("APP_ERROR", "failed", code="19001", message="same failure", details={"operation": operation, "technical": technical})
    assert len(store.recent()) == 3


def test_concurrent_repeat_writers_preserve_counts(tmp_path):
    def emit(_):
        return EventStore(tmp_path).emit("APP_ERROR", "failed", code="19001", message="same failure")
    with ThreadPoolExecutor(max_workers=5) as executor:
        records = list(executor.map(emit, range(20)))
    rows = EventStore(tmp_path).recent()
    assert len(rows) == 1 and rows[0]["count"] == 20
    assert len({record.event_id for record in records}) == 1


def test_retention_prunes_current_and_rotated_events_on_read_and_write(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    monkeypatch.setattr("wechatrebot_manager.events.time.time", lambda: now.timestamp())
    store = EventStore(tmp_path)
    store.path.parent.mkdir()
    rows = [
        {"time": (now - timedelta(seconds=age)).isoformat(), "event": "APP_START", "status": "ok", "event_id": str(age)}
        for age in (ACTIVITY_RETENTION_SECONDS + 1, ACTIVITY_RETENTION_SECONDS, 30)
    ]
    rotated = store.path.with_suffix(".jsonl.1")
    for path in (store.path, rotated):
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    assert len(store.recent()) == 2
    assert len(rotated.read_text("utf-8").splitlines()) == 2
    now += timedelta(days=3)
    store.emit("APP_START", "ok")
    assert len(store.path.read_text("utf-8").splitlines()) == 1
    assert rotated.read_text("utf-8") == ""


def test_repeat_after_quiet_period_creates_new_record(tmp_path):
    store = EventStore(tmp_path)
    first = store.emit("APP_ERROR", "failed", code="19001", message="same failure")
    row = json.loads(store.path.read_text("utf-8"))
    row["time"] = (datetime.fromisoformat(first.time) - timedelta(minutes=6)).isoformat()
    store.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    next_record = store.emit("APP_ERROR", "failed", code="19001", message="same failure")
    assert next_record.event_id != first.event_id
    assert len(store.recent()) == 2


def test_repeat_counts_expire_individually_at_48_hours(tmp_path, monkeypatch):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    cutoff = int(now.timestamp()) - ACTIVITY_RETENTION_SECONDS
    monkeypatch.setattr("wechatrebot_manager.events.time.time", lambda: now.timestamp())
    store = EventStore(tmp_path)
    store.path.parent.mkdir()
    row = {
        "time": datetime.fromtimestamp(cutoff + 120, timezone.utc).isoformat(),
        "first_time": datetime.fromtimestamp(cutoff - 120, timezone.utc).isoformat(),
        "event": "APP_ERROR", "status": "failed", "code": "19001", "count": 11,
        "message": "repeating", "event_id": "repeat-evidence",
        "occurrences": [[cutoff - 120, 4], [cutoff, 3], [cutoff + 120, 4]],
    }
    store.path.write_text(json.dumps(row) + "\n", encoding="utf-8")
    current = store.recent()[0]
    assert current["count"] == 7
    assert datetime.fromisoformat(current["first_time"]).timestamp() == cutoff
    assert current["event_id"] == "repeat-evidence"
    assert json.loads(store.path.read_text("utf-8"))["occurrences"] == [[cutoff, 3], [cutoff + 120, 4]]
    now += timedelta(seconds=1)
    assert store.recent()[0]["count"] == 4
    current = EventStore(tmp_path).recent()[0]
    assert current["count"] == 4


def test_repeat_rewrite_failure_leaves_original_evidence(tmp_path, monkeypatch):
    store = EventStore(tmp_path)
    store.emit("APP_ERROR", "failed", code="19001", message="same failure")
    original = store.path.read_bytes()
    def denied(*_args):
        raise PermissionError("locked")
    monkeypatch.setattr("wechatrebot_manager.json_io.os.replace", denied)
    with pytest.raises(PermissionError):
        store.emit("APP_ERROR", "failed", code="19001", message="same failure")
    assert store.path.read_bytes() == original
    assert list(store.path.parent.glob("*.tmp")) == []


def test_oversized_log_rotates_before_repeat_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr("wechatrebot_manager.events._MAX_LOG_BYTES", 1024)
    store = EventStore(tmp_path)
    repeated = store.emit("APP_ERROR", "failed", code="19001", message="same failure")
    recent = {"time": repeated.time, "event": "APP_START", "status": "ok",
              "event_id": "recent-evidence", "message": "x" * 1200}
    store.path.write_text(json.dumps(recent) + "\n" + json.dumps(repeated.to_dict()) + "\n", encoding="utf-8")

    store.emit("APP_ERROR", "failed", code="19001", message="same failure")

    rotated = store.path.with_suffix(".jsonl.1")
    assert rotated.exists()
    rows = [json.loads(line) for line in rotated.read_text("utf-8").splitlines()]
    assert rows == [recent, repeated.to_dict()]
    assert len(store.path.read_text("utf-8").splitlines()) == 1


@pytest.mark.parametrize("rotated", [False, True])
def test_pruning_oversized_log_preserves_recent_prefix(tmp_path, monkeypatch, rotated):
    monkeypatch.setattr("wechatrebot_manager.events._MAX_LOG_BYTES", 1024)
    now = datetime.now(timezone.utc)
    store = EventStore(tmp_path)
    path = store.path.with_suffix(".jsonl.1") if rotated else store.path
    path.parent.mkdir()
    recent = {"time": now.isoformat(), "event": "APP_START", "status": "ok",
              "event_id": "recent-evidence", "message": "x" * 1200}
    expired = {"time": (now - timedelta(days=3)).isoformat(), "event": "APP_START", "status": "ok"}
    path.write_text(json.dumps(recent) + "\n" + json.dumps(expired) + "\n", encoding="utf-8")

    store.prune()

    assert json.loads(path.read_text("utf-8")) == recent


def test_streaming_prune_failure_preserves_original_log(tmp_path, monkeypatch):
    store = EventStore(tmp_path)
    recent = store.emit("APP_START", "success").to_dict()
    expired = {**recent, "time": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
    store.path.write_text(json.dumps(recent) + "\n" + json.dumps(expired) + "\n", encoding="utf-8")
    original = store.path.read_bytes()
    read_records = store._file_records
    passes = 0

    def interrupted(path):
        nonlocal passes
        passes += 1
        for index, row in enumerate(read_records(path)):
            if passes == 2 and index == 1:
                raise OSError("read interrupted")
            yield row

    store._last_pruned = 0
    monkeypatch.setattr(store, "_file_records", interrupted)
    with pytest.raises(OSError, match="read interrupted"):
        store.prune()
    assert store.path.read_bytes() == original
    assert list(store.path.parent.glob("*.tmp")) == []
