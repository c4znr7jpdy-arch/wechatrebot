from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
import sqlite3
from typing import Any, Callable

from .runtime_status import runtime_snapshot
from urllib.parse import quote

from .customer_logs import sanitize_log_text
from .json_io import write_json_atomic
from .plugin_center import EngineApiClient


_INTERNAL_MARKERS = (
    "cleanup",
    "heartbeat",
    "healthcheck",
    "health_check",
    "memory_compact",
    "cache_refresh",
)
_REMOVED_TASK_NAMES = {
    # 旧版本可能已把已下架插件的计划写入 cron_jobs。更新后不再把这类
    # 遗留任务展示成用户创建的计划，也不尝试修改已经移除的插件配置。
    "yanyun_checkin_daily",
}
_TYPE_LABELS = {
    "weather": ("天气预报", "天气"),
    "oilprice": ("油价提醒", "生活服务"),
    "kfc": ("疯狂星期四", "趣味内容"),
    "news": ("热点资讯", "资讯"),
    "bili": ("动态订阅", "动态订阅"),
    "checkin": ("每日签到", "签到"),
    "short_drama": ("短剧推荐", "短剧"),
}


def _task_name(task: dict[str, Any]) -> str:
    task_type = str(task.get("type") or "")
    # Match jiang_schedule._make_job_name exactly: an explicitly blank session
    # remains blank, which is how the obsolete packaged check-in was named.
    session = str(task.get("session", "default"))
    if task_type == "bili_dynamic":
        return f"bili_{session}_{task.get('extra', {}).get('uid', '')}"
    return f"{task_type}_{session}"


def _source_kind(name: str) -> str:
    for prefix in ("weather", "oilprice", "kfc", "news", "bili", "checkin"):
        if name.startswith(f"{prefix}_"):
            return prefix
    if name.startswith("short_drama_recommend_"):
        return "short_drama"
    return "custom"


def _target_from_payload(payload: dict[str, Any]) -> str:
    target_type, target_id = _target_identity(payload)
    if target_type == "group":
        return f"微信群 · {target_id[-20:]}"
    if target_type == "direct":
        return f"个人聊天 · {target_id.rsplit(':', 1)[-1]}"
    return "系统任务"


def _target_identity(payload: dict[str, Any]) -> tuple[str, str]:
    """Return a machine-readable target without relying on the display label."""
    session = str(payload.get("session") or "")
    if "@chatroom" in session:
        match = re.search(r"([A-Za-z0-9_.-]+@chatroom)", session)
        return "group", match.group(1) if match else ""
    if session:
        return "direct", session.rsplit(":", 1)[-1]
    return "system", ""


