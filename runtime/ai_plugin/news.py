"""Shared Douyin hot-board adapter for commands and scheduled news."""
from __future__ import annotations
from datetime import datetime
import os
import httpx
from nonebot import logger

DOUYIN_API_URL = "https://cn.apihz.cn/api/xinwen/douyin.php"


def normalize_items(payload: object, count: int) -> list[dict]:
    if not isinstance(payload, dict) or payload.get("code") != 200:
        return []
    raw = payload.get("data")
    if not isinstance(raw, list):
        return []
    items = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        if not title:
            continue
        try:
            hot = int(item.get("hot_value") or 0)
        except (TypeError, ValueError):
            hot = 0
        items.append({"title": title, "hot_value": hot})
    return sorted(items, key=lambda item: item["hot_value"], reverse=True)[:count]


async def fetch_hot_news(count: int = 20) -> list[dict]:
    """Only return Douyin news; do not silently substitute another source."""
    limit = max(1, min(int(count), 50))
    api_id = os.getenv("JIANG_CONTENT_API_ID", "").strip()
    api_key = os.getenv("JIANG_CONTENT_API_KEY", "").strip()
    if not api_id or not api_key:
        raise RuntimeError("抖音热榜尚未配置，请联系管理员检查内容服务配置")
    try:
        async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
            response = await client.get(DOUYIN_API_URL, params={"id": api_id, "key": api_key})
            response.raise_for_status()
            items = normalize_items(response.json(), limit)
        if items:
            return items
    except Exception:
        # HTTP exceptions include the request URL and its credential parameters.
        logger.warning("[NEWS] 抖音热榜请求失败")
    raise RuntimeError("抖音热榜暂时不可用，请检查内容服务配置或稍后重试")


def format_news(items: list[dict]) -> str:
    if not items:
        return "暂无抖音热榜数据"
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    lines = [f"📰 抖音热榜 {now}"]
    for index, item in enumerate(items, 1):
        title = str(item.get("title") or "").strip()
        if title:
            lines.append(f"{index}. {title}")
    return "\n".join(lines)
