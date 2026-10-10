"""Shared, standard-library task model for the manager, runtime and delivery gate.

This module is copied unchanged into both runtime components during assembly.
Only explicitly saved tasks exist; reading an empty store never seeds jobs.
"""
from __future__ import annotations

from contextlib import contextmanager
from calendar import monthrange
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterator
import uuid


BEIJING = timezone(timedelta(hours=8), "Asia/Shanghai")
ACTION_NAMES = {
    "weather": "天气预报",
    "news": "抖音热榜",
    "oilprice": "油价提醒",
    "kfc": "星期四文案",
    "short_drama": "影视推荐",
    "group_analysis": "群聊分析",
    "text": "文字提醒",
}
ACTION_FEATURES = {
    "weather": "jiang_commands",
    "news": "jiang_commands",
    "oilprice": "jiang_commands",
    "kfc": "jiang_commands",
    "short_drama": "jiang_short_drama",
    "group_analysis": "astrbot_plugin_qq_group_daily_analysis",
    "text": "",
}

# User-facing command declarations are kept separate from ``ACTION_NAMES``.
# ``ACTION_NAMES`` describes the actions the unified executor can run today;
# this catalog also describes commands that are visible in the product but
# still need an account, interaction, or a dedicated adapter before they can
# safely become a task.  Keeping those entries here lets every entry point
# explain the same decision without exposing plugin or implementation names.
SCHEDULED_COMMANDS = (
    {
        "id": "weather", "display_name": "天气预报", "action": "weather",
        "status": "supported", "aliases": ("天气", "城市天气"),
        "parameters": ("城市", "执行日（今天/明天）"),
        "dependencies": ("常用查询功能", "天气服务"),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "天气图片卡片", "failure_policy": "按发送对象分别记录失败",
        "reason": "", "alternative": "",
    },
    {
        "id": "news", "display_name": "抖音热榜", "action": "news",
        "status": "supported", "aliases": ("新闻", "资讯", "热点"),
        "parameters": ("资讯条数",), "dependencies": ("常用查询功能",),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "资讯文字", "failure_policy": "按发送对象分别记录失败",
        "reason": "", "alternative": "",
    },
    {
        "id": "oilprice", "display_name": "油价提醒", "action": "oilprice",
        "status": "supported", "aliases": ("油价",),
        "parameters": ("省份",), "dependencies": ("常用查询功能", "油价服务"),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "油价文字", "failure_policy": "按发送对象分别记录失败",
        "reason": "", "alternative": "",
    },
    {
        "id": "kfc", "display_name": "星期四文案", "action": "kfc",
        "status": "supported", "aliases": ("kfc", "疯狂星期四", "星期四文案"),
        "parameters": (), "dependencies": ("常用查询功能",),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "文案文字", "failure_policy": "按发送对象分别记录失败",
        "reason": "", "alternative": "",
    },
    {
        "id": "short_drama", "display_name": "影视推荐", "action": "short_drama",
        "status": "supported", "aliases": ("短剧推荐", "影视推荐"),
        "parameters": ("推荐分类", "数量"), "dependencies": ("影视内容服务", "托管播放页"),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "影视链接卡片或安全文字", "failure_policy": "按发送对象分别记录失败",
        "reason": "", "alternative": "",
    },
    {
        "id": "group_analysis", "display_name": "群聊分析", "action": "group_analysis",
        "status": "supported", "aliases": ("群分析", "群聊分析", "群日报", "group_analysis"),
        "parameters": ("分析天数",), "dependencies": ("群聊分析功能", "文本模型", "群消息记录"),
        "recipient_types": ("group",), "external_write": False,
        "result": "各群独立的分析报告", "failure_policy": "按群分别生成并记录结果",
        "reason": "", "alternative": "",
    },
    {
        "id": "text", "display_name": "文字提醒", "action": "text",
        "status": "supported", "aliases": ("提醒", "文字提醒"),
        "parameters": ("提醒文字",), "dependencies": (),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "保存的文字", "failure_policy": "按发送对象分别记录失败",
        "reason": "", "alternative": "",
    },
    {
        "id": "epic", "display_name": "免费游戏查询", "action": None,
        "status": "needs_condition", "aliases": ("epic", "喜加一", "免费游戏"),
        "parameters": (), "dependencies": ("免费游戏查询适配",),
        "recipient_types": ("group", "private"), "external_write": False,
        "result": "查询文字", "failure_policy": "生成失败时跳过本次执行",
        "reason": "该命令尚未接入统一任务执行器。", "alternative": "先在聊天中手动查询。",
    },
    {
        "id": "bili_dynamic", "display_name": "动态订阅", "action": None,
        "status": "needs_condition", "aliases": ("订阅动态", "b站动态"),
        "parameters": ("账号", "检查频率", "首次基线"),
        "dependencies": ("动态订阅服务",), "recipient_types": ("group", "private"),
        "external_write": False, "result": "仅发送新动态", "failure_policy": "按订阅和对象分别记录失败",
        "reason": "动态订阅需要检查频率和去重基线，不能当作固定内容直接执行。",
        "alternative": "请先在订阅管理中创建订阅。",
    },
    {
        "id": "contextual", "display_name": "上下文查询", "action": None,
        "status": "unsupported", "aliases": (), "parameters": ("当前对话上下文",),
        "dependencies": ("当前消息上下文",), "recipient_types": ("group", "private"),
        "external_write": False, "result": "依赖上下文的回复", "failure_policy": "不创建计划",
        "reason": "该命令依赖临时对话上下文，定时执行时无法确认引用内容。",
        "alternative": "请在聊天中手动发送，或改为固定文字提醒。",
    },
    {
        "id": "interactive", "display_name": "交互式命令", "action": None,
        "status": "unsupported", "aliases": (), "parameters": ("临时确认或验证码",),
        "dependencies": ("实时人工操作",), "recipient_types": (),
        "external_write": True, "result": "交互结果", "failure_policy": "不创建计划",
        "reason": "该命令需要扫码、验证码或实时人工确认，不能无人值守执行。",
        "alternative": "请在聊天中手动执行。",
    },
)
SCHEDULED_COMMAND_VERSION = 1


