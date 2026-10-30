"""Actionable run-center reminders with manager-session and durable dismissal."""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from uuid import uuid4

from .activity_service import ActivityService
from .customer_logs import log_timestamp
from .json_io import write_json_atomic
from .task_store import SCHEDULED_COMMAND_VERSION


WINDOW_SECONDS = 60 * 60
_PREFERENCES_LOCK = threading.RLock()
_ENGINE_SOURCES = frozenset({"engine-runtime", "engine-service"})
_MEMORY_LOG = re.compile(
    r"^\[[^\]\r\n]+\]\s+\[Core\]\s+\[(?:ERRO|ERROR|WARN|WARNING)\]"
    r"(?:\s+\[v[^\]\r\n]+\])?\s+\[core\.plugin_initializer(?:_finalize)?:\d+\]:"
    r"[^\r\n]*(?:向量模型连接失败|未返回有效向量)", re.M,
)
_MEMORY_RECOVERED = re.compile(
    r"^\[[^\]\r\n]+\]\s+\[Core\]\s+\[INFO\]"
    r"(?:\s+\[v[^\]\r\n]+\])?\s+\[core\.plugin_initializer_finalize:\d+\]:"
    r"\s*LivingMemory 插件初始化成功。\s*$", re.M,
)

# This catalog is also sent to the UI, including muted categories without a
# recent event. A generic error code such as 19000 is never a mute key.
ALERT_CATEGORIES = {
    "memory_initialization": {
        "title": "长期记忆初始化失败", "level": "warning", "action": "models",
        "action_label": "检查向量模型",
        "message": "向量模型连接失败，长期记忆未能完成初始化。",
    },
    "model_service_unavailable": {
        "title": "模型服务暂不可用", "level": "warning", "action": "models",
        "action_label": "检查模型服务",
        "message": "最近调用的模型服务出现鉴权、额度、接口或返回内容异常，请检查受影响的模型服务。",
    },
    "model_requests_unstable": {
        "title": "模型响应不稳定", "level": "warning", "action": "models",
        "action_label": "检查模型服务",
        "message": "最近多次模型请求超时、受限或连接失败，部分请求可能未完成。",
    },
    "wechat_connection_interrupted": {
        "title": "微信连接已中断", "level": "error", "action": "status",
        "action_label": "检查连接状态",
        "message": "已确认在线的微信连接持续中断，消息收发可能受到影响。",
    },
    "robot_service_stopped": {
        "title": "机器人服务意外停止", "level": "error", "action": "activity",
        "action_label": "查看运行记录",
        "message": "托管的机器人服务意外退出，请查看原因后手动重新启动。",
    },
    "message_delivery_problem": {
        "title": "消息发送需要检查", "level": "warning", "action": "activity",
        "action_label": "查看发送记录",
        "message": "最近有消息发送失败或结果待确认，请先核对聊天记录，避免重复发送。",
    },
    "scheduled_task_blocked": {
        "title": "定时任务需要处理", "level": "warning", "action": "schedules",
        "action_label": "检查定时任务",
        "message": "有定时任务因配置、账号或依赖问题等待处理，请查看任务与执行记录。",
    },
}


def _category(item: Mapping) -> str:
    if item.get("level") not in {"error", "warning"}:
        return ""
    source, code = item.get("source"), str(item.get("code") or "")
    if source in _ENGINE_SOURCES:
        if _MEMORY_LOG.search(str(item.get("message") or "")):
            return "memory_initialization"
        if code in {"16002", "16003", "16005", "16009", "16010", "16012"}:
            return "model_service_unavailable"
        if code in {"16004", "16006", "16007", "16008"}:
            return "model_requests_unstable"
    if code in {"17001", "17002"}:
        return "message_delivery_problem"
    if code == "18004":
        return "scheduled_task_blocked"
    return ""


