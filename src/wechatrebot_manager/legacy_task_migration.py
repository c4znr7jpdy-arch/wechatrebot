"""Explicit, recoverable transfer of owned legacy schedules to paused tasks."""
from __future__ import annotations

from contextlib import closing, contextmanager
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import secrets
import sqlite3
from typing import Callable, Iterator

from .json_io import write_json_atomic
from .payload import _checked_target
from .schedules import ScheduleService, _source_kind, _task_name
from .single_instance import SingleInstanceLock
from .task_store import ACTION_NAMES, TaskStore, normalize_task, schedule_text, validate_account


_JOURNALS = Path("data/manager/legacy-task-migrations")
_FIELDS = ("job_id", "name", "job_type", "cron_expression", "timezone", "payload",
           "enabled", "persistent", "run_once")
_DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_SUPPORTED = {"weather", "news", "oilprice", "kfc", "short_drama"}


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _job_payload(job: dict) -> dict:
    if (not isinstance(job, dict) or not set(_FIELDS) <= job.keys()
            or any(not isinstance(job[field], str) or not job[field] for field in _FIELDS[:6])
            or any(type(job[field]) is not int or job[field] not in (0, 1) for field in _FIELDS[6:])):
        raise ValueError("旧任务记录不完整，无法安全迁移。")
    payload = json.loads(job["payload"])
    if not isinstance(payload, dict):
        raise ValueError("旧任务参数格式不正确。")
    return payload


def _read_journal(path: Path) -> dict:
    record = json.loads(path.read_text("utf-8"))
    key = path.stem
    if (not re.fullmatch(r"[a-f0-9]{64}", key) or not isinstance(record, dict)
            or record.get("schema_version") != 1 or record.get("key") != key
            or record.get("phase") not in ("confirmed", "prepared", "complete")):
        raise ValueError("迁移恢复记录不完整，请保留原数据并检查后重试。")
    plan = record.get("plan")
    if (not isinstance(plan, dict) or _digest(plan) != record.get("fingerprint")
            or any(not isinstance(plan.get(field), dict) for field in ("job", "source", "task"))):
        raise ValueError("迁移恢复记录校验失败，请保留原数据。")
    if (plan.get("account_id") != plan["task"].get("account_id")
            or key != _digest([plan.get("account_id"), plan["job"].get("job_id")])):
        raise ValueError("迁移恢复记录的账号不一致。")
    # Reconstruct the destination from its original source, including ownership.
    # A recovery record cannot supply paths or enable a task.
    if LegacyTaskMigration._convert(plan["job"], plan["source"]) != plan["task"]:
        raise ValueError("迁移恢复记录的任务内容不一致。")
    return record


def has_pending_migrations(root: Path) -> bool:
    directory = _checked_target(root, _JOURNALS, allow_directory=True)
    for path in directory.glob("*.json"):
        path = _checked_target(root, path.relative_to(root))
        record = _read_journal(path)
        if record["phase"] == "prepared":
            return True
    return False