def scheduled_command_catalog() -> list[dict[str, Any]]:
    """Return a JSON-safe copy for manager and runtime capability responses."""
    return [
        {key: list(value) if isinstance(value, tuple) else value for key, value in item.items()}
        for item in SCHEDULED_COMMANDS
    ]


def _command_definition(action: str | None = None, command_id: str | None = None) -> dict[str, Any] | None:
    for item in SCHEDULED_COMMANDS:
        if action is not None and item.get("action") == action:
            return item
        if command_id is not None and item.get("id") == command_id:
            return item
    return None
SHORT_DRAMA_MEDIA_TYPES = ("短剧", "电视剧", "电影", "动漫", "综艺", "体育", "全部")
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
    if not isinstance(raw, dict) or raw.get("kind") not in {"once", "daily", "weekly", "monthly"}:
        raise ValueError("请选择仅一次、每天、每周或每月执行。")
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
    if raw["kind"] == "monthly":
        days = raw.get("monthdays", [])
        last_day = raw.get("last_day", False)
        if (not isinstance(days, list) or type(last_day) is not bool
                or any(type(day) is not int or not 1 <= day <= 31 for day in days)
                or (not days and not last_day and not draft)):
            raise ValueError("请至少选择每月的一个日期或月末最后一天。")
        result["monthdays"] = sorted(set(days))
        result["last_day"] = last_day
    return result


