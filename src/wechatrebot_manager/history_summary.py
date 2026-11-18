"""Rebuildable conversation summaries; messages remain the source of truth."""

_AGGREGATE = """
    SELECT m.account_id, m.conversation_id, m.conversation_type, COUNT(*), MAX(m.timestamp),
           MAX(CASE WHEN m.role='user' THEN m.sender_name ELSE '' END), MAX(m.group_name),
           MAX(CASE WHEN m.conversation_type='private' AND m.role='user' THEN m.avatar ELSE '' END)
    FROM {source}
    GROUP BY m.account_id, m.conversation_id, m.conversation_type
"""
_KEY = "account_id, conversation_id, conversation_type"


def initialize(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS history_conversation_summary (
            account_id TEXT NOT NULL, conversation_id TEXT NOT NULL, conversation_type TEXT NOT NULL,
            message_count INTEGER NOT NULL, last_timestamp REAL NOT NULL,
            sender_name TEXT NOT NULL, group_name TEXT NOT NULL, avatar TEXT NOT NULL,
            PRIMARY KEY(account_id, conversation_id, conversation_type)
        );
        CREATE TABLE IF NOT EXISTS history_summary_dirty (
            account_id TEXT NOT NULL, conversation_id TEXT NOT NULL, conversation_type TEXT NOT NULL,
            PRIMARY KEY(account_id, conversation_id, conversation_type)
        );
        CREATE TABLE IF NOT EXISTS history_summary_state (id INTEGER PRIMARY KEY CHECK(id=1));
        CREATE TRIGGER IF NOT EXISTS history_summary_insert AFTER INSERT ON messages BEGIN
            INSERT INTO history_conversation_summary VALUES (
                NEW.account_id, NEW.conversation_id, NEW.conversation_type, 1, NEW.timestamp,
                CASE WHEN NEW.role='user' THEN NEW.sender_name ELSE '' END, NEW.group_name,
                CASE WHEN NEW.conversation_type='private' AND NEW.role='user' THEN NEW.avatar ELSE '' END
            ) ON CONFLICT(account_id, conversation_id, conversation_type) DO UPDATE SET
                message_count=message_count+1,
                last_timestamp=MAX(last_timestamp, excluded.last_timestamp),
                sender_name=MAX(sender_name, excluded.sender_name),
                group_name=MAX(group_name, excluded.group_name), avatar=MAX(avatar, excluded.avatar);
        END;
        CREATE TRIGGER IF NOT EXISTS history_summary_delete AFTER DELETE ON messages BEGIN
            INSERT OR IGNORE INTO history_summary_dirty VALUES (OLD.account_id, OLD.conversation_id, OLD.conversation_type);
        END;
        CREATE TRIGGER IF NOT EXISTS history_summary_update AFTER UPDATE ON messages BEGIN
            INSERT OR IGNORE INTO history_summary_dirty VALUES (OLD.account_id, OLD.conversation_id, OLD.conversation_type);
            INSERT OR IGNORE INTO history_summary_dirty VALUES (NEW.account_id, NEW.conversation_id, NEW.conversation_type);
        END;
    """)
    # A transaction marker makes interrupted or concurrent initial rebuilds safe.
    connection.execute("BEGIN IMMEDIATE")
    if not connection.execute("SELECT id FROM history_summary_state").fetchone():
        connection.execute("DELETE FROM history_conversation_summary")
        connection.execute("INSERT INTO history_conversation_summary " + _AGGREGATE.format(source="messages m"))
        connection.execute("DELETE FROM history_summary_dirty")
        connection.execute("INSERT INTO history_summary_state VALUES (1)")
    connection.commit()


def read(connection, account, kind, limit):
    connection.execute("BEGIN")
    if connection.execute("SELECT 1 FROM history_summary_dirty LIMIT 1").fetchone():
        connection.rollback()
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(f"DELETE FROM history_conversation_summary WHERE ({_KEY}) IN "
                           f"(SELECT {_KEY} FROM history_summary_dirty)")
        connection.execute("INSERT INTO history_conversation_summary " + _AGGREGATE.format(
            source=f"history_summary_dirty d CROSS JOIN messages m USING ({_KEY})"))
        connection.execute("DELETE FROM history_summary_dirty")
        # Keep the same transaction for both page and totals; a concurrent
        # deletion cannot make the summary stale between rebuilding and reading.
    where, params = "account_id = ?", [account]
    if kind in {"private", "group"}:
        where += " AND conversation_type = ?"
        params.append(kind)
    rows = connection.execute(
        f"SELECT * FROM history_conversation_summary WHERE {where} "
        "ORDER BY last_timestamp DESC, conversation_type, conversation_id LIMIT ?", (*params, limit),
    ).fetchall()
    totals = connection.execute(f"""
        SELECT COALESCE(SUM(message_count),0) AS messages, COUNT(DISTINCT conversation_id) AS conversations,
               SUM(CASE WHEN conversation_type='private' THEN message_count ELSE 0 END) AS private_messages,
               SUM(CASE WHEN conversation_type='group' THEN message_count ELSE 0 END) AS group_messages
        FROM history_conversation_summary WHERE {where}
    """, params).fetchone()
    return rows, totals
