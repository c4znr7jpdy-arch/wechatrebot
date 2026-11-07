"""History mutations share SQLite's import boundary and export literal text."""
import csv
import json
import sqlite3
import threading
from types import SimpleNamespace

import pytest

from wechatrebot_manager.chat_history import ChatHistoryService
from test_chat_history import _bridge_source
from test_app_shell import _api


class _ObservedConnection:
    """Pause at SQL boundaries while still using real SQLite transactions."""

    def __init__(self, connection, before_execute, after_execute=None):
        self.connection = connection
        self.before_execute = before_execute
        self.after_execute = after_execute

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, *args):
        return self.connection.__exit__(*args)

    def execute(self, sql, *args):
        self.before_execute(sql)
        result = self.connection.execute(sql, *args)
        if self.after_execute is not None:
            self.after_execute(sql)
        return result

    def __getattr__(self, name):
        return getattr(self.connection, name)


@pytest.mark.parametrize("operation", ["conversation", "date"])
def test_delayed_import_cannot_restore_deleted_rows_across_service_instances(
    tmp_path, monkeypatch, operation,
):
    source = _bridge_source(tmp_path)
    importer = ChatHistoryService(tmp_path)
    cleaner = ChatHistoryService(tmp_path)
    paused, resume, attempted = (threading.Event() for _ in range(3))
    results, errors = [], []

    def observe(sql):
        worker = threading.current_thread().name
        if worker == "old-importer" and "INSERT OR IGNORE INTO messages" in sql:
            paused.set()
            assert resume.wait(5), "importer was not released"
        if worker == "cleaner" and sql == "BEGIN IMMEDIATE":
            attempted.set()

    def after(sql):
        if threading.current_thread().name == "cleaner" and "DELETE FROM messages" in sql:
            attempted.set()

    for service in (importer, cleaner):
        connect = service._connect
        monkeypatch.setattr(service, "_connect", lambda connect=connect: _ObservedConnection(connect(), observe, after))

    def run(action):
        try:
            action()
        except BaseException as exc:
            errors.append(exc)

    def clean():
        # A second caller first observes the original row, then deletes it.
        assert len(cleaner.messages("456@chatroom", account_id="wxid_bot")) == 1
        count = (cleaner.delete_conversation("456@chatroom", account_id="wxid_bot")
                 if operation == "conversation" else cleaner.delete_before(200, account_id="wxid_bot"))
        results.append(count)

    old = threading.Thread(name="old-importer", target=lambda: run(importer.sync))
    new = threading.Thread(name="cleaner", target=lambda: run(clean))
    old.start()
    try:
        assert paused.wait(5), "importer did not reach its first insert"
        new.start()
        assert attempted.wait(5), "concurrent caller did not reach SQLite"
    finally:
        resume.set()
        old.join(5)
        if new.ident is not None:
            new.join(5)
    assert not old.is_alive() and not new.is_alive()
    assert errors == []
    assert results == [1]
    assert cleaner.messages("456@chatroom", account_id="wxid_bot") == ()
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1


@pytest.mark.parametrize("operation", ["conversation", "date", "csv", "json"])
def test_mutation_and_export_include_source_rows_committed_since_last_read(tmp_path, operation):
    source = _bridge_source(tmp_path)
    service = ChatHistoryService(tmp_path)
    service.sync()
    with sqlite3.connect(source) as connection:
        connection.execute("INSERT INTO messages SELECT 2, account_id, session_id, role, 'pending row', "
                           "sender_id, sender_name, group_id, platform, timestamp, metadata, 'pending:2' "
                           "FROM messages WHERE id=1")
        connection.execute("INSERT INTO messages SELECT 3, 'wxid_other', session_id, role, 'other account', "
                           "sender_id, sender_name, group_id, platform, timestamp, metadata, 'pending:3' "
                           "FROM messages WHERE id=1")
    if operation == "conversation":
        assert service.delete_conversation("456@chatroom", account_id="wxid_bot") == 2
    elif operation == "date":
        assert service.delete_before(200, account_id="wxid_bot") == 2
    else:
        target = service.export(tmp_path / f"history.{operation}", format=operation, account_id="wxid_bot")
        exported = target.read_text(encoding="utf-8-sig")
        assert "pending row" in exported and "other account" not in exported
    if operation in {"conversation", "date"}:
        assert service.messages("456@chatroom", account_id="wxid_bot") == ()
    assert len(service.messages("456@chatroom", account_id="wxid_other")) == 1


