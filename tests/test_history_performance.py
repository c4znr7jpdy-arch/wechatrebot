"""Deterministic read/import budgets, using temporary history only."""
from contextlib import contextmanager
import sqlite3

from wechatrebot_manager.chat_history import ChatHistoryService, HISTORY_IMPORT_BATCH_SIZE
from test_chat_history import _bridge_source


def _many_messages(root, count):
    source = _bridge_source(root)
    with sqlite3.connect(source) as connection:
        connection.executemany("INSERT INTO messages SELECT ?, account_id, session_id, role, ?, "
            "sender_id, sender_name, group_id, platform, ?, metadata, ? FROM messages WHERE id=1",
            ((i, f"message {i}", i, f"many:{i}") for i in range(2, count + 1)))
    return source


def test_reads_import_one_bounded_batch_and_strict_export_drains_snapshot(tmp_path):
    _many_messages(tmp_path, HISTORY_IMPORT_BATCH_SIZE * 2 + 7)
    service = ChatHistoryService(tmp_path)
    first = service.conversations(account_id="wxid_bot")
    assert first["summary"]["messages"] == HISTORY_IMPORT_BATCH_SIZE
    assert service.sync_pending
    output = service.export(tmp_path / "all.json", format="json", account_id="wxid_bot")
    import json
    assert len(json.loads(output.read_text(encoding="utf-8"))) == HISTORY_IMPORT_BATCH_SIZE * 2 + 7
    assert not service.sync_pending
    assert service.delete_conversation("456@chatroom", account_id="wxid_bot") == HISTORY_IMPORT_BATCH_SIZE * 2 + 7
    assert service.conversations(account_id="wxid_bot")["summary"]["messages"] == 0


def test_cached_query_skips_aggregation_and_invalidates_on_external_commit(tmp_path, monkeypatch):
    source = _many_messages(tmp_path, 20)
    service = ChatHistoryService(tmp_path)
    service.sync(strict=True)
    queries = []
    connect = service._connect

    @contextmanager
    def observed():
        with connect() as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(service, "_connect", observed)
    first = service.conversations(account_id="wxid_bot")
    first["items"][0]["display_name"] = "client mutation"
    queries.clear()
    again = service.conversations(account_id="wxid_bot")
    assert again["items"][0]["display_name"] != "client mutation"
    assert not any("GROUP BY" in query for query in queries)
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO messages SELECT 21, account_id, session_id, role, 'new', "
                           "sender_id, sender_name, group_id, platform, 21, metadata, 'new' FROM messages WHERE id=1")
    assert service.conversations(account_id="wxid_bot")["summary"]["messages"] == 21
    # A separate writer can retain its WAL connection across multiple commits.
    external = sqlite3.connect(service.path)
    try:
        external.execute("DELETE FROM messages WHERE source_message_id=21")
        external.commit()
        assert service.conversations(account_id="wxid_bot")["summary"]["messages"] == 20
        external.execute("UPDATE messages SET content='changed' WHERE source_message_id=20")
        external.commit()
        assert service.messages("456@chatroom", account_id="wxid_bot", search="changed")[0]["content"] == "changed"
    finally:
        external.close()
    assert service.conversations(account_id="different")["summary"]["messages"] == 0


def test_cache_is_bounded_and_concurrent_read_does_not_wait_for_import(tmp_path):
    service = ChatHistoryService(tmp_path)
    for i in range(25):
        service.conversations(account_id=str(i))
    assert len(service._query_cache._entries) <= 16
    with service._sync_lock:
        assert service.sync() == 0


def test_account_backfill_is_bounded_and_summary_tracks_account_moves(tmp_path):
    _many_messages(tmp_path, HISTORY_IMPORT_BATCH_SIZE + 8)
    service = ChatHistoryService(tmp_path)
    service.sync(strict=True)
    with service._connect() as connection:
        connection.execute("UPDATE messages SET account_id=''")
        connection.execute("UPDATE import_sources SET account_backfill_complete=0, account_backfill_cursor=0")
    service.sync()
    assert service.sync_pending
    with service._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages WHERE account_id='wxid_bot'").fetchone()[0] == HISTORY_IMPORT_BATCH_SIZE
    service.sync(strict=True)
    assert not service.sync_pending
    assert service.conversations(account_id="wxid_bot")["summary"]["messages"] == HISTORY_IMPORT_BATCH_SIZE + 8
    assert service.conversations(include_unassigned=True)["summary"]["messages"] == 0


def test_summary_updates_incrementally_and_rebuilds_after_delete(tmp_path, monkeypatch):
    source = _many_messages(tmp_path, 20)
    service = ChatHistoryService(tmp_path)
    service.sync(strict=True)
    queries = []
    connect = service._connect

    @contextmanager
    def observed():
        with connect() as connection:
            connection.set_trace_callback(queries.append)
            yield connection

    monkeypatch.setattr(service, "_connect", observed)
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO messages SELECT 21, account_id, session_id, role, 'new', "
                           "sender_id, sender_name, group_id, platform, 21, metadata, 'new' FROM messages WHERE id=1")
    assert service.conversations(account_id="wxid_bot")["summary"]["messages"] == 21
    assert not any("FROM messages" in query and "GROUP BY" in query for query in queries)
    with connect() as connection:
        connection.execute("DELETE FROM messages WHERE source_message_id=1")
    result = service.conversations(account_id="wxid_bot")
    assert result["summary"]["messages"] == 20
    assert result["items"][0]["last_timestamp"] == 21


def test_old_database_adds_summary_without_changing_history(tmp_path):
    _many_messages(tmp_path, 20)
    service = ChatHistoryService(tmp_path)
    service.sync(strict=True)
    expected = service.conversations(account_id="wxid_bot")
    with service._connect() as connection:
        for name in ("insert", "update", "delete"):
            connection.execute(f"DROP TRIGGER history_summary_{name}")
        for table in ("history_summary_state", "history_summary_dirty", "history_conversation_summary"):
            connection.execute(f"DROP TABLE {table}")
    migrated = ChatHistoryService(tmp_path)
    assert migrated.conversations(account_id="wxid_bot") == expected
