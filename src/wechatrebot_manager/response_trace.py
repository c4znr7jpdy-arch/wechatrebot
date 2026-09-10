from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from typing import Any

from .customer_config import load_provider_profile
from .customer_logs import tail_log
from .provider import provider_display_name


_CLOCK = re.compile(r"\[(?:(?P<date>\d{4}-\d{2}-\d{2})\s+)?(?P<time>\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?)\]")
_MEMORY_COUNT = re.compile(r"(?:检索到|注入)\s*(\d+)\s*条记忆")


def _timestamp(line: str, *, fallback_date: str) -> datetime | None:
    match = _CLOCK.search(line)
    if not match:
        return None
    value = f"{match.group('date') or fallback_date} {match.group('time')}"
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _duration_text(seconds: float | None) -> str:
    if seconds is None:
        return ""
    if seconds < 0.1:
        return "<0.1 秒"
    return f"{seconds:.1f} 秒"


def _stage(
    code: str,
    label: str,
    status: str = "success",
    detail: str = "",
    duration: float | None = None,
) -> dict[str, Any]:
    result = {"code": code, "label": label, "status": status}
    if detail:
        result["detail"] = detail
    if duration is not None:
        result["duration_seconds"] = round(max(0.0, duration), 3)
        result["duration_text"] = _duration_text(max(0.0, duration))
    return result


