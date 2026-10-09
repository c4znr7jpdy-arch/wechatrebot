"""
今日油价模块 — /油价 命令，获取各省油价信息
"""
import re as _re

import httpx
from nonebot import logger

API_URL = "https://api.pearapi.ai/api/oilprice"

# 省份简称映射（用户可能输入简称）
_PROVINCE_ALIAS = {
    "川": "四川", "蜀": "四川",
    "渝": "重庆", "京": "北京", "津": "天津",
    "沪": "上海", "粤": "广东", "苏": "江苏",
    "浙": "浙江", "鲁": "山东", "豫": "河南",
    "鄂": "湖北", "湘": "湖南", "皖": "安徽",
    "闽": "福建", "赣": "江西", "冀": "河北",
    "晋": "山西", "辽": "辽宁", "吉": "吉林",
    "黑": "黑龙江", "陕": "陕西", "甘": "甘肃",
    "青": "青海", "琼": "海南", "桂": "广西",
    "黔": "贵州", "滇": "云南", "藏": "西藏",
    "蒙": "内蒙古", "宁": "宁夏", "疆": "新疆",
}


def normalize_province(text: str) -> str:
    """规范化省份名称，支持简称"""
    text = text.strip()
    if text in _PROVINCE_ALIAS:
        return _PROVINCE_ALIAS[text]
    return text


async def fetch_oilprice(province: str) -> dict:
    """获取指定省份的油价信息"""
    province = normalize_province(province)
    for attempt in range(2):
        try:
            # 这些公开 API 应直接连接。禁用环境代理，避免继承启动终端中
            # 已失效的 127.0.0.1 代理；只读 GET 发生传输错误时安全重试一次。
            async with httpx.AsyncClient(timeout=15.0, trust_env=False) as client:
                resp = await client.get(
                    API_URL,
                    params={"type": "get", "province": province},
                )
                resp.raise_for_status()
                data = resp.json()
            break
        except httpx.TransportError as exc:
            if attempt == 1:
                raise
            logger.warning(
                "[OILPRICE] 请求失败，正在直连重试一次: %s",
                type(exc).__name__,
            )
    if data.get("code") != 200:
        raise RuntimeError(data.get("msg", "查询失败"))
    p = data.get("data", {}).get("province")
    if not isinstance(p, dict):
        raise RuntimeError(f"未找到「{province}」的油价信息，请检查省份名称")
    return data["data"]


def format_oilprice(data: dict) -> str:
    """格式化油价信息为文本"""
    p = data.get("province", {})
    if not isinstance(p, dict):
        return "未获取到油价数据"
    name = p.get("pri_name", "未知")
    g92 = p.get("gasoline_92", "-")
    g95 = p.get("gasoline_95", "-")
    g98 = p.get("gasoline_98", "-")
    d0 = p.get("diesel_0", "-")
    time_str = data.get("time", "")

    lines = [
        f"⛽ {name}今日油价",
        "━" * 18,
        f"  92# 汽油  ¥{g92}/升",
        f"  95# 汽油  ¥{g95}/升",
        f"  98# 汽油  ¥{g98}/升",
        f"  0#  柴油  ¥{d0}/升",
    ]
    if time_str:
        lines.append(f"\n📅 更新时间: {time_str}")
    return "\n".join(lines)
