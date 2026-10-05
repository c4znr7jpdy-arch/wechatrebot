"""报告分析时间范围的统一展示文案。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Mapping


def build_report_period(days: int | None, end_at: datetime | None = None) -> dict[str, Any]:
    """构造报告范围；展示最近 N 天的滚动时间窗口。"""
    try:
        normalized_days = max(1, int(days or 1))
    except (TypeError, ValueError):
        normalized_days = 1

    end_at = end_at or datetime.now()
    end_date = end_at.date()
    start_date = end_date - timedelta(days=normalized_days)
    is_single_day = normalized_days == 1

    return {
        "analysis_days": normalized_days,
        "is_single_day": is_single_day,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "date_badge": (
            end_date.strftime("%Y年%m月%d日")
            if is_single_day
            else f"{start_date:%Y年%m月%d日}—{end_date:%m月%d日}"
        ),
        "report_title": (
            "近 1 天群聊回顾，来看看群里发生了什么吧！"
            if is_single_day
            else f"近 {normalized_days} 天群聊回顾，来看看群里发生了什么吧！"
        ),
        "topic_heading": (
            "近 1 天话题 Topics"
            if is_single_day
            else f"近 {normalized_days} 天话题 Topics"
        ),
        "activity_heading": (
            "24H 活跃轨迹"
            if is_single_day
            else f"近 {normalized_days} 天分时活跃轨迹"
        ),
        "report_name": "近 1 天群聊分析报告" if is_single_day else f"近 {normalized_days} 天群聊分析报告",
    }


def normalize_report_period(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """兼容旧历史报告或不含范围元数据的分析结果。"""
    if not isinstance(value, Mapping):
        return build_report_period(1)
    period = build_report_period(value.get("analysis_days", 1))
    period.update({key: item for key, item in value.items() if item is not None})
    return period

