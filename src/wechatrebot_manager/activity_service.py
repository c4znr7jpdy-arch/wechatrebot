"""Bounded diagnostic feed shared by display, copy and export."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from . import __version__
from .customer_logs import (
    ACTIVITY_RETENTION_SECONDS, ERROR_REPEAT_WINDOW_SECONDS,
    LogSource, log_sources, log_timestamp, read_log_tail, sanitize_log_text, strip_log_ansi,
)
from .diagnostics import redact_sensitive
from .error_catalog import load_error_catalog, provider_error_code
from .events import EventStore
from .update_errors import UPDATE_ERRORS


_LEVEL = re.compile(r"\b(ERROR|ERRO|CRITICAL|FATAL|WARNING|WARN|INFO|DEBUG|DBUG)\b", re.I)
_TIME = re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?(?:Z|[+-]\d{2}:?\d{2})?")
_MODEL_FAILURE = re.compile(
    r"(?m)^\[(?:\d{4}-\d{2}-\d{2}[ T])?\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\] "
    r"\[(?:Core|Plug)\] \[(?:ERROR|ERRO|WARNING|WARN)\](?: \[v[^\]\r\n]+\])? "
    r"\[sources\.request_retry:\d+\]: MODEL_REQUEST_FAILED "
    r"error_type=([A-Za-z_][A-Za-z_0-9]*) status_code=(\d{3}|none)\s*$"
)
_PRIVATE_FIELDS = re.compile(r"(?i)^(?:content|prompt|messages|chat_text|request_body|response_body)$")
_EVENT_NAMES = {
    "APP_START": "软件已打开", "CHECK_WECHAT": "正在检查微信环境",
    "WECHAT_READY": "微信环境检查通过", "CORE_START": "开始启动机器人功能",
    "CORE_READY": "机器人功能已就绪", "CONNECTION_START": "开始连接微信",
    "CONNECTION_CONNECTED": "微信连接成功", "FUNCTION_LOADED": "全部功能加载完成",
    "WECHAT_CLOSED": "检测到微信已关闭", "CONNECTION_STOP": "微信连接已停止",
    "CORE_STOP": "机器人已停止", "APP_ERROR": "操作未完成",
}


def diagnostic_text(value: object, root: Path, *, limit: int = 12000) -> str:
    """One redaction boundary for screen, clipboard and exported diagnostics."""
    def private(item: object) -> object:
        if isinstance(item, dict):
            return {str(k): "***" if _PRIVATE_FIELDS.match(str(k)) else private(v) for k, v in item.items()}
        if isinstance(item, list):
            return [private(v) for v in item]
        return item

    if not isinstance(value, str):
        value = json.dumps(private(redact_sensitive(value)), ensure_ascii=False, indent=2)
    text = re.sub(re.escape(str(root)), "<安装目录>", value, flags=re.I)
    text = re.sub(re.escape(root.as_posix()), "<安装目录>", text, flags=re.I)
    text = sanitize_log_text(text)
    return text if len(text) <= limit else text[:limit] + "\n[内容超出上限，已截断]"


def _blocks(text: str):
    """Keep traceback continuation lines with the record that introduced them."""
    block: list[str] = []
    for line in strip_log_ansi(text).splitlines():
        prefix = line[:100]
        start = bool(_TIME.search(prefix) or re.match(r"^\[?\d{2}:\d{2}:\d{2}", prefix) or re.match(r"^\[?(?:ERROR|ERRO|CRITICAL|FATAL|WARNING|WARN|INFO|DEBUG|DBUG)\b", prefix, re.I))
        if start and block:
            yield "\n".join(block)
            block = []
        block.append(line)
    if block:
        yield "\n".join(block)


class ActivityService:
    def __init__(self, root: str | Path, *, build: int = 0) -> None:
        self.root = Path(root).resolve()
        self.build = build
        self.catalog = load_error_catalog(self.root)

    def sources(self) -> tuple[LogSource, ...]:
        return (
            LogSource("manager", "管理器", self.root / "logs/lifecycle-events.jsonl"),
            LogSource("manager", "管理器", self.root / "logs/lifecycle-events.jsonl.1"),
            *log_sources(self.root),
            LogSource("update", "软件更新", self.root / "logs/update.log"),
            LogSource("update", "软件更新", self.root / "logs/update.log.1"),
        )

    def _item(self, source: LogSource, raw: str, *, position: int = 0) -> dict:
        raw = strip_log_ansi(raw)
        structured = None
        if source.key in {"manager", "update"}:
            try:
                structured = json.loads(raw)
            except (ValueError, TypeError):
                pass
        event = structured if isinstance(structured, dict) else {}
        match = _TIME.search(raw[:100])
        time = str(event.get("time") or (match.group() if match else ""))
        if not _TIME.fullmatch(time):
            time = ""
        code = str(event.get("code") or event.get("error_code") or "")
        level_match = _LEVEL.search(raw.splitlines()[0][:100]) if not event else None
        status = str(event.get("status") or event.get("state") or "").lower()
        level = "error" if status in {"failed", "error"} else "warning" if status in {"unknown", "pending", "warning"} else "info"
        if level_match:
            level = {"ERROR": "error", "ERRO": "error", "CRITICAL": "error", "FATAL": "error", "WARN": "warning", "WARNING": "warning"}.get(level_match.group().upper(), "info")
        if not event and "Traceback (most recent call last)" in raw:
            level = "error"
        if not code and level != "info":
            code = classify_log_code(raw)
        if code and code.startswith("UPD-") and code in UPDATE_ERRORS:
            definition = UPDATE_ERRORS[code]
            title, suggestion = definition.title, definition.message
            level = "warning" if code == "UPD-1016" else "error"
        elif code.startswith("LIC-"):
            title, suggestion = "授权操作未完成", str(event.get("message") or "请检查订阅与授权状态。")
        elif code:
            definition = self.catalog.get(code)
            title, suggestion = definition.title, definition.message
            level = definition.level if definition.code == code else level
        else:
            title = _EVENT_NAMES.get(str(event.get("event")), "运行日志" if level == "info" else "运行异常")
            suggestion = "请复制问题信息联系技术支持。" if level != "info" else ""
            code = "19000" if level != "info" else ""
        details = event.get("details") if isinstance(event.get("details"), dict) else {}
        message = str(event.get("message") or raw)
        # Legacy lifecycle events retain their original title and are not failures.
        if level == "info" and event.get("event") in _EVENT_NAMES and not event.get("message"):
            message = title
        detail = diagnostic_text(details.get("technical") or details if details else ({"event": event.get("event"), "message": message} if event else raw), self.root)
        summary = diagnostic_text(message, self.root, limit=1600)
        fingerprint = hashlib.sha256(f"{source.key}|{code}|{level}|{details.get('operation', '')}|{_TIME.sub('', summary)}|{_TIME.sub('', detail)}".encode()).hexdigest()
        identity = diagnostic_text(str(event.get("event_id") or hashlib.sha256(f"{source.path.name}|{position}|{raw}".encode()).hexdigest()[:24]), self.root, limit=80)
        count = event.get("count", 1)
        first_time = str(event.get("first_time") or time)
        return {
            "id": identity, "time": time, "timestamp": log_timestamp(time),
            "source": source.key, "source_name": source.display_name, "level": level,
            "status": status or level, "code": diagnostic_text(code, self.root, limit=80), "title": diagnostic_text(title, self.root, limit=200),
            "message": summary, "detail": detail, "suggestion": diagnostic_text(suggestion, self.root, limit=1000),
            "operation": diagnostic_text(details.get("operation", ""), self.root, limit=160),
            "count": count if isinstance(count, int) and count > 0 else 1,
            "first_time": first_time if _TIME.fullmatch(first_time) else time, "fingerprint": fingerprint,
            "context": "", "event": diagnostic_text(event.get("event", ""), self.root, limit=100),
        }

    def recent(self) -> list[dict]:
        items: list[dict] = []
        cutoff = time.time() - ACTIVITY_RETENTION_SECONDS
        try:
            EventStore(self.root).prune()
        except OSError as exc:
            items.append(self._read_failure(self.sources()[0], exc))
        for source in self.sources():
            try:
                text, truncated = read_log_tail(source.path, max_bytes=256 * 1024, max_lines=1200)
                modified = source.path.stat().st_mtime
            except FileNotFoundError:
                continue
            except OSError as exc:
                items.append(self._read_failure(source, exc))
                continue
            blocks = text.splitlines() if source.key in {"manager", "update"} else list(_blocks(text))
            current: list[tuple[dict, str]] = []
            for index, raw in enumerate(blocks):
                if not raw.strip():
                    continue
                item = self._item(source, raw, position=index)
                if (item["timestamp"] if item["timestamp"] is not None else modified) < cutoff:
                    continue
                current.append((item, raw))
            for index, (item, raw) in enumerate(current):
                if item["level"] != "info" and source.key not in {"manager", "update"}:
                    item["context"] = diagnostic_text("\n".join(raw for _, raw in current[max(0, index - 1):index + 2]), self.root, limit=6000)
                item["truncated"] = truncated
                items.append(item)
        items.sort(key=lambda item: item["timestamp"] if item["timestamp"] is not None else float("-inf"), reverse=True)
        grouped: dict[str, dict] = {}
        previous_times: dict[str, float | None] = {}
        result: list[dict] = []
        for item in items:
            key = item.pop("fingerprint")
            latest = grouped.get(key)
            previous = previous_times.get(key)
            if latest and item["timestamp"] is not None and previous is not None and previous - item["timestamp"] <= ERROR_REPEAT_WINDOW_SECONDS:
                latest["count"] += item["count"]
                latest["first_time"] = item["first_time"]
            else:
                grouped[key] = item
                result.append(item)
            previous_times[key] = log_timestamp(item["first_time"])
        # Prefer retaining actionable records within the bounded response.
        retained = [item for item in result if item["level"] != "info"][:300]
        retained += [item for item in result if item["level"] == "info"][:100]
        retained.sort(key=lambda item: item["timestamp"] if item["timestamp"] is not None else float("-inf"), reverse=True)
        for item in retained:
            item["report"] = self.report(item)
        return retained

    def _read_failure(self, source: LogSource, exc: OSError) -> dict:
        item = self._item(LogSource(source.key, source.display_name, source.path), "ERROR 日志读取失败")
        item.update(code="19003", title="运行日志读取失败", message=diagnostic_text(str(exc), self.root), detail="该来源日志未能读取；其他来源仍可查看。")
        item["fingerprint"] = source.key + str(source.path)
        return item

    def report(self, item: dict) -> str:
        level = {"error": "错误", "warning": "警告", "info": "提示"}[item["level"]]
        lines = [
            "机器人问题报告", f"软件版本：{__version__} · 构建 {self.build}",
            f"事件编号：{item['id']}", f"错误码：{item['code'] or '无（普通记录）'}",
            f"级别：{level}", f"标题：{item['title']}",
            f"发生时间：{item['time'] or '原始日志未提供'}", f"来源：{item['source_name']}",
            f"操作：{item['operation'] or '原始日志未提供'}",
            f"同类记录：{item['count']} 次（首次 {item['first_time'] or '时间未知'}）",
            "当时运行状态：未记录；不使用当前状态推断", "具体错误：", item["message"],
            "技术详情：", item["detail"], "附近日志：", item["context"] or "无",
            "处理建议：", item["suggestion"] or "无需处理",
            "采集范围：最近 48 小时，各日志最多读取 256 KiB / 1200 行，最多显示 300 条异常和 100 条普通记录；间隔不超过 5 分钟的持续同类异常合并并保留次数。无日期日志按文件修改时间判断保留范围。",
            "隐私处理：凭据、已识别聊天正文和个人标识已脱敏。",
        ]
        return "\n".join(lines)


def classify_log_code(text: str) -> str:
    """Only recognize explicit SDK exception evidence, not HTTP-like chat text."""
    text = strip_log_ansi(text)
    failure = _MODEL_FAILURE.search(text)
    if failure:
        status = int(failure.group(2)) if failure.group(2) != "none" else None
        return provider_error_code(failure.group(1), status)
    sdk = re.search(r"(?m)^\s*openai\.(?:_exceptions\.)?(AuthenticationError|PermissionDeniedError|RateLimitError|APITimeoutError|APIConnectionError|NotFoundError|InternalServerError|BadRequestError):", text)
    if not sdk:
        return ""
    if sdk.group(1) == "RateLimitError" and re.search(r"['\"]code['\"]\s*:\s*['\"]insufficient_quota['\"]", text):
        return "16005"
    return provider_error_code(sdk.group(1))
