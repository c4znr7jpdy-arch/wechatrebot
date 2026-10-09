"""
磁力搜索模块 — 基于 apibay (TPB API) 搜索磁力链接
"""
import os
import re
import urllib.parse
from typing import List

import httpx
from nonebot import logger


_API_URL = "https://apibay.org/q.php"
_TIMEOUT = 20
_MAX_RESULTS = 5

_SORT_MAP = {
    "相关度": "",
    "大小": "size",
    "文件大小": "size",
    "热门": "seeders",
    "热门程度": "seeders",
    "热度": "seeders",
    "时间": "time",
    "最新": "time",
}


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024**3:.1f} GB"
    elif size_bytes >= 1024 ** 2:
        return f"{size_bytes / 1024**2:.1f} MB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes} B"


def _get_sort_key(sort_keyword: str) -> str:
    sort_keyword = sort_keyword.strip().lower()
    for key, value in _SORT_MAP.items():
        if key.lower() == sort_keyword:
            return value
    return ""


async def _search_apibay(keyword: str, sort_by: str = "", max_results: int = _MAX_RESULTS) -> List[dict]:
    """调用 apibay API 搜索，返回结果列表"""
    async with httpx.AsyncClient(timeout=_TIMEOUT, verify=False) as client:
        params = {"q": keyword, "cat": "0"}
        resp = await client.get(_API_URL, params=params)
        resp.raise_for_status()
        data = resp.json()

    if not data or (len(data) == 1 and data[0].get("id") == "0"):
        return []

    if sort_by == "size":
        data.sort(key=lambda x: int(x.get("size", 0)), reverse=True)
    elif sort_by == "seeders":
        data.sort(key=lambda x: int(x.get("seeders", 0)), reverse=True)
    elif sort_by == "time":
        data.sort(key=lambda x: int(x.get("added", 0)), reverse=True)

    return data[:max_results]


def detect_bt_intent(text: str) -> str | None:
    """检测磁力搜索意图，返回搜索关键词；无意图返回 None"""
    m = re.match(r"^\s*(?:bt|BT|磁力搜索|搜磁力)\s+(.+)$", text)
    if m:
        return m.group(1).strip()
    return None


async def search_magnet(keyword: str) -> str:
    """执行磁力搜索，返回格式化结果文本"""
    args_list = keyword.split()

    sort_keyword = ""
    if len(args_list) == 1:
        search_kw = args_list[0]
    else:
        sort_keyword = args_list[0]
        search_kw = " ".join(args_list[1:])

    sort_by = _get_sort_key(sort_keyword)
    if not sort_by and sort_keyword:
        search_kw = keyword

    try:
        results = await _search_apibay(search_kw, sort_by)
    except Exception as e:
        logger.exception(f"[MAGNET] 搜索失败: {e}")
        return f"磁力搜索失败: {str(e)[:100]}"

    if not results:
        return f"未找到「{search_kw}」相关的磁力链接"

    lines = [f"找到 {len(results)} 条结果"]
    for idx, item in enumerate(results, 1):
        name = item.get("name", "未知")
        info_hash = item.get("info_hash", "")
        size = _format_size(int(item.get("size", 0)))
        seeders = item.get("seeders", "0")
        leechers = item.get("leechers", "0")
        magnet = f"magnet:?xt=urn:btih:{info_hash}&dn={urllib.parse.quote(name)}"

        lines.append(
            f"\n===== 结果 {idx} =====\n"
            f"标题：{name}\n"
            f"大小：{size} | 做种：{seeders} | 下载：{leechers}\n"
            f"磁力：{magnet}"
        )

    return "\n".join(lines)
