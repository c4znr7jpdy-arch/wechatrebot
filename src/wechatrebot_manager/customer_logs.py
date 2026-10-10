from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re


@dataclass(frozen=True, slots=True)
class LogSource:
    key: str
    display_name: str
    path: Path


def log_sources(root: str | Path) -> tuple[LogSource, ...]:
    install = Path(root).expanduser().resolve()
    return (
        LogSource("engine-service", "回复引擎", install / "logs" / "astrbot-service.log"),
        LogSource("wechat-connection", "微信连接", install / "logs" / "new-hook-bridge.log"),
        LogSource("engine-runtime", "运行核心", install / "logs" / "astrbot-core.log"),
        LogSource("message-forward", "消息转发", install / "app" / "bridge" / "var" / "log" / "bridge.log"),
    )


def sanitize_log_text(text: str) -> str:
    # Redact before truncation/formatting: headers may contain spaces and JSON
    # credentials may contain escaped quotes. Never retain chat payloads.
    text = re.sub(r"\x1b\[[0-9;]*m", "", str(text))
    text = re.sub(r"(?im)(\b(?:Authorization|Cookie|Set-Cookie)\s*:\s*)[^\r\n]*", r"\1***", text)
    text = re.sub(r"(?im)(\[[^\]\n]*event_bus:\d+\]\s*:?).*", r"\1 <聊天内容已脱敏>", text)
    text = re.sub(r"(?im)(?:Tool `[^`]+` Result:|completion:|genai result:|保存打断消息:|语音转文本[^:\n]*结果:|已合并消息，提示:).*", "<消息或工具正文已脱敏>", text)
    # Nested payload arrays/objects cannot be safely redacted using a scalar
    # value regex. Remove their remainder within this bounded log block.
    text = re.sub(r'''(?is)(["']?(?:content|prompt|messages|request_body|response_body)["']?\s*[=:]\s*)[\[{].*''', r"\1<正文已脱敏>", text)
    fields = r"api[_ -]?key|token|authorization|cookie|set-cookie|password|secret|credential|access_token|refresh_token|wxid|user_id|group_id|chat_id|content|prompt|messages|chat_text"
    text = re.sub(
        rf'''(?im)(["']?(?:{fields})["']?\s*[=:]\s*)(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[^\r\n,;}}]+)''',
        r"\1***", text,
    )
    text = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+", "Bearer ***", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{8,}\b", "***", text)
    text = re.sub(r"(?i)\bwxid_[a-z0-9_-]+|\b\d+@chatroom\b", "<聊天标识>", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1***@", text)
    text = re.sub(r"(?im)(?:收到消息|收到私聊|收到群聊|消息内容|聊天内容|用户输入|模型输入|模型输出)\s*[:：].*", "聊天内容：<已脱敏>", text)
    text = re.sub(r"(?i)(https?://[^\s?'\"]+)\?[^\s'\"]+", r"\1?<已脱敏>", text)
    text = re.sub(r"(?i)[a-z]:[\\/]Users[\\/][^\\/\s\"']+", "<用户目录>", text)
    text = re.sub(r'''(?i)\b[a-z]:[\\/](?:[^\r\n"'<>]*[\\/])([^\\/\r\n"'<>]+)''', r"<本地路径>/\1", text)
    replacements = (
        (r"天使(?:之)?心|(?i:angel[-_ ]?heart)", "机器人核心"),
        (r"(?i)kawayiyokami", "核心维护者"),
        (r"(?i)astrbot", "回复引擎"),
        (r"(?i)new[-_ ]?hook", "微信连接"),
        (r"(?i)hook[_ -]?bridge", "微信连接"),
        (r"(?i)\bhook\b", "微信连接"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"(?i)(api[_ -]?key|token|authorization)(\s*[=:]\s*)\S+", r"\1\2***", text)
    return text


def read_log_tail(path: str | Path, *, max_bytes: int = 512 * 1024, max_lines: int = 1200) -> tuple[str, bool]:
    """Bounded raw read. Callers must redact before display or persistence."""
    target = Path(path)
    with target.open("rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        raw = handle.read(max_bytes)
    text = raw.decode("utf-8", errors="replace")
    if size > max_bytes:
        text = text.partition("\n")[2]
    lines = text.splitlines()
    return "\n".join(lines[-max_lines:]), size > max_bytes or len(lines) > max_lines


def tail_log(path: str | Path, *, max_bytes: int = 512 * 1024, max_lines: int = 1200) -> str:
    target = Path(path)
    try:
        text, _ = read_log_tail(target, max_bytes=max_bytes, max_lines=max_lines)
    except FileNotFoundError:
        return "当前还没有日志。启动机器人后，这里会自动显示运行记录。"
    except OSError as exc:
        return f"日志暂时无法读取：{exc}"
    return sanitize_log_text(text)


__all__ = ["LogSource", "log_sources", "sanitize_log_text", "tail_log", "read_log_tail"]
