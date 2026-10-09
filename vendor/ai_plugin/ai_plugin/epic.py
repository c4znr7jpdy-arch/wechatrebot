"""
Epic Games 喜加一模块 — /epic 或 /喜加一 命令
获取 Epic 商店每周免费游戏信息
"""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import json
import hashlib

import httpx
from nonebot import logger

# Epic 免费游戏 API
EPIC_API = "https://store-site-backend-static-ipv4.ak.epicgames.com/freeGamesPromotions"
EPIC_PARAMS = {"locale": "zh-CN", "country": "CN", "allowCountries": "CN"}
EPIC_HEADERS = {
    "Referer": "https://www.epicgames.com/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
}

# 数据目录
_DATA_DIR = Path(__file__).parent.parent / "data"
_HISTORY_FILE = _DATA_DIR / "epic_push_history.json"

# 时区
CST = timezone(timedelta(hours=8))


async def fetch_epic_free() -> list[dict]:
    """获取 Epic 当前免费游戏列表"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.get(EPIC_API, params=EPIC_PARAMS, headers=EPIC_HEADERS)
        resp.raise_for_status()
        data = resp.json()

    elements = data.get("data", {}).get("Catalog", {}).get("searchStore", {}).get("elements", [])
    free_games = []

    for game in elements:
        # 检查是否有有效的免费促销
        promotions = game.get("promotions", {})
        if not promotions:
            continue

        current_offers = promotions.get("promotionalOffers", [])
        upcoming_offers = promotions.get("upcomingPromotionalOffers", [])

        # 当前免费
        is_current_free = False
        end_date = None
        for promo_group in current_offers:
            for offer in promo_group.get("promotionalOffers", []):
                if offer.get("discountSetting", {}).get("discountPercentage") == 0:
                    is_current_free = True
                    end_str = offer.get("endDate", "")
                    if end_str:
                        try:
                            end_date = datetime.fromisoformat(end_str.replace("Z", "+00:00")).astimezone(CST)
                        except Exception:
                            pass

        if not is_current_free:
            continue

        # 提取游戏信息
        title = game.get("title", "")
        description = game.get("description", "")
        original_price = game.get("price", {}).get("totalPrice", {}).get("fmtPrice", {}).get("originalPrice", "未知")
        developer = game.get("seller", {}).get("name", "")

        # 提取图片
        image_url = ""
        for img in game.get("keyImages", []):
            if img.get("type") in ("Thumbnail", "DieselStoreFrontWide", "OfferImageWide"):
                image_url = img.get("url", "")
                break

        # 提取商店链接
        store_url = ""
        for mapping in game.get("offerMappings", []):
            slug = mapping.get("pageSlug", "")
            if slug:
                store_url = f"https://store.epicgames.com/zh-CN/p/{slug}"
                break
        if not store_url:
            for mapping in game.get("catalogNs", {}).get("mappings", []):
                slug = mapping.get("pageSlug", "")
                if slug:
                    store_url = f"https://store.epicgames.com/zh-CN/p/{slug}"
                    break
        if not store_url:
            for attr in game.get("customAttributes", []):
                if attr.get("key") == "productSlug":
                    store_url = f"https://store.epicgames.com/zh-CN/p/{attr.get('value', '')}"
                    break

        free_games.append({
            "title": title,
            "description": description[:100] + "..." if len(description) > 100 else description,
            "original_price": original_price,
            "developer": developer,
            "end_date": end_date.strftime("%m月%d日 %H:%M") if end_date else "未知",
            "image_url": image_url,
            "store_url": store_url,
        })

    return free_games


def format_epic_free(games: list[dict]) -> str:
    """格式化免费游戏信息为文本"""
    if not games:
        return "🎮 当前没有免费游戏"

    lines = ["🎮 Epic 本周喜加一"]
    lines.append("━" * 20)

    for i, game in enumerate(games, 1):
        lines.append(f"📌 {game['title']}")
        if game['developer']:
            lines.append(f"   开发商: {game['developer']}")
        lines.append(f"   原价: {game['original_price']}")
        lines.append(f"   截止: {game['end_date']}")
        if game['description']:
            lines.append(f"   简介: {game['description']}")
        if game['store_url']:
            lines.append(f"   领取: {game['store_url']}")
        if i < len(games):
            lines.append("")

    lines.append("━" * 20)
    lines.append("🔗 前往 Epic 商店领取吧~")

    return "\n".join(lines)


def _load_push_history() -> dict:
    """加载推送历史"""
    if not _HISTORY_FILE.exists():
        return {}
    try:
        return json.loads(_HISTORY_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save_push_history(history: dict):
    """保存推送历史"""
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    _HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), "utf-8")


def check_and_update_history(job_id: str, games: list[dict]) -> bool:
    """检查是否需要推送（内容有变化才推送），返回 True 表示需要推送"""
    if not games:
        return False

    # 生成内容指纹
    content = json.dumps([g["title"] + g["store_url"] for g in games], sort_keys=True)
    fingerprint = hashlib.md5(content.encode()).hexdigest()

    history = _load_push_history()
    if history.get(job_id) == fingerprint:
        return False

    history[job_id] = fingerprint
    _save_push_history(history)
    return True
