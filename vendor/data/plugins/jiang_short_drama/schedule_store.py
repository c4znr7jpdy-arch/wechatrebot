"""Persistent validation and storage for recommendation schedules."""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime
from pathlib import Path
from typing import Any


MEDIA_OPTIONS: tuple[tuple[str, str], ...] = (
    ("短剧", "短剧（含 AI 漫剧）"),
    ("电视剧", "电视剧"),
    ("电影", "电影"),
    ("动漫", "动漫"),
    ("综艺", "综艺"),
    ("体育", "体育回放"),
    ("全部", "全部分类"),
)
DAY_OPTIONS: tuple[tuple[str, str], ...] = (
    ("mon", "周一"),
    ("tue", "周二"),
    ("wed", "周三"),
    ("thu", "周四"),
    ("fri", "周五"),
    ("sat", "周六"),
    ("sun", "周日"),
)
_MEDIA_VALUES = {value for value, _ in MEDIA_OPTIONS}
_DAY_VALUES = {value for value, _ in DAY_OPTIONS}
_GROUP_UMO_RE = re.compile(r"^[A-Za-z0-9_.-]+:GroupMessage:.+$")
_TASK_ID_RE = re.compile(r"^[a-f0-9]{12}$")


class ScheduleConfigError(ValueError):
    """Raised when a WebUI schedule payload is invalid."""


def _now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def validate_task(raw: Any, *, task_id: str | None = None) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raise ScheduleConfigError("任务配置必须是 JSON 对象")

    session = str(raw.get("session") or "").strip()
    if not _GROUP_UMO_RE.fullmatch(session):
        raise ScheduleConfigError(
            "群聊 UMO 格式不正确，应类似 aiocqhttp:GroupMessage:群ID"
        )
    media_type = str(raw.get("media_type") or "短剧").strip()
    if media_type not in _MEDIA_VALUES:
        raise ScheduleConfigError("推荐分类不受支持")

    try:
        hour = int(raw.get("hour", 9))
        minute = int(raw.get("minute", 0))
        limit = int(raw.get("limit", 12))
    except (TypeError, ValueError) as exc:
        raise ScheduleConfigError("时间和推荐数量必须是整数") from exc
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ScheduleConfigError("发送时间必须在 00:00 至 23:59 之间")
    if not 1 <= limit <= 20:
        raise ScheduleConfigError("每次推荐数量必须在 1 至 20 之间")

    raw_days = raw.get("days") or []
    if not isinstance(raw_days, list):
        raise ScheduleConfigError("星期设置必须是数组")
    days = [str(value) for value in raw_days if str(value) in _DAY_VALUES]
    days = list(dict.fromkeys(days))

    name = str(raw.get("name") or f"{media_type}推荐").strip()
    if not name:
        name = f"{media_type}推荐"
    if len(name) > 40:
        raise ScheduleConfigError("任务名称不能超过 40 个字符")

    normalized_id = str(task_id or raw.get("id") or "").strip().lower()
    if normalized_id and not _TASK_ID_RE.fullmatch(normalized_id):
        raise ScheduleConfigError("任务 ID 格式不正确")
    if not normalized_id:
        normalized_id = secrets.token_hex(6)

    created_at = str(raw.get("created_at") or _now_text())
    return {
        "id": normalized_id,
        "name": name,
        "session": session,
        "media_type": media_type,
        "hour": hour,
        "minute": minute,
        "days": days,
        "limit": limit,
        "enabled": bool(raw.get("enabled", True)),
        "created_at": created_at,
        "updated_at": _now_text(),
    }


def task_cron_expression(task: dict[str, Any]) -> str:
    days = ",".join(task.get("days") or ()) or "*"
    return f"{int(task['minute'])} {int(task['hour'])} * * {days}"


class RecommendationScheduleStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def load(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            payload = json.loads(self.path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        rows = payload.get("tasks") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return []
        tasks: list[dict[str, Any]] = []
        for row in rows:
            try:
                task = validate_task(row, task_id=str(row.get("id") or ""))
                task["created_at"] = str(row.get("created_at") or task["created_at"])
                task["updated_at"] = str(row.get("updated_at") or task["updated_at"])
                tasks.append(task)
            except (AttributeError, ScheduleConfigError):
                continue
        return tasks

    def save_all(self, tasks: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = self.path.with_suffix(".json.tmp")
        temp_path.write_text(
            json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2),
            "utf-8",
        )
        temp_path.replace(self.path)

    def upsert(self, raw: Any) -> dict[str, Any]:
        requested_id = str(raw.get("id") or "").strip() if isinstance(raw, dict) else ""
        tasks = self.load()
        current = next((item for item in tasks if item["id"] == requested_id), None)
        merged = {**(current or {}), **(raw if isinstance(raw, dict) else {})}
        task = validate_task(merged, task_id=requested_id or None)
        tasks = [item for item in tasks if item["id"] != task["id"]]
        tasks.append(task)
        self.save_all(tasks)
        return task

    def delete(self, task_id: str) -> bool:
        tasks = self.load()
        kept = [item for item in tasks if item["id"] != task_id]
        if len(kept) == len(tasks):
            return False
        self.save_all(kept)
        return True

    def get(self, task_id: str) -> dict[str, Any] | None:
        return next((item for item in self.load() if item["id"] == task_id), None)

    def public_config(self) -> dict[str, Any]:
        return {
            "tasks": self.load(),
            "media_options": [
                {"value": value, "label": label}
                for value, label in MEDIA_OPTIONS
            ],
            "day_options": [
                {"value": value, "label": label}
                for value, label in DAY_OPTIONS
            ],
            "timezone": "Asia/Shanghai",
        }