class LegacyTaskMigration:
    def __init__(self, legacy: ScheduleService, store: TaskStore, *,
                 stop: Callable[[], bool], stopped: Callable[[], bool]):
        self.legacy = legacy
        self.root = legacy.install_root
        self.store = store
        self.stop = stop
        self.stopped = stopped
        self._previews: dict[str, dict] = {}

    def _path(self, key: str) -> Path:
        if not isinstance(key, str) or not re.fullmatch(r"[a-f0-9]{64}", key):
            raise ValueError("迁移记录标识无效，请刷新任务。")
        return _checked_target(self.root, _JOURNALS / (key + ".json"))

    def _journal(self, key: str) -> dict | None:
        path = self._path(key)
        if not path.exists():
            return None
        return _read_journal(path)

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        lock = SingleInstanceLock(self.root / _JOURNALS)
        if not lock.acquire():
            raise ValueError("正在处理另一项迁移，请稍后重试。")
        try:
            yield
        finally:
            lock.release()

    def _account(self) -> str:
        try:
            return str(self.legacy._account_hint() or "")
        except OSError:
            return ""

    def _authorize(self, account: str, *, recovery: bool = False) -> None:
        validate_account(account)
        current = self._account()
        if current == account:
            return
        if not current and recovery and self.stopped():
            return  # Only an already confirmed, fixed-owner journal may resume offline.
        raise ValueError("微信账号已变化或尚未确认，请刷新后重试。")

    def _source_path(self, kind: str) -> Path:
        path = self.legacy._drama_tasks_path() if kind == "short_drama" else self.legacy._schedule_tasks_path()
        return _checked_target(self.root, path.relative_to(self.root))

    @staticmethod
    def _matches(raw: dict, job: dict) -> bool:
        kind = _source_kind(job["name"])
        if kind == "short_drama":
            return job["name"] == "short_drama_recommend_" + str(raw.get("id", ""))
        return raw.get("type") == kind and _task_name(raw) == job["name"]

    def _jobs(self, job_id: str = "", name: str = "") -> list[dict]:
        path = _checked_target(self.root, self.legacy.database.relative_to(self.root))
        if not path.is_file():
            return []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute("PRAGMA table_info(cron_jobs)")}
            if not set(_FIELDS) <= columns:
                raise ValueError("旧任务缺少类型或时区信息，无法安全迁移。")
            rows = connection.execute(
                "SELECT " + ",".join(_FIELDS) + " FROM cron_jobs WHERE " + ("name=?" if name else "job_id=?"),
                (name or job_id,),
            )
            return [dict(row) for row in rows]

    @staticmethod
    def _convert(job: dict, source: dict) -> dict:
        payload = _job_payload(job)
        kind = _source_kind(job["name"])
        if kind not in _SUPPORTED:
            raise ValueError("该旧任务需要专用适配，请继续使用原功能管理。")
        if (job["job_type"] != "basic" or job["timezone"] != "Asia/Shanghai"
                or job["persistent"] != 1 or job["run_once"] != 0):
            raise ValueError("仅支持北京时间的固定重复任务迁移。")
        account = validate_account(payload.get("account_id"))
        if source.get("account_id", account) != account or not LegacyTaskMigration._matches(source, job):
            raise ValueError("旧任务来源或账号不一致。")
        if type(source.get("enabled", True)) is not bool or job["enabled"] not in (0, 1):
            raise ValueError("旧任务启停状态不正确。")
        if bool(job["enabled"]) != source.get("enabled", True):
            raise ValueError("旧任务与来源的启停状态不一致，请先核对。")
        extra = source.get("extra", {})
        if not isinstance(extra, dict):
            raise ValueError("旧任务参数格式不正确。")
        session = source.get("session")
        match = re.fullmatch(r"[A-Za-z0-9_.-]+:(GroupMessage|FriendMessage):([A-Za-z0-9_.@-]{1,128})", str(session))
        if not match or (match[1] == "GroupMessage") != match[2].endswith("@chatroom"):
            raise ValueError("旧任务发送对象不明确，无法自动转换。")
        if kind == "short_drama":
            if (not isinstance(source.get("id"), str) or not source["id"]
                    or payload.get("task_id") != source["id"]):
                raise ValueError("旧影视计划与来源记录不一致。")
            arguments = {"media_type": source.get("media_type"), "limit": source.get("limit")}
            days = source.get("days", [])
        else:
            if payload.get("session") != session or source.get("type") != kind:
                raise ValueError("旧任务发送对象或内容不一致。")
            argument = {"weather": "city", "oilprice": "province"}.get(kind)
            if argument and (not isinstance(payload.get(argument), str) or payload.get(argument) != extra.get(argument)):
                raise ValueError("旧任务与来源的查询参数不一致，请先核对。")
            arguments = ({argument: payload.get(argument)} if argument else {"count": 20} if kind == "news" else {})
            days = str(extra.get("day_of_week") or "").split(",") if extra.get("day_of_week") else []
        if not isinstance(days, list) or any(day not in _DAYS for day in days):
            raise ValueError("旧任务星期设置暂不支持转换。")
        hour, minute = source.get("hour"), source.get("minute")
        if type(hour) is not int or type(minute) is not int or not 0 <= hour < 24 or not 0 <= minute < 60:
            raise ValueError("旧任务来源缺少有效的执行时间。")
        parts = str(job["cron_expression"]).split()
        if len(parts) != 5 or parts[2:4] != ["*", "*"] or parts[:2] != [str(minute), str(hour)]:
            raise ValueError("旧任务与来源时间不一致，或使用了暂不支持的时间规则。")
        if set(parts[4].split(",")) != set(days or ["*"]):
            raise ValueError("旧任务与来源的执行星期不一致。")
        schedule = {"kind": "weekly" if days else "daily", "time": f"{hour:02d}:{minute:02d}",
                    "timezone": "Asia/Shanghai"}
        if days:
            schedule["weekdays"] = sorted({_DAYS.index(day) for day in days})
        task = normalize_task({"id": "legacy_" + _digest([account, job["job_id"]])[:40],
                               "account_id": account, "name": source.get("name") or ACTION_NAMES[kind],
                               "action": kind, "arguments": arguments,
                               "targets": [{"type": "group" if match[1] == "GroupMessage" else "private", "id": match[2]}],
                               "schedule": schedule, "enabled": False, "draft": False})
        task["draft"] = True
        return task

    def _fresh_plan(self, job_id: str, *, confirmed_account: str = "") -> dict:
        rows = self._jobs(job_id)
        if len(rows) != 1:
            raise ValueError("没有找到唯一的旧任务，请刷新列表。")
        job = rows[0]
        payload = _job_payload(job)
        if _source_kind(job["name"]) not in _SUPPORTED:
            raise ValueError("该旧任务需要专用适配，请继续使用原功能管理。")
        account = payload.get("account_id")
        if confirmed_account and account != confirmed_account:
            raise ValueError("旧任务账号已改变，不能沿用上次迁移确认。")
        self._authorize(account, recovery=bool(confirmed_account))
        if len(self._jobs(name=job["name"])) != 1:
            raise ValueError("同一来源有多条旧任务，请先核对重复或账号归属。")
        source = self.legacy._read_tasks(self._source_path(_source_kind(job["name"])))
        matches = [raw for raw in source["tasks"] if self._matches(raw, job)]
        if len(matches) != 1:
            raise ValueError("旧任务与来源无法唯一对应，请先核对原配置。")
        return {"account_id": account, "job": job, "source": matches[0], "task": self._convert(job, matches[0])}

    @staticmethod
    def _summary(record: dict) -> dict:
        task = record["plan"]["task"]
        return {"key": record["key"], "job_id": record["plan"]["job"]["job_id"],
                "account_id": task["account_id"], "name": task["name"],
                "action": task["action"],
                "targets": task["targets"], "arguments": task["arguments"],
                "schedule_text": schedule_text(task["schedule"]),
                "can_cancel": record["phase"] == "confirmed",
                "previously_enabled": bool(record["plan"]["job"]["enabled"])}

    def cancel(self, key: str) -> dict:
        with self._exclusive():
            record = self._journal(key)
            if not record or record["phase"] != "confirmed":
                raise ValueError("任务已经开始迁移，请继续完成恢复，不能丢弃记录。")
            self._authorize(record["plan"]["account_id"], recovery=True)
            self._path(key).unlink()
            self._previews.clear()
        return {"cancelled": True, "message": "迁移确认已取消，原任务未改变。"}

    def pending(self) -> list[dict]:
        directory = _checked_target(self.root, _JOURNALS, allow_directory=True)
        result = []
        account = self._account()
        for path in directory.glob("*.json"):
            record = self._journal(path.stem)
            if record and record["phase"] != "complete":
                if account and account != record["plan"]["account_id"]:
                    continue
                self._authorize(record["plan"]["account_id"], recovery=True)
                result.append(self._summary(record))
        return result

    def preview(self, job_id: str, recovery_key: str = "") -> dict:
        if not isinstance(job_id, str) or not isinstance(recovery_key, str):
            raise ValueError("请重新选择需要迁移的旧任务。")
        record = self._journal(recovery_key) if recovery_key else None
        if record:
            self._authorize(record["plan"]["account_id"], recovery=True)
            if record["phase"] == "confirmed":
                record["plan"] = self._fresh_plan(record["plan"]["job"]["job_id"], confirmed_account=record["plan"]["account_id"])
                record["fingerprint"] = _digest(record["plan"])
        else:
            plan = self._fresh_plan(job_id)
            key = _digest([plan["account_id"], job_id])
            record = {"schema_version": 1, "key": key, "phase": "confirmed", "plan": plan, "fingerprint": _digest(plan)}
            saved = self._journal(key)
            if saved and saved["phase"] == "complete":
                raise ValueError("这项旧任务已有迁移记录，请先核对原任务是否被重新创建。")
            if saved and saved["phase"] == "prepared":
                record = saved
        token = secrets.token_urlsafe(24)
        self._previews.clear()
        self._previews[token] = deepcopy(record)
        return {**self._summary(record), "token": token,
                "message": "迁移会停止机器人，保留原目标和时间，并转为暂停草稿；核对后再启用。"}

    def commit(self, token: str) -> dict:
        if not isinstance(token, str):
            raise ValueError("请重新预览并确认这项旧任务。")
        record = deepcopy(self._previews.get(token))
        if not record:
            raise ValueError("请重新预览并确认这项旧任务。")
        with self._exclusive():
            saved = self._journal(record["key"])
            if saved and saved["phase"] != "confirmed" and saved["fingerprint"] != record["fingerprint"]:
                raise ValueError("迁移内容已改变，请重新预览。")
            self._authorize(record["plan"]["account_id"], recovery=saved is not None)
            if saved and saved["phase"] == "complete":
                return self._result(saved)
            if saved and saved["phase"] == "prepared":
                record = saved
            if not saved or saved["phase"] == "confirmed":
                if self._fresh_plan(record["plan"]["job"]["job_id"], confirmed_account=record["plan"]["account_id"] if saved else "") != record["plan"]:
                    raise ValueError("旧任务已改变，请重新预览后再迁移。")
                if self._destination(record["plan"]) is not None:
                    raise ValueError("目标任务标识已被使用，旧任务尚未修改。")
                write_json_atomic(self._path(record["key"]), record)
            if not self.stop() or not self.stopped():
                return {"pending": True, "message": "请先关闭机器人微信，停止完成后点击继续迁移。"}
            self._transfer(record)
            return self._result(record)

    def _destination(self, plan: dict) -> dict | None:
        path = _checked_target(self.root, self.store.path.relative_to(self.root))
        if not path.is_file():
            return None
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduled_tasks'").fetchone():
                return None
            row = connection.execute("SELECT * FROM scheduled_tasks WHERE id=?", (plan["task"]["id"],)).fetchone()
        if row and row["account_id"] != plan["account_id"]:
            raise ValueError("目标任务标识已被其他账号使用，迁移已暂停。")
        return dict(row) if row else None

    def _result(self, record: dict) -> dict:
        row = self._destination(record["plan"])
        if row and row["deleted"]:
            return {"deleted": True, "message": "任务已迁移并随后被删除，未重新创建。"}
        return {"item": self.store.get(record["plan"]["account_id"], record["plan"]["task"]["id"]),
                "message": "旧任务已迁入草稿，保留当前编辑结果；机器人不会自动启动。"}

    def _transfer(self, record: dict) -> None:
        plan = record["plan"]
        job = plan["job"]
        path = self._source_path(_source_kind(job["name"]))
        payload = self.legacy._read_tasks(path)
        matches = [raw for raw in payload["tasks"] if self._matches(raw, job)]
        if matches and matches != [plan["source"]]:
            raise ValueError("旧任务来源在迁移期间已改变，已保留数据，请核对后处理。")
        database = _checked_target(self.root, self.legacy.database.relative_to(self.root))
        # mode=rw refuses to create an empty replacement if the database vanished.
        with closing(sqlite3.connect(database.as_uri() + "?mode=rw", uri=True, timeout=5)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            existing = [dict(row) for row in connection.execute("SELECT " + ",".join(_FIELDS) + " FROM cron_jobs WHERE job_id=? OR name=?", (job["job_id"], job["name"]))]
            if existing and existing != [job]:
                raise ValueError("旧调度记录已改变，迁移已暂停。")
            destination = self._destination(plan)
            if destination and (matches or existing):
                raise ValueError("目标任务标识已被使用，旧任务尚未修改。")
            if record["phase"] == "confirmed" and (not matches or not existing):
                raise ValueError("旧任务在停止期间已改变，请重新预览。")
            # Database lock waits and disk reads can outlive the first stop
            # probe. Recheck ownership and process absence before any cleanup.
            self._authorize(plan["account_id"], recovery=True)
            if not self.stopped():
                raise ValueError("无法确认机器人已经停止，请停止后继续迁移。")
            record["phase"] = "prepared"
            write_json_atomic(self._path(record["key"]), record)
            # No destination becomes visible before every legacy producer is gone.
            if matches:
                payload["tasks"] = [raw for raw in payload["tasks"] if not self._matches(raw, job)]
                write_json_atomic(path, payload)
            connection.execute("DELETE FROM cron_jobs WHERE job_id=?", (job["job_id"],))
        if destination is None:
            self.store.save(plan["task"])
        record["phase"] = "complete"
        write_json_atomic(self._path(record["key"]), record)