class RuntimeAlertsService:
    def __init__(self, root: str | Path, *, build: int = 0,
                 clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic) -> None:
        self.root = Path(root).resolve()
        self.activity = ActivityService(self.root, build=build)
        self.path = self.root / "data/manager/runtime-alert-preferences.json"
        self.session_id = uuid4().hex
        self._clock, self._monotonic = clock, monotonic
        self._lock = threading.RLock()
        self._closed: dict[str, dict] = {}
        self._items: list[dict] = []
        self._live_started: dict[str, float] = {}
        self._run_started_at = 0.0
        self._observed_online = False
        self._disconnected_since: float | None = None

    def _now(self) -> str:
        return datetime.fromtimestamp(self._clock(), timezone.utc).astimezone().isoformat(timespec="seconds")

    def _read_preferences(self) -> dict:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"schema_version": 1, "muted": {}}
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ValueError("提醒设置文件无法读取，请检查后重试。") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("muted", {}), dict):
            raise ValueError("提醒设置格式无效，请检查后重试。")
        for category, at in payload.get("muted", {}).items():
            if category in ALERT_CATEGORIES and (not isinstance(at, str) or log_timestamp(at) is None):
                raise ValueError("提醒设置的操作时间无效，请检查后重试。")
        return payload

    def _alert(self, category: str, records: list[dict], *, active: bool = False) -> dict:
        definition = ALERT_CATEGORIES[category]
        latest = max(records, key=lambda item: item["timestamp"]) if records else {}
        detected = self._live_started.setdefault(category, self._clock()) if active else self._clock()
        timestamp = latest.get("timestamp", detected)
        time_text = str(latest.get("time") or datetime.fromtimestamp(timestamp, timezone.utc).astimezone().isoformat(timespec="seconds"))
        item = {
            "id": f"{category}:{self.session_id}",
            "category": category, **definition,
            "state": "active" if active else "recovery_unconfirmed",
            "time": time_text, "timestamp": timestamp,
            "first_time": min((str(entry.get("first_time") or entry["time"]) for entry in records), default=time_text),
            "count": sum(entry.get("count", 1) for entry in records) or 1,
            "detail": str(latest.get("detail") or definition["message"]),
            "report": str(latest.get("report") or ""),
            "closed_at": "", "muted_at": "", "hidden": False,
        }
        if not item["report"]:
            item["report"] = f"机器人问题报告\n标题：{item['title']}\n发生时间：{time_text}\n来源：运行状态检查\n说明：{item['message']}"
        return item

    def _recent(self, records: Iterable[dict]) -> list[dict]:
        now = self._clock()
        return [item for item in records if isinstance(item, dict)
                and isinstance(item.get("timestamp"), (int, float))
                and not isinstance(item["timestamp"], bool)
                and 0 <= now - item["timestamp"] <= WINDOW_SECONDS]

    def _log_alerts(self, records: list[dict]) -> list[dict]:
        grouped: dict[str, list[dict]] = {}
        recovered = max((item["timestamp"] for item in records
                         if item.get("source") in _ENGINE_SOURCES
                         and item.get("level") == "info"
                         and _MEMORY_RECOVERED.search(str(item.get("message") or ""))), default=0)
        for item in records:
            category = _category(item)
            if category:
                grouped.setdefault(category, []).append(item)
        memory = grouped.get("memory_initialization", [])
        if memory and recovered >= max(item["timestamp"] for item in memory):
            grouped.pop("memory_initialization")
        cutoff = self._clock() - 5 * 60
        unstable = []
        for item in grouped.get("model_requests_unstable", []):
            if item["timestamp"] < cutoff:
                continue
            first = log_timestamp(str(item.get("first_time") or ""))
            # Merged records spanning the boundary cannot prove how many
            # retries occurred in this window; only the latest one is certain.
            unstable.append(item if first is not None and first >= cutoff
                            else {**item, "count": 1, "first_time": item["time"]})
        if sum(item.get("count", 1) for item in unstable) < 3:
            grouped.pop("model_requests_unstable", None)
        else:
            grouped["model_requests_unstable"] = unstable
        deliveries = grouped.get("message_delivery_problem", [])
        if not any(item.get("code") == "17002" for item in deliveries) and sum(item.get("count", 1) for item in deliveries) < 2:
            grouped.pop("message_delivery_problem", None)
        return [self._alert(category, entries) for category, entries in grouped.items()]

    def _live_alerts(self, view: Mapping, records: list[dict]) -> list[dict]:
        state = str(view.get("state") or "")
        error = view.get("error") if isinstance(view.get("error"), dict) else {}
        code = str(error.get("code") or "")
        if view.get("online") is True:
            self._observed_online, self._disconnected_since = True, None
            self._live_started.pop("wechat_connection_interrupted", None)
            self._live_started.pop("robot_service_stopped", None)
        elif state in {"STOPPED", "STOPPING", "STOP_WAIT_WECHAT", "LICENSE_PAUSED", "LICENSE_READY"}:
            self._observed_online, self._disconnected_since = False, None
            self._live_started.pop("wechat_connection_interrupted", None)
            self._live_started.pop("robot_service_stopped", None)
        connection = state == "ERROR" and code in {"11005", "12004"}
        if self._observed_online and state in {"WAITING_WECHAT", "VERIFYING_CONNECTION"}:
            if self._disconnected_since is None:
                self._disconnected_since = self._monotonic()
            connection = self._monotonic() - self._disconnected_since >= 10
        items = [self._alert("wechat_connection_interrupted", [], active=True)] if connection else []
        stopped = state == "ERROR" and (code == "13004" or (code == "13002" and any(
            item.get("source") == "manager" and item.get("code") == "13002"
            and "托管服务意外退出" in str(item.get("message") or "") for item in records)))
        if stopped:
            matches = [item for item in records if item.get("source") == "manager" and item.get("code") == code]
            items.append(self._alert("robot_service_stopped", matches, active=True))
        return items

    def snapshot(self, view: Mapping | None = None, *, records: Iterable[dict] | None = None,
                 tasks: Iterable[dict] = ()) -> dict:
        with self._lock:
            recent = self._recent(self.activity.recent() if records is None else records)
            run_started = max((item["timestamp"] for item in recent
                               if item.get("source") == "manager" and item.get("event") == "CORE_START"), default=0)
            if run_started > self._run_started_at:
                self._run_started_at = run_started
                self._observed_online, self._disconnected_since = False, None
                self._live_started.clear()
            recent = [item for item in recent if item["timestamp"] >= self._run_started_at]
            items = self._log_alerts(recent)
            items.extend(self._live_alerts(view or {}, recent))
            blocked = [task for task in tasks if isinstance(task, dict) and task.get("enabled") is True
                       and (task.get("status") in {"blocked", "needs_review", "waiting_account"}
                            or task.get("definition_version", 0) != SCHEDULED_COMMAND_VERSION)]
            if blocked:
                items = [item for item in items if item["category"] != "scheduled_task_blocked"]
                task_alert = self._alert("scheduled_task_blocked", [], active=True)
                task_alert["count"] = len(blocked)
                items.append(task_alert)
            else:
                self._live_started.pop("scheduled_task_blocked", None)
            self._items = items
            return self._response()

    def _response(self) -> dict:
        with _PREFERENCES_LOCK:
            muted = self._read_preferences().get("muted", {})
        items = []
        for raw in self._items:
            item = dict(raw)
            item["closed_at"] = self._closed.get(item["id"], {}).get("closed_at", "")
            item["muted_at"] = muted.get(item["category"], "")
            item["hidden"] = bool(item["closed_at"] or item["muted_at"])
            items.append(item)
        items.sort(key=lambda item: (item["level"] != "error", -item["timestamp"]))
        return {
            "items": items,
            "categories": [{"id": category, "title": definition["title"], "muted_at": muted.get(category, "")}
                           for category, definition in ALERT_CATEGORIES.items()],
            "closed": [dict(item) for item in self._closed.values()],
            "session_id": self.session_id, "window_seconds": WINDOW_SECONDS, "updated_at": self._now(),
        }

    def set_preference(self, category: str, action: str, alert_id: str = "") -> dict:
        if not isinstance(category, str) or category not in ALERT_CATEGORIES:
            raise ValueError("提醒类别无效，请刷新后重试。")
        if not isinstance(action, str) or action not in {"close", "mute", "reopen", "restore"}:
            raise ValueError("提醒操作无效，请刷新后重试。")
        if not isinstance(alert_id, str):
            raise ValueError("提醒标识无效，请刷新后重试。")
        with self._lock:
            if action in {"close", "reopen"}:
                if action == "close" and not any(item["id"] == alert_id and item["category"] == category for item in self._items):
                    raise ValueError("这条提醒已变化，请刷新后重试。")
                if action == "reopen" and self._closed.get(alert_id, {}).get("category") != category:
                    raise ValueError("没有找到对应的本次关闭记录。")
                # Check durable settings first; a malformed file must not hide
                # an alert and then return a misleading save failure.
                self._response()
                if action == "close":
                    self._closed.setdefault(alert_id, {"id": alert_id, "category": category, "closed_at": self._now()})
                else:
                    self._closed.pop(alert_id, None)
            else:
                with _PREFERENCES_LOCK:
                    payload = self._read_preferences()
                    muted = dict(payload.get("muted", {}))
                    if action == "mute":
                        muted.setdefault(category, self._now())
                    else:
                        muted.pop(category, None)
                    write_json_atomic(self.path, {**payload, "schema_version": 1, "muted": muted})
            return self._response()
