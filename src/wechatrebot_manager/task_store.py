"""Shared, standard-library task model for the manager, runtime and delivery gate.

This module is copied unchanged into both runtime components during assembly.
Only explicitly saved tasks exist; reading an empty store never seeds jobs.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
import uuid


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
ACTION_NAMES = {"weather": "天气预报", "news": "热点资讯", "oilprice": "油价提醒", "kfc": "星期四文案", "text": "文字提醒"}
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_TARGET = re.compile(r"^[A-Za-z0-9_.@-]{1,128}$")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_FINAL_DELIVERIES = {"submitted", "failed", "unknown", "skipped"}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime:
    value = value or _now()
    if value.tzinfo is None:
        raise ValueError("执行时间缺少时区。")
    return value.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def validate_account(value: Any) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise ValueError("请先连接微信并确认机器人账号。")
    return value


def normalize_schedule(raw: Any, *, draft: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict) or raw.get("kind") not in {"once", "daily", "weekly"}:
        raise ValueError("请选择仅一次、每天或每周执行。")
    clock = raw.get("time")
    if not isinstance(clock, str) or (not _CLOCK.fullmatch(clock) and not (draft and not clock)):
        raise ValueError("请输入有效的执行时间，格式为 HH:MM。")
    if raw.get("timezone", "Asia/Shanghai") != "Asia/Shanghai":
        raise ValueError("当前定时任务使用北京时间。")
    result: dict[str, Any] = {"kind": raw["kind"], "time": clock, "timezone": "Asia/Shanghai"}
    if raw["kind"] == "once":
        if draft and not raw.get("date"):
            result["date"] = ""
            return result
        try:
            value = raw["date"]
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError
            date.fromisoformat(value)
        except (KeyError, TypeError, ValueError):
            raise ValueError("请选择有效的执行日期。") from None
        result["date"] = value
    if raw["kind"] == "weekly":
        days = raw.get("weekdays")
        if not isinstance(days, list) or (not days and not draft) or any(type(day) is not int or not 0 <= day <= 6 for day in days):
            raise ValueError("请至少选择每周的一天。")
        result["weekdays"] = sorted(set(days))
    return result


def next_occurrences(schedule: dict[str, Any], now: datetime | None = None, count: int = 3) -> list[str]:
    schedule = normalize_schedule(schedule)
    current = _aware(now).astimezone(BEIJING)
    clock = time.fromisoformat(schedule["time"])
    result: list[str] = []
    if schedule["kind"] == "once":
        occurrence = datetime.combine(date.fromisoformat(schedule["date"]), clock, BEIJING)
        return [occurrence.isoformat()] if occurrence > current else []
    for offset in range(max(1, min(count, 20)) * 7 + 1):
        day = current.date() + timedelta(days=offset)
        if schedule["kind"] == "weekly" and day.weekday() not in schedule["weekdays"]:
            continue
        occurrence = datetime.combine(day, clock, BEIJING)
        if occurrence > current:
            result.append(occurrence.isoformat())
            if len(result) >= count:
                break
    return result


def schedule_text(schedule: dict[str, Any]) -> str:
    schedule = normalize_schedule(schedule)
    if schedule["kind"] == "once":
        prefix = schedule["date"]
    elif schedule["kind"] == "daily":
        prefix = "每天"
    else:
        prefix = "、".join(("周一", "周二", "周三", "周四", "周五", "周六", "周日")[day] for day in schedule["weekdays"])
    return f"{prefix} {schedule['time']}"


def normalize_arguments(action: str, arguments: Any, *, draft: bool = False) -> dict[str, Any]:
    if action not in ACTION_NAMES or not isinstance(arguments, dict):
        raise ValueError("请选择支持定时执行的内容。")
    if action == "weather":
        city = str(arguments.get("city") or "").strip()
        if (not city and not draft) or len(city) > 80 or any(ord(char) < 32 for char in city):
            raise ValueError("请填写有效的天气城市。")
        day = arguments.get("day", "today")
        output = arguments.get("output", "image")
        if day not in {"today", "tomorrow"} or output != "image":
            raise ValueError("请选择今天或明天；天气统一使用图片卡片。")
        return {"city": city, "day": day, "output": output}
    if action == "news":
        count = arguments.get("count", 10)
        if type(count) is not int or not 1 <= count <= 30:
            raise ValueError("资讯条数应为 1 到 30。")
        return {"count": count}
    if action == "oilprice":
        province = str(arguments.get("province") or "").strip()
        if (not province and not draft) or len(province) > 40 or any(ord(char) < 32 for char in province):
            raise ValueError("请填写油价查询省份。")
        return {"province": province}
    if action == "text":
        text = arguments.get("text")
        text = "" if text is None and draft else text
        if not isinstance(text, str) or (not text.strip() and not draft) or len(text) > 4000 or "\x00" in text:
            raise ValueError("请填写 1 到 4000 字的提醒内容。")
        return {"text": text}
    return {}


def normalize_task(raw: Any, *, now: datetime | None = None, _allow_test: bool = False) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ValueError("任务配置格式不正确。")
    account = validate_account(raw.get("account_id"))
    task_id = raw.get("id") or str(uuid.uuid4())
    if not isinstance(task_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", task_id):
        raise ValueError("任务标识无效。")
    if task_id.startswith("test_") and not _allow_test:
        raise ValueError("任务标识使用了系统保留前缀。")
    action = raw.get("action")
    draft = raw.get("draft", False)
    arguments = normalize_arguments(action, raw.get("arguments", {}), draft=draft is True)
    name = str(raw.get("name") or ACTION_NAMES[action]).strip()
    if not name or len(name) > 100 or any(ord(char) < 32 for char in name):
        raise ValueError("任务名称应为 1 到 100 个字符。")
    draft = raw.get("draft", False)
    enabled = raw.get("enabled", False)
    if not isinstance(draft, bool) or not isinstance(enabled, bool):
        raise ValueError("任务启用状态格式不正确。")
    enabled = enabled and not draft
    targets = raw.get("targets", [])
    if not isinstance(targets, list) or len(targets) > 100 or (not targets and not draft):
        raise ValueError("请选择 1 到 100 个发送对象。")
    normalized_targets = []
    seen = set()
    for target in targets:
        if not isinstance(target, dict):
            raise ValueError("发送对象格式不正确。")
        kind, identity = target.get("type"), target.get("id")
        if kind not in {"group", "private"} or not isinstance(identity, str) or not _TARGET.fullmatch(identity):
            raise ValueError("请从当前账号的群聊或联系人列表选择发送对象。")
        if (kind == "group") != identity.endswith("@chatroom"):
            raise ValueError("发送对象与聊天类型不一致。")
        key = (kind, identity)
        if key not in seen:
            # Names are resolved from the current account's directory, not stored
            # as a second, long-lived address book.
            normalized_targets.append({"type": kind, "id": identity})
            seen.add(key)
    schedule = normalize_schedule(raw.get("schedule"), draft=draft)
    if enabled and not next_occurrences(schedule, now):
        raise ValueError("执行时间已过去，请选择未来的时间。")
    return {"id": task_id, "account_id": account, "name": name, "action": action,
            "arguments": arguments, "targets": normalized_targets, "schedule": schedule,
            "enabled": enabled, "draft": draft}


def parse_command(command: str) -> dict[str, Any]:
    if not isinstance(command, str) or len(command) > 500:
        raise ValueError("请输入一个支持的查询命令。")
    text = command.strip().lstrip("/\\").strip()
    if match := re.fullmatch(r"(.+?)天气(?:\s*(今天|明天))?", text):
        return {"action": "weather", "arguments": {"city": match[1].strip(), "day": "tomorrow" if match[2] == "明天" else "today", "output": "image"}}
    if match := re.fullmatch(r"天气\s+(.+?)(?:\s+(今天|明天))?", text):
        return {"action": "weather", "arguments": {"city": match[1].strip(), "day": "tomorrow" if match[2] == "明天" else "today", "output": "image"}}
    if match := re.fullmatch(r"(?:新闻|资讯|热点)(?:\s+(\d{1,2}))?", text):
        return {"action": "news", "arguments": normalize_arguments("news", {"count": int(match[1] or 10)})}
    if match := re.fullmatch(r"(?:(.+?)油价|油价\s+(.+))", text):
        return {"action": "oilprice", "arguments": {"province": (match[1] or match[2]).strip()}}
    if text.casefold() in {"kfc", "疯狂星期四", "星期四文案"}:
        return {"action": "kfc", "arguments": {}}
    if match := re.fullmatch(r"(?:提醒|文字提醒)\s+(.+)", text, re.DOTALL):
        return {"action": "text", "arguments": normalize_arguments("text", {"text": match[1]})}
    raise ValueError("当前支持天气、新闻、油价和星期四文案命令；也可以直接选择内容。")


class TaskStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser().resolve()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS scheduled_tasks (
                id TEXT PRIMARY KEY, account_id TEXT NOT NULL, revision INTEGER NOT NULL,
                payload TEXT NOT NULL, enabled INTEGER NOT NULL, deleted INTEGER NOT NULL DEFAULT 0,
                next_due TEXT, status TEXT NOT NULL, last_result TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS scheduled_tasks_due ON scheduled_tasks(account_id, enabled, next_due);
            CREATE TABLE IF NOT EXISTS scheduled_task_runs (
                id TEXT PRIMARY KEY, task_id TEXT NOT NULL, account_id TEXT NOT NULL,
                revision INTEGER NOT NULL, scheduled_at TEXT NOT NULL, task TEXT NOT NULL,
                status TEXT NOT NULL, created_at TEXT NOT NULL, finished_at TEXT,
                UNIQUE(task_id, scheduled_at)
            );
            CREATE TABLE IF NOT EXISTS scheduled_task_deliveries (
                run_id TEXT NOT NULL REFERENCES scheduled_task_runs(id), target_type TEXT NOT NULL,
                target_id TEXT NOT NULL, status TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                PRIMARY KEY(run_id, target_type, target_id)
            );
        """)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _public(row: sqlite3.Row) -> dict[str, Any]:
        task = json.loads(row["payload"])
        try:
            text = schedule_text(task["schedule"])
        except ValueError:
            text = "待设置时间"
        return {**task, "revision": row["revision"], "enabled": bool(row["enabled"]),
                "status": row["status"], "next_run_time": row["next_due"] or "",
                "schedule_text": text, "last_result": row["last_result"]}

    def list(self, account_id: str) -> list[dict[str, Any]]:
        validate_account(account_id)
        if not self.path.is_file():
            return []
        with self._connect() as connection:
            return [self._public(row) for row in connection.execute(
                r"SELECT * FROM scheduled_tasks WHERE account_id=? AND deleted=0 AND id NOT LIKE 'test\_%' ESCAPE '\' ORDER BY updated_at DESC,id", (account_id,))]

    def get(self, account_id: str, task_id: str) -> dict[str, Any]:
        validate_account(account_id)
        if not self.path.is_file():
            raise ValueError("没有找到这个任务，可能已被删除。")
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE id=? AND account_id=? AND deleted=0", (task_id, account_id)).fetchone()
        if row is None:
            raise ValueError("没有找到这个任务，可能已被删除。")
        return self._public(row)

    def save(self, payload: dict[str, Any], *, now: datetime | None = None, _allow_test: bool = False) -> dict[str, Any]:
        current = _aware(now)
        task = normalize_task(payload, now=current, _allow_test=_allow_test)
        serialized = _json(task)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task["id"],)).fetchone()
            expected = payload.get("revision", 0)
            if type(expected) is not int or expected < 0:
                raise ValueError("任务版本无效，请刷新后重试。")
            if previous:
                if previous["account_id"] != task["account_id"] or previous["deleted"]:
                    raise ValueError("任务已删除或不属于当前机器人账号。")
                if expected == 0 and previous["payload"] == serialized:
                    return self._public(previous)  # repeated create acknowledgement
                if expected != previous["revision"]:
                    raise ValueError("任务已在其他窗口修改，请重新读取后保存。")
                if previous["payload"] == serialized:
                    return self._public(previous)
            elif expected:
                raise ValueError("任务已不存在，请重新读取列表。")
            revision = previous["revision"] + 1 if previous else 1
            future = next_occurrences(task["schedule"], current, 1) if task["enabled"] else []
            next_due = datetime.fromisoformat(future[0]).astimezone(timezone.utc).isoformat() if future else None
            status = "draft" if task["draft"] else "enabled" if task["enabled"] else "paused"
            connection.execute("""INSERT INTO scheduled_tasks(id,account_id,revision,payload,enabled,next_due,status,updated_at)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,
                payload=excluded.payload,enabled=excluded.enabled,next_due=excluded.next_due,status=excluded.status,updated_at=excluded.updated_at""",
                (task["id"], task["account_id"], revision, serialized, int(task["enabled"]), next_due, status, current.isoformat()))
            self._cancel_pending(connection, task["id"], "任务配置已改变，本次尚未发送的对象已取消。")
        return self.get(task["account_id"], task["id"])

    @staticmethod
    def _cancel_pending(connection: sqlite3.Connection, task_id: str, message: str) -> None:
        connection.execute("""UPDATE scheduled_task_deliveries SET status='skipped',message=?
            WHERE status='pending' AND run_id IN (SELECT id FROM scheduled_task_runs WHERE task_id=? AND status='running')""", (message, task_id))

    def set_enabled(self, account_id: str, task_id: str, enabled: bool, revision: int) -> dict[str, Any]:
        task = self.get(account_id, task_id)
        task.update(enabled=enabled, draft=False, revision=revision)
        return self.save(task)

    def delete(self, account_id: str, task_id: str, revision: int) -> None:
        validate_account(account_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE id=? AND account_id=?", (task_id, account_id)).fetchone()
            if not row:
                raise ValueError("没有找到这个任务。")
            if row["deleted"]:
                return
            if type(revision) is not int or revision != row["revision"]:
                raise ValueError("任务已在其他窗口修改，请刷新后重试。")
            connection.execute("UPDATE scheduled_tasks SET deleted=1,enabled=0,next_due=NULL,revision=revision+1,status='deleted',updated_at=? WHERE id=?", (_now().isoformat(), task_id))
            self._cancel_pending(connection, task_id, "任务已删除。")

    @staticmethod
    def _run(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        task = json.loads(row["task"])
        task["revision"] = row["revision"]
        deliveries = [{"type": item["target_type"], "id": item["target_id"], "status": item["status"], "message": item["message"]}
                      for item in connection.execute("SELECT * FROM scheduled_task_deliveries WHERE run_id=? ORDER BY target_type,target_id", (row["id"],))]
        return {"id": row["id"], "task_id": row["task_id"], "account_id": row["account_id"], "revision": row["revision"],
                "scheduled_at": row["scheduled_at"], "status": row["status"], "task": task,
                "created_at": row["created_at"], "finished_at": row["finished_at"], "deliveries": deliveries}

    def runs(self, account_id: str, task_id: str) -> list[dict[str, Any]]:
        self.get(account_id, task_id)
        with self._connect() as connection:
            return [self._run(connection, row) for row in connection.execute(
                "SELECT * FROM scheduled_task_runs WHERE account_id=? AND task_id=? ORDER BY scheduled_at DESC LIMIT 50", (account_id, task_id))]

    def claim_due(self, now: datetime, account_id: str) -> list[dict[str, Any]]:
        current = _aware(now)
        validate_account(account_id)
        if not self.path.is_file():
            return []
        claimed = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute("SELECT * FROM scheduled_tasks WHERE account_id=? AND deleted=0 AND enabled=1 AND next_due<=? ORDER BY next_due LIMIT 100", (account_id, current.isoformat())).fetchall()
            for row in rows:
                task = json.loads(row["payload"])
                running = connection.execute("SELECT 1 FROM scheduled_task_runs WHERE task_id=? AND status='running'", (row["id"],)).fetchone()
                late = (current - datetime.fromisoformat(row["next_due"])).total_seconds() > 90
                skipped = late or bool(running)
                run_id = str(uuid.uuid4())
                cursor = connection.execute("""INSERT OR IGNORE INTO scheduled_task_runs
                    (id,task_id,account_id,revision,scheduled_at,task,status,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?)""",
                    (run_id, row["id"], account_id, row["revision"], row["next_due"], row["payload"], "skipped" if skipped else "running", current.isoformat(), current.isoformat() if skipped else None))
                if cursor.rowcount:
                    reason = "错过执行时间，已跳过。" if late else "上次任务仍在执行，已跳过。" if running else ""
                    for target in task["targets"]:
                        connection.execute("INSERT INTO scheduled_task_deliveries(run_id,target_type,target_id,status,message) VALUES(?,?,?,?,?)",
                                           (run_id, target["type"], target["id"], "skipped" if skipped else "pending", reason))
                    if not skipped:
                        claimed.append(self._run(connection, connection.execute("SELECT * FROM scheduled_task_runs WHERE id=?", (run_id,)).fetchone()))
                future = next_occurrences(task["schedule"], current, 1)
                due = datetime.fromisoformat(future[0]).astimezone(timezone.utc).isoformat() if future else None
                task_status = (
                    "running" if cursor.rowcount and not skipped
                    else "enabled" if future else "completed"
                )
                connection.execute("UPDATE scheduled_tasks SET next_due=?,status=?,last_result=CASE WHEN ? THEN ? ELSE last_result END WHERE id=?",
                                   (due, task_status, int(skipped), reason if cursor.rowcount else "已跳过重复执行。", row["id"]))
        return claimed

    def create_run_now(self, account_id: str, task_id: str, target: dict[str, str], *, now: datetime | None = None) -> dict[str, Any]:
        """Create one explicit test occurrence for one already persisted temporary task."""
        current = _aware(now)
        if not isinstance(task_id, str) or not task_id.startswith("test_"):
            raise ValueError("即时执行只允许用于临时测试任务。")
        task = self.get(account_id, task_id)
        normalized_target = {"type": target.get("type"), "id": target.get("id")} if isinstance(target, dict) else {}
        if not task["enabled"] or task["draft"] or normalized_target not in task["targets"]:
            raise ValueError("测试发送对象或任务状态已经变化，请重新确认。")
        run_id = str(uuid.uuid4())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM scheduled_tasks WHERE id=? AND account_id=? AND enabled=1 AND deleted=0 AND revision=?",
                (task_id, account_id, task["revision"]),
            ).fetchone()
            if row is None:
                raise ValueError("测试任务已经变化，请重新确认。")
            connection.execute("""INSERT INTO scheduled_task_runs
                (id,task_id,account_id,revision,scheduled_at,task,status,created_at)
                VALUES(?,?,?,?,?,?,?,?)""",
                (run_id, task_id, account_id, task["revision"], current.isoformat(), row["payload"], "running", current.isoformat()),
            )
            connection.execute(
                "INSERT INTO scheduled_task_deliveries(run_id,target_type,target_id,status) VALUES(?,?,?,'pending')",
                (run_id, normalized_target["type"], normalized_target["id"]),
            )
            # A process crash must not leave a test task eligible for a later
            # normal scheduler claim. Startup recovery removes the hidden row.
            connection.execute("UPDATE scheduled_tasks SET status='running',next_due=NULL WHERE id=?", (task_id,))
            return self._run(connection, connection.execute(
                "SELECT * FROM scheduled_task_runs WHERE id=?", (run_id,)
            ).fetchone())

    def set_delivery(self, run_id: str, target_type: str, target_id: str, status: str, message: str = "") -> None:
        if status not in {"sending", *_FINAL_DELIVERIES}:
            raise ValueError("发送状态无效。")
        # Never store runtime exception bodies, credentials, or message content here.
        message = str(message)[:300]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM scheduled_task_deliveries WHERE run_id=? AND target_type=? AND target_id=?", (run_id, target_type, target_id)).fetchone()
            if row is None:
                raise ValueError("没有找到这次发送记录。")
            if row["status"] in _FINAL_DELIVERIES:
                if row["status"] == status:
                    return
                raise ValueError("已结束的发送记录不能重复执行。")
            if status == "sending" and row["status"] != "pending":
                raise ValueError("本次消息已在发送，不能重复执行。")
            connection.execute("UPDATE scheduled_task_deliveries SET status=?,message=? WHERE run_id=? AND target_type=? AND target_id=?", (status, message, run_id, target_type, target_id))

    def finish_run(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM scheduled_task_runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise ValueError("没有找到执行记录。")
            if row["status"] != "running":
                return self._run(connection, row)
            connection.execute("UPDATE scheduled_task_deliveries SET status='unknown',message='发送结果未确认，请检查后处理。' WHERE run_id=? AND status='sending'", (run_id,))
            connection.execute("UPDATE scheduled_task_deliveries SET status='skipped',message='本次未发送。' WHERE run_id=? AND status='pending'", (run_id,))
            run = self._run(connection, row)
            statuses = [item["status"] for item in run["deliveries"]]
            submitted = statuses.count("submitted")
            status = "submitted" if statuses and submitted == len(statuses) else "partial" if submitted else "unknown" if "unknown" in statuses else "failed" if "failed" in statuses else "skipped"
            summary = f"{submitted}/{len(statuses)} 个对象已提交微信" if submitted else {"failed": "发送失败，请查看执行记录。", "unknown": "发送结果待确认。", "skipped": "本次已跳过。"}[status]
            connection.execute("UPDATE scheduled_task_runs SET status=?,finished_at=? WHERE id=?", (status, _now().isoformat(), run_id))
            connection.execute("""UPDATE scheduled_tasks SET last_result=?,status=CASE WHEN next_due IS NULL THEN 'completed' ELSE 'enabled' END
                WHERE id=? AND revision=? AND deleted=0 AND enabled=1""", (summary, row["task_id"], row["revision"]))
            return {**run, "status": status}

    def recover_interrupted(self) -> None:
        """Called once by the sole runtime instance, never by manager readers."""
        if not self.path.is_file():
            return
        with self._connect() as connection:
            ids = [row["id"] for row in connection.execute("SELECT id FROM scheduled_task_runs WHERE status='running'")]
        for run_id in ids:
            self.finish_run(run_id)
        with self._connect() as connection:
            connection.execute(r"""UPDATE scheduled_tasks
                SET deleted=1,enabled=0,next_due=NULL,status='deleted',revision=revision+1,updated_at=?
                WHERE deleted=0 AND id LIKE 'test\_%' ESCAPE '\'""", (_now().isoformat(),))

    def authorize_delivery(self, account_id: str, task_id: str, run_id: str, target_type: str, target_id: str) -> bool:
        if not self.path.is_file():
            return False
        try:
            with self._connect() as connection:
                row = connection.execute("""SELECT t.payload,t.revision,r.task FROM scheduled_tasks t
                    JOIN scheduled_task_runs r ON r.task_id=t.id
                    JOIN scheduled_task_deliveries d ON d.run_id=r.id
                    WHERE t.id=? AND t.account_id=? AND t.enabled=1 AND t.deleted=0
                    AND r.id=? AND r.account_id=t.account_id AND r.revision=t.revision AND r.status='running'
                    AND d.target_type=? AND d.target_id=? AND d.status='sending'""", (task_id, account_id, run_id, target_type, target_id)).fetchone()
                if not row:
                    return False
                task = json.loads(row["payload"])
                return not task.get("draft", True) and {"type": target_type, "id": target_id} in task.get("targets", [])
        except (sqlite3.Error, ValueError, OSError):
            return False


__all__ = ["TaskStore", "ACTION_NAMES", "BEIJING", "normalize_task", "normalize_arguments", "normalize_schedule", "next_occurrences", "schedule_text", "parse_command", "validate_account"]
