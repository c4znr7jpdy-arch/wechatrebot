"""Public hot-news adapter used by the commercial runtime.

The upstream plugin historically depended on an internal apihz credential. The
customer-facing build uses Toutiao's public hot-board endpoint instead, while
keeping the configured apihz path for existing installations.
"""
from __future__ import annotations

from datetime import datetime
import os

import httpx
from nonebot import logger


_API_HOSTS = (
    "https://cn.apihz.cn",
    "http://81.69.163.176",
    "http://101.35.2.25",
    "http://124.222.204.22",
    "http://101.34.207.105",
    "http://43.142.65.209",
    "http://81.68.85.14",
)
_TOUTIAO_HOT_BOARD_URL = "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc"
_PUBLIC_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}


def _normalize_toutiao_items(payload: object, count: int) -> list[dict]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    items = []
    for item in payload["data"]:
        if not isinstance(item, dict):
            continue
        title = str(item.get("Title") or "").strip()
        if not title:
            continue
        try:
            hot_value = int(item.get("HotValue") or 0)
        except (TypeError, ValueError):
            hot_value = 0
        items.append({
            "title": title,
            "hot_value": hot_value,
            "url": str(item.get("Url") or "").strip(),
        })
    return items[:count]


async def _fetch_public_news(count: int) -> list[dict]:
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0),
        headers=_PUBLIC_HEADERS,
        follow_redirects=True,
        trust_env=False,
    ) as client:
        response = await client.get(_TOUTIAO_HOT_BOARD_URL)
        response.raise_for_status()
        items = _normalize_toutiao_items(response.json(), count)
    if not items:
        raise RuntimeError("今日头条未返回有效热榜数据")
    return items


async def _fetch_configured_news(api_id: str, api_key: str, count: int) -> list[dict]:
    for host in _API_HOSTS:
        try:
            async with httpx.AsyncClient(timeout=10.0, trust_env=False) as client:
                response = await client.get(
                    f"{host}/api/xinwen/douyin.php",
                    params={"id": api_id, "key": api_key},
                )
                if response.status_code != 200:
                    continue
                payload = response.json()
                if payload.get("code") != 200:
                    logger.warning("[NEWS] apihz 错误: %s", payload.get("msg", ""))
                    continue
                items = payload.get("data") or []
                if items:
                    items.sort(key=lambda item: item.get("hot_value", 0), reverse=True)
                    return items[:count]
        except Exception as exc:
            logger.warning("[NEWS] %s 请求失败: %s", host, exc)
    return []


async def fetch_hot_news(count: int = 20) -> list[dict]:
    """获取热榜；优先兼容旧凭据，否则使用公开今日头条接口。"""
    limit = max(1, min(int(count), 50))
    api_id = os.getenv("JIANG_CONTENT_API_ID", "").strip()
    api_key = os.getenv("JIANG_CONTENT_API_KEY", "").strip()
    if api_id and api_key:
        configured = await _fetch_configured_news(api_id, api_key, limit)
        if configured:
            return configured
    try:
        return await _fetch_public_news(limit)
    except Exception as exc:
        logger.warning("[NEWS] 公开热榜请求失败: %s", exc)
        raise RuntimeError("热点新闻暂时不可用，请稍后再试") from exc


def format_news(items: list[dict]) -> str:
    """格式化热榜列表。"""
    if not items:
        return "暂无热榜数据"
    now = datetime.now().strftime("%Y/%m/%d %H:%M")
    lines = [f"📰 今日热点 {now}"]
    for item in items:
        title = str(item.get("title") or "").strip()
        if title:
            lines.append(f"#{title}")
    return "\n".join(lines)