def _is_obsolete_default_checkin(row: sqlite3.Row) -> bool:
    """Recognize the invalid check-in job seeded by older customer builds."""
    if str(row["name"] or "") not in {"checkin_", "checkin_default"}:
        return False
    try:
        payload = json.loads(row["payload"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    return not isinstance(payload, dict) or not str(payload.get("session") or "").strip()


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
    target_type: str
    target_id: str
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
            "target_type": self.target_type,
            "target_id": self.target_id,
            "enabled": self.enabled,
            "status": self.status,
            "cron_expression": self.cron_expression,
            "schedule_text": self.schedule_text,
            "last_run_at": self.last_run_at,
            "next_run_time": self.next_run_time,
            "last_error": self.last_error,
        }


class ScheduleService:
    def __init__(self, install_root: str | Path, *, account_hint: Callable[[], str] | None = None) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.data_root = self.install_root / "app" / "astrbot" / "data"
        self.database = self.data_root / "data_v4.db"
        self.engine = EngineApiClient(self.install_root)
        self._account_hint = account_hint or self._runtime_account

    def _runtime_account(self) -> str:
        snapshot = runtime_snapshot()
        return snapshot.wechat_identity if snapshot.account_status_available else ""

    @staticmethod
    def _owner(row: sqlite3.Row) -> str:
        try:
            payload = json.loads(row["payload"] or "{}")
        except (TypeError, ValueError):
            return ""
        return str(payload.get("account_id") or "").strip() if isinstance(payload, dict) else ""

    def _authorize_job(self, row: sqlite3.Row) -> None:
        account = self._account_hint()
        if not account:
            raise ValueError("尚未确认当前微信账号，请连接后刷新任务。")
        owner = self._owner(row)
        if not owner:
            raise ValueError("旧任务缺少账号归属，已保留原数据并禁止修改。")
        if owner != account:
            raise ValueError("任务不属于当前微信账号，请刷新任务列表。")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def list(self, account_id: str | None = None) -> dict[str, object]:
        account = self._account_hint()
        if not account or (account_id is not None and account_id != account):
            return {"items": [], "running": 0, "paused": 0, "account_id": "", "requires_login": True}
        if not self.database.is_file():
            return {"items": [], "running": 0, "paused": 0}
        with self._connect() as connection:
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'cron_jobs'"
            ).fetchone() is None:
                return {"items": [], "running": 0, "paused": 0}
        self._remove_obsolete_default_checkins()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT job_id, name, description, cron_expression, payload,
                       enabled, status, last_run_at, next_run_time, last_error
                FROM cron_jobs ORDER BY enabled DESC, next_run_time, name
                """
            ).fetchall()
        items: list[ScheduleItem] = []
        unassigned = 0
        for row in rows:
            owner = self._owner(row)
            if not owner:
                unassigned += 1
                continue
            if owner != account:
                continue
            raw_name = str(row["name"] or "")
            if raw_name in _REMOVED_TASK_NAMES or any(
                marker in raw_name.casefold() for marker in _INTERNAL_MARKERS
            ):
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
            target_type, target_id = _target_identity(payload)
            items.append(
                ScheduleItem(
                    id=str(row["job_id"]),
                    name=label,
                    source=source,
                    source_kind=kind,
                    target=_target_from_payload(payload),
                    target_type=target_type,
                    target_id=target_id,
                    enabled=bool(row["enabled"]),
                    status="运行中" if bool(row["enabled"]) else "已暂停",
                    cron_expression=cron,
                    schedule_text=_schedule_text(cron),
                    last_run_at=str(row["last_run_at"] or ""),
                    next_run_time=str(row["next_run_time"] or ""),
                    last_error=sanitize_log_text(str(row["last_error"] or "")),
                )
            )
        if self._account_hint() != account:
            return {"items": [], "running": 0, "paused": 0, "account_id": "", "requires_login": True}
        return {
            "items": [item.to_dict() for item in items],
            "running": sum(1 for item in items if item.enabled),
            "paused": sum(1 for item in items if not item.enabled),
            "account_id": account,
            "unassigned_count": unassigned,
            "message": "部分旧任务缺少账号归属，已保留原数据并限制操作。" if unassigned else "",
        }

    def _remove_obsolete_default_checkins(self) -> None:
        """Permanently remove the target-less job created by pre-fix packages."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, name, payload FROM cron_jobs "
                "WHERE name IN ('checkin_', 'checkin_default')"
            ).fetchall()
        account = self._account_hint()
        obsolete = [row for row in rows if account and self._owner(row) == account and _is_obsolete_default_checkin(row)]
        for row in obsolete:
            self._authorize_job(row)
            job_id = str(row["job_id"])
            name = str(row["name"] or "")
            self._validate_source(name)
            if self.engine.is_available():
                self.engine.request(
                    "DELETE", f"/api/v1/cron/jobs/{quote(job_id, safe='')}"
                )
            else:
                with self._connect() as connection:
                    connection.execute(
                        "DELETE FROM cron_jobs WHERE job_id = ?", (job_id,)
                    )
            self._update_source(name, delete=True)

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
        self._authorize_job(row)
        return row

    def _engine_update(self, job_id: str, payload: dict[str, object]) -> bool:
        if not self.engine.is_available():
            return False
        self._job(job_id)
        self.engine.request(
            "PATCH", f"/api/v1/cron/jobs/{quote(job_id, safe='')}", payload
        )
        return True

    def _offline_update(self, job_id: str, **values: object) -> None:
        allowed = {"enabled", "cron_expression", "status", "next_run_time"}
        updates = {key: value for key, value in values.items() if key in allowed}
        if not updates:
            return
        assignment = ", ".join(f"{key} = ?" for key in updates)
        with self._connect() as connection:
            row = connection.execute("SELECT payload FROM cron_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise ValueError("没有找到这个定时任务。")
            self._authorize_job(row)
            connection.execute(
                f"UPDATE cron_jobs SET {assignment} WHERE job_id = ?",
                (*updates.values(), job_id),
            )

    def _schedule_tasks_path(self) -> Path:
        return self.data_root / "jiang_schedule" / "tasks.json"

    def _checkin_config_path(self) -> Path:
        return self.data_root / "jiang_schedule" / "checkin_config.json"

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
        except FileNotFoundError:
            return {"tasks": []}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("定时任务来源配置无法读取，请先修复配置后重试。") from exc
        if (
            not isinstance(payload, dict)
            or not isinstance(payload.get("tasks"), list)
            or any(not isinstance(task, dict) for task in payload["tasks"])
        ):
            raise ValueError("定时任务来源配置格式不正确，请先修复配置后重试。")
        return payload

    def _validate_source(self, name: str) -> None:
        """Reject unreadable sources before changing a live job or its database."""
        kind = _source_kind(name)
        if kind in {"weather", "oilprice", "kfc", "news", "bili", "checkin"}:
            self._read_tasks(self._schedule_tasks_path())
            if kind == "checkin":
                self._read_checkin_config()
        elif kind == "short_drama":
            self._read_tasks(self._drama_tasks_path())

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
            if kind == "checkin":
                self._update_checkin_config(
                    name=name,
                    enabled=enabled,
                    clock=clock,
                    delete=delete,
                )
            path = self._schedule_tasks_path()
            payload = self._read_tasks(path)
            tasks = payload["tasks"]
            kept: list[dict[str, Any]] = []
            for raw in tasks:
                if _task_name(raw) != name:
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
            tasks = payload["tasks"]
            kept = []
            for raw in tasks:
                if str(raw.get("id")) != task_id:
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

    def _read_checkin_config(self) -> dict[str, Any]:
        path = self._checkin_config_path()
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            # A registered check-in job normally means the plugin has already
            # created this file.  Keep a valid disabled configuration for old
            # installations too, otherwise plugin startup seeds an enabled
            # default task and makes a deleted job reappear.
            payload = {"version": 1, "accounts": [], "schedule": {}}
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ValueError("签到定时配置读取失败，请先在签到设置中重新保存。") from exc
        if not isinstance(payload, dict):
            raise ValueError("签到定时配置格式不正确，请先在签到设置中重新保存。")
        return payload

    def _update_checkin_config(
        self,
        *,
        name: str,
        enabled: bool | None = None,
        clock: tuple[int, int] | None = None,
        delete: bool = False,
    ) -> None:
        payload = self._read_checkin_config()
        schedule = payload.get("schedule")
        schedule = dict(schedule) if isinstance(schedule, dict) else {}
        session = str(schedule.get("session") or "")
        if session and name != f"checkin_{session}":
            # A stale target-less cron row must not disable the valid schedule
            # the user configured after that row was originally seeded.
            return
        if delete:
            schedule["enabled"] = False
        elif enabled is not None:
            schedule["enabled"] = enabled
        if clock is not None:
            schedule["time"] = f"{clock[0]:02d}:{clock[1]:02d}"
        payload["schedule"] = schedule
        accounts = payload.get("accounts")
        if not isinstance(accounts, list):
            payload["accounts"] = []
        payload.setdefault("version", 1)
        write_json_atomic(self._checkin_config_path(), payload)

    def set_enabled(self, job_id: str, enabled: bool) -> dict[str, object]:
        row = self._job(job_id)
        self._validate_source(str(row["name"]))
        online = self._engine_update(job_id, {"enabled": enabled})
        self._job(job_id)
        self._update_source(str(row["name"]), enabled=enabled)
        if not online:
            self._offline_update(
                job_id,
                enabled=int(enabled),
                status="scheduled" if enabled else "paused",
                next_run_time=None,
            )
        return {"updated": True, "enabled": enabled, "applied": online,
                "message": "任务状态已生效。" if online else "任务状态已保存，启动机器人后使用。"}

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
        self._validate_source(str(row["name"]))
        online = self._engine_update(job_id, {"cron_expression": expression})
        self._job(job_id)
        self._update_source(str(row["name"]), clock=(hour, minute))
        if not online:
            self._offline_update(job_id, cron_expression=expression, next_run_time=None)
        return {"updated": True, "cron_expression": expression, "applied": online,
                "message": "执行时间已生效。" if online else "执行时间已保存，启动机器人后使用。"}

    def delete(self, job_id: str) -> dict[str, object]:
        row = self._job(job_id)
        self._validate_source(str(row["name"]))
        deleted_online = self.engine.is_available()
        self._authorize_job(row)
        if deleted_online:
            self.engine.request("DELETE", f"/api/v1/cron/jobs/{quote(job_id, safe='')}")
        self._update_source(str(row["name"]), delete=True)
        if not deleted_online:
            with self._connect() as connection:
                connection.execute("DELETE FROM cron_jobs WHERE job_id = ?", (job_id,))
        return {"deleted": True}


__all__ = ["ScheduleItem", "ScheduleService"]