class ResponseTraceService:
    """Project recent reply logs into privacy-safe, customer-readable stages."""

    def __init__(self, install_root: str | Path) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = self.install_root / "logs" / "astrbot-core.log"

    def _model_label(self) -> str:
        profile = load_provider_profile(self.install_root)
        if profile is None:
            return "回复模型"
        normalized = profile.normalized()
        return provider_display_name(normalized.source_id, normalized.model)

    def _trace(self, lines: list[str], model_label: str) -> dict[str, Any] | None:
        if not lines or not any("Prepare to send" in line for line in lines):
            return None
        today = datetime.now().date().isoformat()
        entries = [
            (line, _timestamp(line, fallback_date=today))
            for line in lines
        ]
        times = [value for _line, value in entries if value is not None]
        start = times[0] if times else None
        end = times[-1] if times else None
        stages: list[dict[str, Any]] = []

        def first_time(predicate) -> datetime | None:
            return next(
                (value for line, value in entries if value is not None and predicate(line)),
                None,
            )

        def last_time(predicate) -> datetime | None:
            return next(
                (value for line, value in reversed(entries) if value is not None and predicate(line)),
                None,
            )

        def elapsed(begin: datetime | None, finish: datetime | None) -> float | None:
            if begin is None or finish is None:
                return None
            return max(0.0, (finish - begin).total_seconds())

        direct = any("direct_wake" in line or "命中快路径" in line for line in lines)
        decision_time = first_time(
            lambda line: "direct_wake" in line
            or "命中快路径" in line
            or "秘书决策 action=" in line
        )
        if direct:
            stages.append(
                _stage(
                    "decision",
                    "点名快路径",
                    detail="已跳过额外回复判断",
                    duration=elapsed(start, decision_time),
                )
            )
        elif any("秘书决策 action=reply" in line for line in lines):
            stages.append(_stage("decision", "回复判断", duration=elapsed(start, decision_time)))

        memory_lines = [line for line in lines if "memory_recall" in line or "记忆召回" in line or "条记忆" in line]
        memory_start = first_time(
            lambda line: "memory_recall" in line or "开始记忆召回" in line
        )
        memory_end = last_time(
            lambda line: "memory_recall" in line
            or ("检索" in line and "超过" in line)
            or "条记忆" in line
        )
        memory_duration = elapsed(memory_start, memory_end)
        if any("short_casual" in line for line in memory_lines):
            stages.append(_stage("memory", "长期记忆", "skipped", "普通寒暄无需查询", memory_duration))
        elif any("auto_recall_off" in line or "recall_state=disabled" in line for line in memory_lines):
            stages.append(_stage("memory", "长期记忆已关闭", "skipped", duration=memory_duration))
        elif any("超过" in line and "检索" in line for line in lines):
            stages.append(_stage("memory", "长期记忆降级", "skipped", "检索超时，已继续回复", memory_duration))
        else:
            counts = [int(match.group(1)) for line in memory_lines if (match := _MEMORY_COUNT.search(line))]
            if counts:
                count = max(counts)
                stages.append(_stage("memory", f"长期记忆 {count} 条" if count else "长期记忆无结果", "success" if count else "skipped", duration=memory_duration))
            elif any("explicit_memory_request" in line or "recall_state=searching" in line for line in memory_lines):
                stages.append(_stage("memory", "明确回忆请求", duration=memory_duration))

        emotion_time = first_time(lambda line: "compact 情绪上下文已注入" in line)
        if any("compact 情绪上下文已注入" in line for line in lines):
            stages.append(_stage("emotion", "精简情感", duration=elapsed(decision_time, emotion_time)))
        prepare_time = last_time(lambda line: "Prepare to send" in line)
        model_start_candidates = [value for value in (decision_time, memory_end, emotion_time) if value]
        model_start = max(model_start_candidates) if model_start_candidates else start
        stages.append(_stage("model", model_label, duration=elapsed(model_start, prepare_time)))

        expression_time = first_time(
            lambda line: "表情触发语音" in line or "meme_manager" in line
        )
        if any("表情触发语音" in line for line in lines):
            stages.append(_stage("voice", "语音生成", duration=elapsed(model_start, expression_time)))
        elif any("meme_manager" in line and "[]" not in line for line in lines):
            stages.append(_stage("expression", "表情选择", duration=elapsed(model_start, expression_time)))

        voice = any("Prepare to send" in line and "ComponentType.Record" in line for line in lines)
        completion_time = first_time(lambda line: "AI回复完成" in line or "发送完成" in line)
        send_status = "success" if completion_time is not None else "pending"
        stages.append(
            _stage(
                "send",
                "语音发送" if voice else "文字发送",
                send_status,
                "" if completion_time is not None else "已交给微信连接发送",
                elapsed(prepare_time, completion_time),
            )
        )
        duration = max(0.0, (end - start).total_seconds()) if start and end else None
        title = "群聊回复" if any("GroupMessage" in line for line in lines) else "私聊回复"
        return {
            "time": end.astimezone().isoformat(timespec="milliseconds") if end else "",
            "title": title,
            "duration_seconds": round(duration, 1) if duration is not None else None,
            "duration_text": f"{duration:.1f} 秒" if duration is not None else "已完成",
            "stages": stages,
        }

    def recent(self, limit: int = 12) -> list[dict[str, Any]]:
        if limit <= 0:
            return []
        lines = tail_log(self.path, max_bytes=1024 * 1024, max_lines=3000).splitlines()
        anchors = [index for index, line in enumerate(lines) if "Prepare to send" in line]
        traces: list[dict[str, Any]] = []
        model_label = self._model_label()
        previous_anchor = -1
        for position, anchor in enumerate(anchors):
            floor = max(previous_anchor + 1, anchor - 100)
            inbound = [
                index for index in range(floor, anchor + 1)
                if "core.event_bus" in lines[index]
            ]
            start = inbound[-1] if inbound else floor
            next_anchor = anchors[position + 1] if position + 1 < len(anchors) else len(lines)
            next_inbound = next(
                (
                    index
                    for index in range(anchor + 1, min(next_anchor, len(lines)))
                    if "core.event_bus" in lines[index]
                ),
                next_anchor,
            )
            end_index = min(next_inbound, anchor + 40)
            completion = next(
                (
                    index
                    for index in range(anchor + 1, end_index)
                    if "AI回复完成" in lines[index] or "发送完成" in lines[index]
                ),
                None,
            )
            if completion is not None:
                end_index = completion + 1
            else:
                end_index = anchor + 1
            trace = self._trace(lines[start:end_index], model_label)
            if trace is not None:
                traces.append(trace)
            previous_anchor = anchor
        return list(reversed(traces[-limit:]))


__all__ = ["ResponseTraceService"]