def next_occurrences(schedule: dict[str, Any], now: datetime | None = None, count: int = 3) -> list[str]:
    schedule = normalize_schedule(schedule)
    current = _aware(now).astimezone(BEIJING)
    clock = time.fromisoformat(schedule["time"])
    result: list[str] = []
    if schedule["kind"] == "once":
        occurrence = datetime.combine(date.fromisoformat(schedule["date"]), clock, BEIJING)
        return [occurrence.isoformat()] if occurrence > current else []
    count = max(1, min(count, 20))
    # A task on the 31st can skip a short month. 62 days per occurrence
    # covers that gap while retaining the existing bounded preview size.
    horizon = 62 if schedule["kind"] == "monthly" else 7
    for offset in range(count * horizon + 1):
        day = current.date() + timedelta(days=offset)
        if schedule["kind"] == "weekly" and day.weekday() not in schedule["weekdays"]:
            continue
        if schedule["kind"] == "monthly" and not (
            day.day in schedule["monthdays"]
            or (schedule["last_day"] and day.day == monthrange(day.year, day.month)[1])
        ):
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
    elif schedule["kind"] == "monthly":
        dates = [f"{day}日" for day in schedule["monthdays"]]
        if schedule["last_day"]:
            dates.append("最后一天")
        prefix = "每月" + "、".join(dates)
    else:
        prefix = "每周" + "、".join(("一", "二", "三", "四", "五", "六", "日")[day] for day in schedule["weekdays"])
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
    if action == "short_drama":
        media_type = str(arguments.get("media_type") or "短剧").strip()
        limit = arguments.get("limit", 12)
        if media_type not in SHORT_DRAMA_MEDIA_TYPES:
            raise ValueError("请选择支持的影视推荐分类。")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise ValueError("影视推荐数量应为 1 到 20。")
        return {"media_type": media_type, "limit": limit}
    if action == "group_analysis":
        days = arguments.get("days", 1)
        if type(days) is not int or not 1 <= days <= 7:
            raise ValueError("群聊分析天数应为 1 到 7。")
        return {"days": days}
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
        if action == "group_analysis" and kind != "group":
            raise ValueError("群聊分析只能发送到被分析的群聊。")
        key = (kind, identity)
        if key not in seen:
            # Names are resolved from the current account's directory, not stored
            # as a second, long-lived address book.
            normalized_targets.append({"type": kind, "id": identity})
            seen.add(key)
    schedule = normalize_schedule(raw.get("schedule"), draft=draft)
    if enabled and not next_occurrences(schedule, now):
        raise ValueError("执行时间已过去，请选择未来的时间。")
    definition_version = raw.get("definition_version", SCHEDULED_COMMAND_VERSION)
    if type(definition_version) is not int or definition_version < 1:
        raise ValueError("任务命令定义版本无效，请重新读取后保存。")
    return {"id": task_id, "account_id": account, "name": name, "action": action,
            "arguments": arguments, "targets": normalized_targets, "schedule": schedule,
            "enabled": enabled, "draft": draft, "definition_version": definition_version}


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
    if text == "短剧推荐":
        return {"action": "short_drama", "arguments": {"media_type": "短剧", "limit": 12}}
    for alias in _command_definition(action="group_analysis")["aliases"]:
        if match := re.fullmatch(re.escape(alias) + r"(?:\s+(\d{1,2}))?", text):
            return {"action": "group_analysis", "arguments": normalize_arguments("group_analysis", {"days": int(match[1] or 1)})}
    if match := re.fullmatch(r"(?:提醒|文字提醒)\s+(.+)", text, re.DOTALL):
        return {"action": "text", "arguments": normalize_arguments("text", {"text": match[1]})}
    raise ValueError("当前支持天气、新闻、油价、星期四文案、短剧推荐和群聊分析命令；也可以直接选择内容。")


