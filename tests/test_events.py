import json
from pathlib import Path

import pytest

from wechatrebot_manager.events import EventStore


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
