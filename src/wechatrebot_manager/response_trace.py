"""Project correlated reply events without returning log bodies or identities."""
from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
from typing import Any

from .customer_logs import log_sources, read_log_tail, strip_log_ansi


_TRACE_LINE = re.compile(
    r"^\[(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}(?:\.\d+)?\] "
    r"\[Core\] \[INFO\] \[core\.wechat_reply_trace:\d+\]: WRB_REPLY_TRACE (\{.*\})$"
)
_LEGACY_SEND = re.compile(
    r"^\[(?P<time>(?:\d{4}-\d{2}-\d{2} )?\d{2}:\d{2}:\d{2}(?:\.\d+)?)\] "
    r"(?:\[Core\] )?\[INFO\] \[respond\.stage(?::\d+)?\]: Prepare to send\b"
)
_LABELS = {"received": "收到消息", "decision": "回复判断", "model": "模型请求", "send": "提交微信接口", "pipeline": "回复处理"}
_STATUSES = {"pending", "success", "failed", "cancelled", "skipped"}


def _duration_text(seconds: float) -> str:
    return "<0.1 秒" if seconds < 0.1 else f"{seconds:.1f} 秒"


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def _event(line: str) -> dict[str, Any] | None:
    match = _TRACE_LINE.fullmatch(line)
    if match is None:
        return None
    try:
        event = json.loads(match[1])
        if not isinstance(event, dict) or type(event.get("schema")) is not int or event["schema"] != 1:
            return None
        if not re.fullmatch(r"[0-9a-f]{32}", str(event.get("trace_id", ""))):
            return None
        if (event.get("stage") not in _LABELS or event.get("kind") not in {"group", "private"}
                or event.get("status") not in _STATUSES or event.get("phase") not in {"point", "begin", "end"}):
            return None
        if (type(event.get("sequence")) is not int or not 1 <= event["sequence"] <= 512
                or type(event.get("operation")) is not int or not 0 <= event["operation"] <= 512
                or not _number(event.get("elapsed_ms"))):
            return None
        timestamp = datetime.fromisoformat(event["time"])
        if timestamp.tzinfo is None:
            return None
        if event["phase"] == "point":
            if event["stage"] != "received" or event["operation"] != 0 or event["status"] != "success":
                return None
        elif event["operation"] < 1 or event["stage"] == "received":
            return None
        elif event["phase"] == "begin":
            if event["status"] != "pending":
                return None
        elif (event["status"] == "pending" or not _number(event.get("duration_ms"))
              or event["duration_ms"] > event["elapsed_ms"] + 1):
            return None
        return event
    except (ValueError, TypeError, KeyError, OverflowError):
        return None


def _project(events: list[dict[str, Any]]) -> dict[str, Any]:
    # Grouping is by the event's random identifier, never log adjacency or chat.
    ordered = sorted(events, key=lambda value: value["sequence"])
    sequences = [value["sequence"] for value in ordered]
    contiguous = (sequences == list(range(1, len(ordered) + 1))
                  and len({value["kind"] for value in ordered}) == 1
                  and all(left["elapsed_ms"] <= right["elapsed_ms"]
                          for left, right in zip(ordered, ordered[1:])))
    received = next((value for value in ordered if value["stage"] == "received"), None)
    begins = {(value["stage"], value["operation"]): value for value in ordered if value["phase"] == "begin"}
    ends = {(value["stage"], value["operation"]): value for value in ordered if value["phase"] == "end"}
    stages = []
    if received:
        stages.append({"code": "received", "label": _LABELS["received"], "status": "success",
                       "offset_seconds": received["elapsed_ms"] / 1000})
    for key in dict.fromkeys((*begins, *ends)):
        stage, _operation = key
        begin, end = begins.get(key), ends.get(key)
        if stage == "pipeline" and end and end["status"] == "success":
            continue
        status = end["status"] if end else "pending"
        item = {"code": stage, "label": _LABELS[stage],
                "status": "skipped" if status == "cancelled" else status}
        if status == "cancelled":
            item["detail"] = "任务已取消"
        elif stage == "send" and status == "success":
            item["detail"] = "微信接口已接受；不代表对方已收到"
        if begin:
            item["offset_seconds"] = begin["elapsed_ms"] / 1000
        if begin and end and end["sequence"] > begin["sequence"] and end["elapsed_ms"] >= begin["elapsed_ms"]:
            duration = end["duration_ms"] / 1000
            item.update(duration_seconds=duration, duration_text=_duration_text(duration))
        else:
            item["duration_text"] = "耗时未知"
        stages.append(item)
    stages.sort(key=lambda value: value.get("offset_seconds", float("inf")))
    pipeline = next((value for value in reversed(ordered) if value["stage"] == "pipeline" and value["phase"] == "end"), None)
    complete = bool(received and contiguous and pipeline and pipeline is ordered[-1]
                    and begins.keys() == ends.keys())
    duration = pipeline["elapsed_ms"] / 1000 if complete else None
    status = pipeline["status"] if pipeline else "pending"
    sends = [value for value in ends.values() if value["stage"] == "send"]
    if any(value["status"] == "failed" for value in sends):
        status = "failed"
    elif status == "skipped" and sends and all(value["status"] == "success" for value in sends):
        # Exclusive command handlers stop propagation after their valid send.
        status = "success"
    elif status == "success" and not sends:
        status = "skipped"
    if not complete and status == "success":
        status = "pending"
    return {
        "trace_id": ordered[0]["trace_id"], "time": ordered[-1]["time"],
        "title": "群聊回复" if ordered[0]["kind"] == "group" else "私聊回复",
        "status": status, "complete": complete,
        "duration_seconds": duration,
        "duration_text": _duration_text(duration) if duration is not None else "耗时未知",
        "stages": stages,
    }


class ResponseTraceService:
    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = next(source.path for source in log_sources(self.install_root) if source.key == "engine-runtime")

    def recent(self, limit: int = 12) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        try:
            text, _truncated = read_log_tail(self.path, max_bytes=1024 * 1024, max_lines=3000)
        except FileNotFoundError:
            return []
        lines = strip_log_ansi(text).splitlines()
        groups: dict[str, list[dict[str, Any]]] = {}
        legacy = []
        for line in lines:
            event = _event(line)
            if event:
                groups.setdefault(event["trace_id"], []).append(event)
            elif match := _LEGACY_SEND.match(line):
                # This one record proves only that send preparation was reached.
                # Adjacent receive/completion lines may belong to another reply.
                timestamp = match["time"]
                legacy.append({
                    "time": timestamp.replace(" ", "T") if len(timestamp) > 15 else "",
                    "title": "历史回复（关联信息不足）", "status": "pending", "complete": False,
                    "duration_seconds": None, "duration_text": "耗时未知",
                    "stages": [{"code": "send", "label": "准备发送", "status": "pending",
                                "duration_text": "耗时未知", "detail": "旧日志缺少请求标识，无法确认阶段关联和发送结果"}],
                })
        if groups:
            # Do not duplicate the unstructured Prepare-to-send line emitted by
            # the same new engine, or mix uncorrelated stages into a real trace.
            traces = [_project(events) for events in groups.values()]
            traces.sort(key=lambda value: value["time"], reverse=True)
            return traces[:min(limit, 50)]
        return list(reversed(legacy[-min(limit, 50):]))


__all__ = ["ResponseTraceService"]