@pytest.mark.parametrize("operation", ["conversation", "date"])
def test_access_is_revalidated_after_waiting_for_sqlite_writer(tmp_path, monkeypatch, operation):
    _bridge_source(tmp_path)
    service = ChatHistoryService(tmp_path)
    service.sync()
    attempted = threading.Event()
    allowed = [True]
    errors = []
    connect = service._connect
    monkeypatch.setattr(service, "_connect", lambda: _ObservedConnection(
        connect(), lambda sql: attempted.set() if sql == "BEGIN IMMEDIATE" else None,
    ))

    def validate():
        if not allowed[0]:
            raise ValueError("account changed")

    def mutate():
        try:
            if operation == "conversation":
                service.delete_conversation("456@chatroom", account_id="wxid_bot", validate_access=validate)
            else:
                service.delete_before(200, account_id="wxid_bot", validate_access=validate)
        except ValueError as exc:
            errors.append(str(exc))

    blocker = connect()
    # Isolate the lock wait after sync from the importer's separate transaction.
    monkeypatch.setattr(service, "sync", lambda **_kwargs: 0)
    blocker.execute("BEGIN IMMEDIATE")
    thread = threading.Thread(target=mutate)
    thread.start()
    try:
        assert attempted.wait(5)
        allowed[0] = False
    finally:
        blocker.rollback()
        blocker.close()
        thread.join(5)
    assert not thread.is_alive()
    assert errors == ["account changed"]
    assert len(service.messages("456@chatroom", account_id="wxid_bot")) == 1


@pytest.mark.parametrize("value,dangerous", [
    ("=1+1", True), ("+1+1", True), ("-1+1", True), ("@SUM(1,1)", True),
    ("\t=1+1", True), ("\r=1+1", True), ("\n=1+1", True), ("  =1+1", True),
    (" \t@SUM(1,1)", True), ("\tordinary", True),
    ("ordinary", False), ("中文，emoji🙂", False), ("", False),
    ("already 'quoted'", False), ("'=1+1", False), ("line\n=1+1", False),
])
def test_csv_user_fields_are_literal_and_json_keeps_original_values(tmp_path, value, dangerous):
    source = _bridge_source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("UPDATE messages SET content=?, sender_name=?, metadata=?", (
            value, value, json.dumps({"group_name": value, "wechat_name": value,
                                     "group_nickname": value, "wechat_id": value}),
        ))
    service = ChatHistoryService(tmp_path)
    service.sync()
    # Keep this check about the exporter, including values normalized at import.
    with service._connect() as connection:
        connection.execute("UPDATE messages SET conversation_id=?, group_name=?, wechat_id=?, "
                           "wechat_name=?, group_nickname=?, content=?, sender_id=?", (value,) * 7)
    target = service.export(tmp_path / "history.csv", account_id="wxid_bot")
    with target.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.reader(handle))
    expected = "'" + value if dangerous else value
    assert [rows[1][index] for index in (1, 2, 4, 5, 6, 7)] == [expected] * 6
    target = service.export(tmp_path / "history.json", format="json", account_id="wxid_bot")
    item = json.loads(target.read_text(encoding="utf-8"))[0]
    assert [item[key] for key in ("conversation_id", "group_name", "wechat_id", "wechat_name",
                                 "group_nickname", "content")] == [value] * 6


