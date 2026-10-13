"""Safe, persisted reasons for report sections without analysis content."""
from __future__ import annotations

from collections.abc import Mapping


ANALYSIS_TIMEOUT_MESSAGE = (
    "本次模型请求超时，未能生成分析内容。请调整群聊分析的请求超时，"
    "或选择响应更快的分析模型。"
)
_SECTIONS = ("topic", "user_title", "golden_quote", "chat_quality")


def normalize_timeout_sections(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [name for name in _SECTIONS if name in value]


def timeout_sections_from_errors(errors: object) -> list[str]:
    """Only the known timeout result becomes public; never copy upstream errors."""
    if not isinstance(errors, list):
        return []
    return [name for name in _SECTIONS if f"{name}: {ANALYSIS_TIMEOUT_MESSAGE}" in errors]


def section_unavailable_reason(
    analysis_result: Mapping[str, object], section: str, enabled: bool,
) -> str:
    if not enabled:
        return "此板块未启用，可在群聊分析的常用设置中开启。"
    if section in normalize_timeout_sections(analysis_result.get("timeout_sections")):
        return ANALYSIS_TIMEOUT_MESSAGE
    return "本次未获得有效分析内容。可能是消息不足或模型调用失败，请检查分析模型后重试。"
