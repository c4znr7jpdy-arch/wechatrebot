"""Offline synthetic history benchmark; never opens a customer installation."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import json
from pathlib import Path
import platform
import statistics
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from wechatrebot_manager.chat_history import ChatHistoryService


def measure(rows: int = 300000, conversations: int = 250, samples: int = 3) -> dict:
    with tempfile.TemporaryDirectory(prefix="history-benchmark-") as directory:
        service = ChatHistoryService(directory)
        with service._connect() as connection:
            connection.executemany("""INSERT INTO messages (
                source_key,source_message_id,account_id,session_id,conversation_id,conversation_type,
                role,sender_id,sender_name,group_id,content,timestamp
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""", (
                ("synthetic", i, "account", f"g{i%conversations}@chatroom", f"g{i%conversations}@chatroom",
                 "group", "user", f"sender-{i%80}", f"name-{i%80}", f"g{i%conversations}@chatroom",
                 "synthetic keyword " + "x" * 150, 1700000000 + i) for i in range(rows)
            ))
        queries = []
        connect = service._connect

        @contextmanager
        def observed():
            with connect() as connection:
                connection.set_trace_callback(queries.append)
                yield connection

        service._connect = observed
        started = time.perf_counter()
        result = service.conversations(account_id="account", limit=None)
        cold = (time.perf_counter() - started) * 1000
        timing = []
        for _ in range(samples):
            started = time.perf_counter()
            service.conversations(account_id="account", limit=None)
            timing.append((time.perf_counter() - started) * 1000)
        scans = sum("FROM messages" in query and "GROUP BY" in query for query in queries)
        if result["summary"]["messages"] != rows or scans:
            raise AssertionError("Unfiltered directory must use the maintained summary without message aggregation")
        return {"scope": "synthetic_offline_history", "python": platform.python_version(),
                "rows": rows, "conversations": conversations, "samples": samples,
                "first_ms": round(cold, 3), "cached_median_ms": round(statistics.median(timing), 3),
                "message_aggregations": scans, "summary_messages": result["summary"]["messages"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=300000)
    parser.add_argument("--conversations", type=int, default=250)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not 1 <= args.rows <= 1000000 or not 1 <= args.conversations <= 10000 or not 1 <= args.samples <= 30:
        parser.error("rows 1..1000000, conversations 1..10000, samples 1..30 required")
    report = json.dumps(measure(args.rows, args.conversations, args.samples), indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(report, encoding="utf-8")
    print(report)
