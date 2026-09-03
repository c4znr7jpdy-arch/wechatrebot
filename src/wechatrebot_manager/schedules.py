from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any
from urllib.parse import quote

from .customer_logs import sanitize_log_text
from .json_io import write_json_atomic
from .plugin_center import EngineApiClient, PluginCenterError


_INTERNAL_MARKERS = (
    "cleanup",
    "heartbeat",
    "healthcheck",
    "health_check",
    "memory_compact",
    "cache_refresh",
)
_TYPE_LABELS = {
    "weather": ("天气预报", "天气"),
    "oilprice": ("油价提醒", "生活服务"),
    "kfc": ("疯狂星期四", "趣味内容"),
    "news": ("热点资讯", "资讯"),
    "bili": ("动态订阅", "动态订阅"),
    "checkin": ("每日签到", "签到"),
    "short_drama": ("短剧推荐", "短剧"),
    "yanyun": ("燕云签到", "游戏工具"),
}


def _task_name(task: dict[str, Any]) -> str:
    task_type = str(task.get("type") or "")
    session = str(task.get("session") or "default")
    if task_type == "bili_dynamic":
        return f"bili_{session}_{task.get('extra', {}).get('uid', '')}"
    return f"{task_type}_{session}"


def _source_kind(name: str) -> str:
    for prefix in ("weather", "oilprice", "kfc", "news", "bili", "checkin"):
        if name.startswith(f"{prefix}_"):
            return prefix
    if name.startswith("short_drama_recommend_"):
        return "short_drama"
    if name == "yanyun_checkin_daily":
        return "yanyun"
    return "custom"


def _target_from_payload(payload: dict[str, Any]) -> str:
    session = str(payload.get("session") or "")
    if "@chatroom" in session:
        match = re.search(r"([^:]+@chatroom)", session)
        return f"微信群 · {(match.group(1) if match else session)[-20:]}"
    if session:
        return f"个人聊天 · {session.rsplit(':', 1)[-1]}"
    return "系统任务"


def _schedule_text(expression: str | None) -> str:
    if not expression:
        return "单次任务"
    parts = expression.split()
    if len(parts) != 5:
        return expression
    minute, hour, day, month, weekday = parts
    if day == "*" and month == "*" and hour.isdigit() and minute.isdigit():
        clock = f"{int(hour):02d}:{int(minute):02d}"
        if weekday == "*":
            return f"每天 {clock}"
        labels = {
            "mon": "周一",
            "tue": "周二",
            "wed": "周三",
            "thu": "周四",
            "fri": "周五",
            "sat": "周六",
            "sun": "周日",
        }
        days = "、".join(labels.get(item, item) for item in weekday.split(","))
        return f"{days} {clock}"
    return expression


@dataclass(frozen=True, slots=True)
class ScheduleItem:
    id: str
    name: str
    source: str
    source_kind: str
    target: str
    enabled: bool
    status: str
    cron_expression: str
    schedule_text: str
    last_run_at: str
    next_run_time: str
    last_error: str

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "source_kind": self.source_kind,
            "target": self.target,
            "enabled": self.enabled,
            "status": self.status,
            "cron_expression": self.cron_expression,
            "schedule_text": self.schedule_text,
            "last_run_at": self.last_run_at,
            "next_run_time": self.next_run_time,
            "last_error": self.last_error,
        }


