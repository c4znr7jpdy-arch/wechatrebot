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
    replacements = (
        (r"(?i)astrbot", "回复引擎"),
        (r"(?i)new[-_ ]?hook", "微信连接"),
        (r"(?i)hook[_ -]?bridge", "微信连接"),
        (r"(?i)\bhook\b", "微信连接"),
    )
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)
    text = re.sub(r"(?i)(api[_ -]?key|token|authorization)(\s*[=:]\s*)\S+", r"\1\2***", text)
    return text


def tail_log(path: str | Path, *, max_bytes: int = 512 * 1024, max_lines: int = 1200) -> str:
    target = Path(path)
    try:
        size = target.stat().st_size
        with target.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            raw = handle.read(max_bytes)
    except FileNotFoundError:
        return "当前还没有日志。启动机器人后，这里会自动显示运行记录。"
    except OSError as exc:
        return f"日志暂时无法读取：{exc}"
    text = raw.decode("utf-8", errors="replace")
    if size > max_bytes and "\n" in text:
        text = text.split("\n", 1)[1]
    lines = text.splitlines()[-max_lines:]
    return sanitize_log_text("\n".join(lines))


__all__ = ["LogSource", "log_sources", "sanitize_log_text", "tail_log"]