def test_paged_api_keeps_equal_timestamps_account_scoped_after_sync(tmp_path):
    source = _bridge_source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute("DELETE FROM messages")
        connection.executemany("INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (
            (index, "wxid_bot" if index % 5 else "wxid_other", "hook_bridge:GroupMessage:456@chatroom",
             "user", str(index), "wxid_member", "member", "456@chatroom", "hook_bridge",
             1000.0, "{}", f"page:{index}") for index in range(1, 67)
        ))
    api = _api(tmp_path)
    api._history = ChatHistoryService(tmp_path)
    api._history_data_identity = lambda: "wxid_bot"
    api._groups = SimpleNamespace(members=lambda _group: {"account_id": "wxid_bot", "items": []})
    before = None
    contents = []
    while True:
        filters = {"paged": True, "page_size": 7, "account_id": "wxid_bot"}
        if before is not None:
            filters["before"] = before
        result = api.get_messages("456@chatroom", filters)
        assert result["ok"] is True
        page = result["data"]
        assert page["account_id"] == "wxid_bot"
        contents.extend(int(item["content"]) for item in page["items"])
        if not page["has_more"]:
            break
        oldest = page["items"][0]
        before = {"id": oldest["id"], "timestamp": oldest["timestamp"]}
    assert len(contents) == len(set(contents))
    assert sorted(contents) == [index for index in range(1, 67) if index % 5]


@pytest.mark.parametrize("damage", ["locked", "corrupt", "schema"])
@pytest.mark.parametrize("operation", ["conversation", "date", "export"])
def test_failed_source_sync_preserves_index_and_export_then_allows_retry(
    tmp_path, monkeypatch, damage, operation,
):
    source = _bridge_source(tmp_path)
    service = ChatHistoryService(tmp_path)
    service.sync()
    target = tmp_path / "existing.csv"
    target.write_text("existing file", encoding="utf-8")
    original_source = source.read_bytes()
    blocker = None
    if damage == "locked":
        blocker = sqlite3.connect(source)
        blocker.execute("BEGIN EXCLUSIVE")
        connect = sqlite3.connect

        def quick_busy(*args, **kwargs):
            connection = connect(*args, **kwargs)
            if kwargs.get("uri"):
                connection.execute("PRAGMA busy_timeout=40")
            return connection

        monkeypatch.setattr(sqlite3, "connect", quick_busy)
    elif damage == "corrupt":
        source.write_bytes(b"not a SQLite database")
    else:
        with sqlite3.connect(source) as connection:
            connection.execute("ALTER TABLE messages RENAME TO unavailable_messages")

    def action(path=target):
        if operation == "conversation":
            return service.delete_conversation("456@chatroom", account_id="wxid_bot")
        if operation == "date":
            return service.delete_before(200, account_id="wxid_bot")
        return service.export(path, account_id="wxid_bot")

    try:
        # Ordinary reads retain their existing best-effort index fallback.
        assert len(service.messages("456@chatroom", account_id="wxid_bot")) == 1
        with pytest.raises(sqlite3.Error):
            action()
        assert target.read_text(encoding="utf-8") == "existing file"
        if operation == "export":
            missing = tmp_path / "not-created" / "history.csv"
            with pytest.raises(sqlite3.Error):
                action(missing)
            assert not missing.parent.exists()
        with service._connect() as connection:
            assert connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 1
    finally:
        if blocker is not None:
            blocker.rollback()
            blocker.close()
        else:
            source.write_bytes(original_source)
    result = action()
    if operation == "export":
        assert result == target
        assert target.read_text(encoding="utf-8-sig") != "existing file"
    else:
        assert result == 1
        assert service.messages("456@chatroom", account_id="wxid_bot") == ()


def test_new_install_without_source_has_empty_mutation_and_export_results(tmp_path):
    service = ChatHistoryService(tmp_path)
    assert service.sync(strict=True) == 0
    assert service.delete_conversation("friend", account_id="wxid_bot") == 0
    assert service.delete_before(200, account_id="wxid_bot") == 0
    target = service.export(tmp_path / "empty.json", format="json", account_id="wxid_bot")
    assert json.loads(target.read_text(encoding="utf-8")) == []