class ScheduleService:
    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.data_root = self.install_root / "app" / "astrbot" / "data"
        self.database = self.data_root / "data_v4.db"
        self.engine = EngineApiClient(self.install_root)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def list(self) -> dict[str, object]:
        if not self.database.is_file():
            return {"items": [], "running": 0, "paused": 0}
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, name, description, cron_expression, payload,
                       enabled, status, last_run_at, next_run_time, last_error
                FROM cron_jobs ORDER BY enabled DESC, next_run_time, name
                """
            ).fetchall()
        items: list[ScheduleItem] = []
        for row in rows:
            raw_name = str(row["name"] or "")
            if any(marker in raw_name.casefold() for marker in _INTERNAL_MARKERS):
                continue
            kind = _source_kind(raw_name)
            label, source = _TYPE_LABELS.get(
                kind,
                (sanitize_log_text(str(row["description"] or raw_name)), "自定义任务"),
            )
            try:
                payload = json.loads(row["payload"] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            if not isinstance(payload, dict):
                payload = {}
            cron = str(row["cron_expression"] or "")
            items.append(
                ScheduleItem(
                    id=str(row["job_id"]),
                    name=label,
                    source=source,
                    source_kind=kind,
                    target=_target_from_payload(payload),
                    enabled=bool(row["enabled"]),
                    status="运行中" if bool(row["enabled"]) else "已暂停",
                    cron_expression=cron,
                    schedule_text=_schedule_text(cron),
                    last_run_at=str(row["last_run_at"] or ""),
                    next_run_time=str(row["next_run_time"] or ""),
                    last_error=sanitize_log_text(str(row["last_error"] or "")),
                )
            )
        return {
            "items": [item.to_dict() for item in items],
            "running": sum(1 for item in items if item.enabled),
            "paused": sum(1 for item in items if not item.enabled),
        }

    def _job(self, job_id: str) -> sqlite3.Row:
        if not self.database.is_file():
            raise ValueError("当前还没有定时任务。")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id, name, cron_expression, payload FROM cron_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise ValueError("没有找到这个定时任务。")
        return row

    def _engine_update(self, job_id: str, payload: dict[str, object]) -> bool:
        try:
            if not self.engine.is_available():
                return False
            self.engine.request(
                "PATCH", f"/api/v1/cron/jobs/{quote(job_id, safe='')}", payload
            )
            return True
        except PluginCenterError:
            return False

    def _offline_update(self, job_id: str, **values: object) -> None:
        allowed = {"enabled", "cron_expression", "status", "next_run_time"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        assignment = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            connection.execute(
                f"UPDATE cron_jobs SET {assignment} WHERE job_id = ?",
                (*updates.values(), job_id),
            )

    def _schedule_tasks_path(self) -> Path:
        return self.data_root / "jiang_schedule" / "tasks.json"

    def _drama_tasks_path(self) -> Path:
        return (
            self.data_root
            / "plugin_data"
            / "jiang_short_drama"
            / "recommendation_schedules.json"
        )

    @staticmethod
    def _read_tasks(path: Path) -> dict[str, Any]:
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {"tasks": []}
        return payload if isinstance(payload, dict) else {"tasks": []}

    def _update_source(
        self,
        name: str,
        *,
        enabled: bool | None = None,
        clock: tuple[int, int] | None = None,
        delete: bool = False,
    ) -> None:
        kind = _source_kind(name)
        if kind in {"weather", "oilprice", "kfc", "news", "bili", "checkin"}:
            path = self._schedule_tasks_path()
            payload = self._read_tasks(path)
            tasks = payload.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            kept: list[dict[str, Any]] = []
            for raw in tasks:
                if not isinstance(raw, dict) or _task_name(raw) != name:
                    if isinstance(raw, dict):
                        kept.append(raw)
                    continue
                if delete:
                    continue
                item = dict(raw)
                if enabled is not None:
                    item["enabled"] = enabled
                if clock is not None:
                    item["hour"], item["minute"] = clock
                kept.append(item)
            payload["tasks"] = kept
            write_json_atomic(path, payload)
        elif kind == "short_drama":
            task_id = name.removeprefix("short_drama_recommend_")
            path = self._drama_tasks_path()
            payload = self._read_tasks(path)
            tasks = payload.get("tasks")
            tasks = tasks if isinstance(tasks, list) else []
            kept = []
            for raw in tasks:
                if not isinstance(raw, dict) or str(raw.get("id")) != task_id:
                    if isinstance(raw, dict):
                        kept.append(raw)
                    continue
                if delete:
                    continue
                item = dict(raw)
                if enabled is not None:
                    item["enabled"] = enabled
                if clock is not None:
                    item["hour"], item["minute"] = clock
                kept.append(item)
            payload["tasks"] = kept
            write_json_atomic(path, payload)
        elif kind == "yanyun":
            path = self.data_root / "config" / "jiang_yanyun_checkin_config.json"
            payload = self._read_tasks(path)
            if enabled is not None or delete:
                payload["enabled"] = False if delete else enabled
            if clock is not None:
                payload["schedule_time"] = f"{clock[0]:02d}:{clock[1]:02d}"
            write_json_atomic(path, payload)

    def set_enabled(self, job_id: str, enabled: bool) -> dict[str, object]:
        row = self._job(job_id)
        self._update_source(str(row["name"]), enabled=enabled)
        if not self._engine_update(job_id, {"enabled": enabled}):
            self._offline_update(
                job_id,
                enabled=int(enabled),
                status="scheduled" if enabled else "paused",
                next_run_time=None if not enabled else None,
            )
        return {"updated": True, "enabled": enabled}

    def update_time(self, job_id: str, value: str) -> dict[str, object]:
        match = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", value)
        if not match:
            raise ValueError("时间格式应为 HH:MM。")
        hour, minute = int(match.group(1)), int(match.group(2))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError("请输入有效的 24 小时时间。")
        row = self._job(job_id)
        parts = str(row["cron_expression"] or "").split()
        if len(parts) != 5:
            raise ValueError("这个任务暂不支持修改执行时间。")
        expression = " ".join((str(minute), str(hour), *parts[2:]))
        self._update_source(str(row["name"]), clock=(hour, minute))
        if not self._engine_update(job_id, {"cron_expression": expression}):
            self._offline_update(job_id, cron_expression=expression)
        return {"updated": True, "cron_expression": expression}

    def delete(self, job_id: str) -> dict[str, object]:
        row = self._job(job_id)
        self._update_source(str(row["name"]), delete=True)
        deleted_online = False
        try:
            if self.engine.is_available():
                self.engine.request(
                    "DELETE", f"/api/v1/cron/jobs/{quote(job_id, safe='')}"
                )
                deleted_online = True
        except PluginCenterError:
            deleted_online = False
        if not deleted_online:
            with self._connect() as connection:
                connection.execute("DELETE FROM cron_jobs WHERE job_id = ?", (job_id,))
        return {"deleted": True}


__all__ = ["ScheduleItem", "ScheduleService"]