def inspect_scheduled_command(command: str) -> dict[str, Any]:
    """Classify a user-visible command before it enters the task editor.

    Parsing a supported command remains backwards compatible with
    :func:`parse_command`; this wrapper adds the product decision and the
    explanation required for commands that need interaction or another
    adapter.  Unknown text is deliberately returned as an actionable result
    instead of being treated as an executable task.
    """
    if not isinstance(command, str) or len(command) > 500:
        raise ValueError("请输入一个支持的查询命令。")
    text = command.strip().lstrip("/\\").strip()
    if not text:
        raise ValueError("请输入一个支持的查询命令。")
    try:
        parsed = parse_command(command)
    except ValueError:
        parsed = None
    if parsed is not None:
        definition = _command_definition(action=parsed.get("action"))
        if definition is None:
            return {
                "recognized": True, "status": "unsupported", "command": text,
                "reason": "该命令尚未声明可定时化参数和结果。",
                "alternative": "请直接选择定时任务中的固定内容。",
            }
        return {
            **parsed, "recognized": True, "status": definition["status"],
            "command": text,
            "definition": scheduled_command_catalog()[next(
                index for index, item in enumerate(SCHEDULED_COMMANDS)
                if item["id"] == definition["id"]
            )],
            "reason": definition["reason"], "alternative": definition["alternative"],
        }

    lowered = text.casefold()
    command_id = None
    if lowered in {"epic", "喜加一", "免费游戏"}:
        command_id = "epic"
    elif lowered.startswith(("订阅动态", "b站动态")):
        command_id = "bili_dynamic"
    elif lowered.startswith(("查询", "查一下", "介绍一下", "科普")):
        command_id = "contextual"
    elif lowered in {"理想", "图来", "占卜", "塔罗牌", "登录", "扫码"}:
        command_id = "interactive"
    definition = _command_definition(command_id=command_id)
    if definition is None:
        return {
            "recognized": False, "status": "unsupported", "command": text,
            "reason": "没有识别出可定时化的命令。",
            "alternative": "请直接选择定时任务中的内容，或改为固定文字提醒。",
        }
    return {
        "recognized": True, "status": definition["status"], "command": text,
        "definition": scheduled_command_catalog()[next(
            index for index, item in enumerate(SCHEDULED_COMMANDS)
            if item["id"] == definition["id"]
        )],
        "reason": definition["reason"], "alternative": definition["alternative"],
    }


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
            CREATE TABLE IF NOT EXISTS scheduled_task_owners (
                action TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                phase TEXT NOT NULL, plan TEXT NOT NULL
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
        # Tasks written before command metadata existed are deliberately marked
        # for review instead of being silently treated as current.
        task.setdefault("definition_version", 0)
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
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for owner in connection.execute("SELECT plan FROM scheduled_task_owners WHERE phase='prepared'"):
                if any(item.get("id") == task["id"] for item in json.loads(owner["plan"]).get("items", [])):
                    raise ValueError("该任务标识正在迁移，请等待迁移完成后再编辑。")
            self._save_task(connection, task, payload.get("revision", 0), current)
        return self.get(task["account_id"], task["id"])

    def _save_task(self, connection: sqlite3.Connection, task: dict, expected: int, current: datetime) -> None:
        """Save one normalized task inside the caller's transaction."""
        serialized = _json(task)
        previous = connection.execute("SELECT * FROM scheduled_tasks WHERE id=?", (task["id"],)).fetchone()
        if type(expected) is not int or expected < 0:
            raise ValueError("任务版本无效，请刷新后重试。")
        if previous:
            if previous["account_id"] != task["account_id"] or previous["deleted"]:
                raise ValueError("任务已删除或不属于当前机器人账号。")
            if expected == 0 and previous["payload"] == serialized:
                return  # repeated create acknowledgement
            if expected != previous["revision"]:
                raise ValueError("任务已在其他窗口修改，请重新读取后保存。")
            if previous["payload"] == serialized:
                return
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

    def action_owner(self, action: str) -> dict[str, Any] | None:
        """Read permanent scheduler ownership without creating or upgrading a store."""
        if not self.path.is_file():
            return None
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_task_owners'").fetchone():
                return None
            row = connection.execute("SELECT account_id,phase,plan FROM scheduled_task_owners WHERE action=?", (action,)).fetchone()
            if row is None:
                return None
            plan = json.loads(row[2])
            if row[1] not in {"confirmed", "prepared", "complete"} or not isinstance(plan, dict):
                raise ValueError("任务迁移记录不完整，请保留数据并检查后重试。")
            return {"account_id": row[0], "phase": row[1], "plan": plan}
        finally:
            connection.close()

    def claim_action(self, action: str, account_id: str, plan: dict, *, phase: str = "complete",
                     expected_plan: dict | None = None) -> dict:
        """Retire a legacy producer independently of later edits or deleted tasks."""
        validate_account(account_id)
        if action not in ACTION_NAMES or not isinstance(plan, dict) or phase not in {"confirmed", "prepared", "complete"}:
            raise ValueError("任务迁移内容无效。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if phase == "prepared":
                for item in plan.get("items", []):
                    if connection.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (item["id"],)).fetchone():
                        raise ValueError("迁移目标任务标识已被使用；旧配置尚未修改，请重新预览迁移。")
            previous = connection.execute("SELECT * FROM scheduled_task_owners WHERE action=?", (action,)).fetchone()
            if previous:
                replacing = (previous["phase"] == phase == "confirmed" and previous["account_id"] == account_id
                             and expected_plan is not None and previous["plan"] == _json(expected_plan))
                if replacing:
                    connection.execute("UPDATE scheduled_task_owners SET plan=? WHERE action=?", (_json(plan), action))
                elif (previous["phase"] != "complete" or phase != "complete") and (
                        previous["account_id"] != account_id or previous["plan"] != _json(plan)):
                    raise ValueError("已有另一项任务迁移待处理，请刷新后继续。")
                elif previous["phase"] == "confirmed" and phase == "prepared":
                    connection.execute("UPDATE scheduled_task_owners SET phase='prepared' WHERE action=?", (action,))
                elif previous["phase"] != "complete" and phase == "complete":
                    raise ValueError("请先完成已经确认的旧任务迁移。")
            else:
                connection.execute("INSERT INTO scheduled_task_owners VALUES (?,?,?,?)",
                                   (action, account_id, phase, _json(plan)))
        return self.action_owner(action)

    def migration_conflicts(self, plan: dict) -> bool:
        if not self.path.is_file():
            return False
        connection = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'").fetchone():
                return False
            return any(connection.execute("SELECT 1 FROM scheduled_tasks WHERE id=?", (item["id"],)).fetchone()
                       for item in plan.get("items", []))
        finally:
            connection.close()

    def cancel_action_migration(self, action: str, account_id: str, plan: dict) -> None:
        validate_account(account_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "DELETE FROM scheduled_task_owners WHERE action=? AND account_id=? AND phase='confirmed' AND plan=?",
                (action, account_id, _json(plan)),
            ).rowcount
            if changed != 1:
                raise ValueError("迁移已经开始写入或状态已变化，不能撤销，请继续迁移。")

    def complete_action_migration(self, action: str, account_id: str, plan: dict) -> None:
        """Commit all paused drafts and retirement together; a retry never recreates a deleted task."""
        current = _now()
        tasks = [normalize_task(item, now=current) for item in plan.get("items", [])]
        if not tasks or any(item["action"] != action or item["account_id"] != account_id
                            or not item["draft"] or item["enabled"] for item in tasks):
            raise ValueError("旧任务只能迁入所属账号的暂停草稿。")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owner = connection.execute("SELECT * FROM scheduled_task_owners WHERE action=?", (action,)).fetchone()
            if not owner or owner["account_id"] != account_id or owner["plan"] != _json(plan):
                raise ValueError("迁移记录已变化，请重新读取。")
            if owner["phase"] == "complete":
                return
            if owner["phase"] != "prepared":
                raise ValueError("迁移记录状态不正确。")
            for task in tasks:
                self._save_task(connection, task, 0, current)
            connection.execute("UPDATE scheduled_task_owners SET phase='complete' WHERE action=?", (action,))

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


__all__ = [
    "TaskStore", "ACTION_NAMES", "SCHEDULED_COMMANDS", "SCHEDULED_COMMAND_VERSION", "BEIJING", "normalize_task",
    "normalize_arguments", "normalize_schedule", "next_occurrences", "schedule_text",
    "parse_command", "inspect_scheduled_command", "scheduled_command_catalog", "validate_account",
]
