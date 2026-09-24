"""
基础命令插件 — 新闻、天气、Epic、KFC、油价、B站动态、帮助
直接复用 ai_plugin 下的现有模块，通过 sys.path 引入
"""
import asyncio
import hashlib
import sys
import os
import re
import logging
import importlib
import random
import tempfile
import time
import textwrap
from datetime import date, timedelta
from pathlib import Path

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
import astrbot.api.message_components as Comp

logger = logging.getLogger("jiang_commands")


def _apply_content_service_config(config: AstrBotConfig) -> None:
    mappings = {
        "content_api_id": "JIANG_CONTENT_API_ID",
        "content_api_key": "JIANG_CONTENT_API_KEY",
        "weather_detail_token": "JIANG_WEATHER_DETAIL_TOKEN",
        "random_image_proxy": "JIANG_RANDOM_IMAGE_PROXY",
    }
    for key, environment_name in mappings.items():
        value = str(config.get(key, "") or "").strip()
        os.environ[environment_name] = value

_WEATHER_PATTERN = re.compile(
    r"^\s*(.+?)天气(?:\s*([0-9]{2,4}|[0-9]{1,2}\s*(?:月|[-/.])\s*"
    r"[0-9]{1,2}\s*日?))?\s*$"
)
_WEATHER_MAX_OFFSET = 30
_OILPRICE_PATTERN = re.compile(r"^\s*油价\s*(.+?)\s*$")
_RANDOM_IMAGE_URLS = (
    "https://boudoir.ortlinde.com/random",
    "https://acg.yaohud.cn/dm/adaptive.php",
)
_RANDOM_IMAGE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/137.0.0.0 Safari/537.36"
    )
}


def _log_weather_warmup(task: asyncio.Task) -> None:
    try:
        task.result()
    except asyncio.CancelledError:
        pass
    except Exception as exc:
        logger.warning(f"天气服务预热失败: {exc}")


def _weather_date_parts(value: str) -> list[tuple[int, int]]:
    """把日期后缀拆成所有可能的月、日组合。"""
    compact = re.sub(r"\s+", "", value or "").rstrip("日")
    separated = re.fullmatch(r"(\d{1,2})(?:月|[-/.])(\d{1,2})", compact)
    if separated:
        return [(int(separated.group(1)), int(separated.group(2)))]
    if not compact.isdigit() or not 2 <= len(compact) <= 4:
        return []
    if len(compact) == 2:
        return [(int(compact[0]), int(compact[1]))]
    if len(compact) == 4:
        return [(int(compact[:2]), int(compact[2:]))]
    # 三位数字同时尝试 MDD 与 MMD，例如 121 => 1/21 或 12/1。
    return [
        (int(compact[0]), int(compact[1:])),
        (int(compact[:2]), int(compact[2])),
    ]


def _resolve_weather_date(
    value: str,
    today: date | None = None,
) -> tuple[int | None, str | None]:
    """在未来一个月内消解日期后缀，返回天数偏移或用户提示。"""
    current = today or date.today()
    parts = list(dict.fromkeys(_weather_date_parts(value)))
    valid_parts: list[tuple[int, int]] = []
    candidates: set[date] = set()
    for month, day in parts:
        part_is_valid = False
        for year in (current.year - 1, current.year, current.year + 1):
            try:
                candidate = date(year, month, day)
            except ValueError:
                continue
            part_is_valid = True
            candidates.add(candidate)
        if part_is_valid:
            valid_parts.append((month, day))

    if not valid_parts:
        return None, "日期格式不对，请使用 902、0902、9月2日 或 9-2。"

    in_range = sorted(
        candidate
        for candidate in candidates
        if 0 <= (candidate - current).days <= _WEATHER_MAX_OFFSET
    )
    if len(in_range) == 1:
        return (in_range[0] - current).days, None

    range_end = current + timedelta(days=_WEATHER_MAX_OFFSET)
    range_text = (
        f"目前支持未来一个月（{current.month}月{current.day}日—"
        f"{range_end.month}月{range_end.day}日）的预报。"
    )
    if len(valid_parts) > 1:
        options = "、".join(f"{month}月{day}日" for month, day in valid_parts)
        examples = " 或 ".join(f"{month:02d}{day:02d}" for month, day in valid_parts)
        return None, f"{value} 可能是 {options}，请写成 {examples}。{range_text}"
    month, day = valid_parts[0]
    return None, f"{month}月{day}日不在可查询范围内。{range_text}"


_TAROT_MAJOR_ARCANA = (
    ("愚者", "新的开始、冒险、自由"),
    ("魔术师", "行动力、资源整合、创造"),
    ("女祭司", "直觉、秘密、静观"),
    ("女皇", "滋养、丰盛、关系生长"),
    ("皇帝", "秩序、边界、掌控"),
    ("教皇", "传统、学习、规则"),
    ("恋人", "选择、吸引、价值一致"),
    ("战车", "推进、意志、胜利"),
    ("力量", "温柔的勇气、自控、耐心"),
    ("隐士", "独处、内省、寻找答案"),
    ("命运之轮", "转机、循环、变化"),
    ("正义", "平衡、判断、因果"),
    ("倒吊人", "暂停、换角度、牺牲"),
    ("死神", "结束、蜕变、旧事翻篇"),
    ("节制", "调和、疗愈、适度"),
    ("恶魔", "执念、诱惑、被困住"),
    ("高塔", "突变、瓦解、真相暴露"),
    ("星星", "希望、修复、远方的光"),
    ("月亮", "迷雾、潜意识、不确定"),
    ("太阳", "明朗、活力、成功"),
    ("审判", "觉醒、复盘、召唤"),
    ("世界", "完成、整合、新阶段"),
)
_TAROT_SUITS = {
    "权杖": ("行动、热情、创造力", "点燃这件事的火"),
    "圣杯": ("情绪、关系、感受", "照见心里的水位"),
    "宝剑": ("想法、沟通、冲突", "把问题切清楚"),
    "星币": ("现实、资源、稳定", "落到现实层面的土壤"),
}
_TAROT_MINOR_RANKS = (
    ("王牌", "种子、机会、起点"),
    ("二", "选择、平衡、协作"),
    ("三", "扩展、成果、交流"),
    ("四", "稳定、休整、停滞"),
    ("五", "摩擦、短缺、挑战"),
    ("六", "流动、互助、回归"),
    ("七", "评估、防守、耐心"),
    ("八", "推进、练习、变化"),
    ("九", "积累、临界、独处"),
    ("十", "完成、压力、收束"),
    ("侍从", "消息、学习、试探"),
    ("骑士", "推进、表达、追逐"),
    ("王后", "成熟、照料、内在力量"),
    ("国王", "掌控、责任、外在权威"),
)
_TAROT_SPREAD = ("现状", "阻碍", "建议", "走向")
_TAROT_CARD_DIR = Path(__file__).parent / "tarot_cards"
_TAROT_FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
_TAROT_FONT_NORMAL = "C:/Windows/Fonts/msyh.ttc"
_TAROT_FONT_FALLBACK = "C:/Windows/Fonts/simhei.ttf"
_TAROT_SYMBOLS = {
    "愚者": "0",
    "魔术师": "I",
    "女祭司": "II",
    "女皇": "III",
    "皇帝": "IV",
    "教皇": "V",
    "恋人": "VI",
    "战车": "VII",
    "力量": "VIII",
    "隐士": "IX",
    "命运之轮": "X",
    "正义": "XI",
    "倒吊人": "XII",
    "死神": "XIII",
    "节制": "XIV",
    "恶魔": "XV",
    "高塔": "XVI",
    "星星": "XVII",
    "月亮": "XVIII",
    "太阳": "XIX",
    "审判": "XX",
    "世界": "XXI",
    "权杖": "W",
    "圣杯": "C",
    "宝剑": "S",
    "星币": "P",
}
_TAROT_PALETTES = {
    "major": ((34, 25, 58), (212, 176, 88), (101, 72, 152)),
    "权杖": ((63, 28, 28), (232, 148, 85), (126, 54, 45)),
    "圣杯": ((23, 45, 67), (118, 194, 220), (50, 99, 142)),
    "宝剑": ((35, 38, 60), (179, 193, 230), (78, 84, 137)),
    "星币": ((31, 59, 43), (155, 205, 125), (68, 119, 74)),
}


def _build_tarot_deck() -> list[tuple[str, str]]:
    deck = list(_TAROT_MAJOR_ARCANA)
    for suit, (suit_keywords, suit_note) in _TAROT_SUITS.items():
        for rank, rank_keywords in _TAROT_MINOR_RANKS:
            deck.append((f"{suit}{rank}", f"{suit_keywords}；{rank_keywords}；{suit_note}"))
    return deck


_TAROT_DECK = _build_tarot_deck()


def _strip_system_identity_prefix(text: str) -> str:
    cleaned = (text or "").strip()
    if cleaned.startswith("[系统身份提示：") and "]\n" in cleaned:
        cleaned = cleaned.split("]\n", 1)[1].lstrip()
    return cleaned


def _random_image_proxy_override() -> dict[str, str] | None:
    """Return the plugin-specific proxy override, if one was configured."""
    proxy_url = os.getenv("JIANG_RANDOM_IMAGE_PROXY", "").strip()
    if not proxy_url:
        return None
    return {"http": proxy_url, "https": proxy_url}


def _is_valid_random_image_response(response) -> bool:
    if response is None:
        return False
    content_type = (response.headers.get("Content-Type") or "").lower()
    return (
        response.status_code == 200
        and content_type.startswith("image/")
        and len(response.content) > 1000
    )


def _draw_tarot_cards(count: int) -> list[dict[str, str]]:
    cards = random.sample(_TAROT_DECK, k=min(count, len(_TAROT_DECK)))
    results = []
    for name, keywords in cards:
        reversed_card = random.choice((False, True))
        results.append(
            {
                "name": name,
                "orientation": "逆位" if reversed_card else "正位",
                "keywords": keywords,
                "tone": "提醒先别硬冲，换个姿势会顺一点"
                if reversed_card
                else "这张牌的力道比较顺，可以主动推进",
            }
        )
    return results


def _format_single_tarot(card: dict[str, str]) -> str:
    return (
        f"你抽到：{card['orientation']}{card['name']}\n"
        f"关键词：{card['keywords']}\n"
        f"牌意：{card['tone']}。"
    )


def _format_tarot_spread(cards: list[dict[str, str]]) -> str:
    lines = ["给你抽了四张："]
    for position, card in zip(_TAROT_SPREAD, cards):
        lines.append(
            f"{position}：{card['orientation']}{card['name']} - "
            f"{card['keywords']}。{card['tone']}。"
        )
    lines.append("整体看，先抓住最亮的那条线，别被一时情绪带跑。")
    return "\n".join(lines)


def _format_tarot_spread_card(position: str, card: dict[str, str], index: int) -> str:
    return (
        f"第 {index} 张｜{position}\n"
        f"{card['orientation']}{card['name']}\n"
        f"关键词：{card['keywords']}\n"
        f"牌意：{card['tone']}。"
    )


def _tarot_font(size: int, bold: bool = False):
    from PIL import ImageFont

    candidates = (
        _TAROT_FONT_BOLD if bold else _TAROT_FONT_NORMAL,
        _TAROT_FONT_FALLBACK,
    )
    for path in candidates:
        try:
            return ImageFont.truetype(path, size, index=0)
        except Exception:
            continue
    return ImageFont.load_default()


def _tarot_suit(card_name: str) -> str:
    for suit in _TAROT_SUITS:
        if card_name.startswith(suit):
            return suit
    return "major"


def _tarot_symbol(card_name: str) -> str:
    if card_name in _TAROT_SYMBOLS:
        return _TAROT_SYMBOLS[card_name]
    for suit, symbol in _TAROT_SYMBOLS.items():
        if card_name.startswith(suit):
            return symbol
    return "T"


def _draw_centered_text(draw, box, text: str, font, fill) -> None:
    bbox = draw.textbbox((0, 0), text, font=font)
    width = bbox[2] - bbox[0]
    height = bbox[3] - bbox[1]
    x1, y1, x2, y2 = box
    draw.text(
        (x1 + (x2 - x1 - width) / 2, y1 + (y2 - y1 - height) / 2),
        text,
        font=font,
        fill=fill,
    )


def _wrapped_lines(text: str, width: int) -> list[str]:
    parts = re.split(r"[；;，,、]\s*", text)
    lines: list[str] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        lines.extend(textwrap.wrap(part, width=width) or [part])
    return lines


def _render_tarot_card_image(
    card: dict[str, str],
    *,
    position: str | None = None,
    index: int | None = None,
) -> str:
    """Render a lightweight tarot card PNG and return its filesystem path."""
    from PIL import Image, ImageDraw

    _TAROT_CARD_DIR.mkdir(parents=True, exist_ok=True)
    suit = _tarot_suit(card["name"])
    bg, accent, panel = _TAROT_PALETTES.get(suit, _TAROT_PALETTES["major"])
    orientation = card["orientation"]
    name = card["name"]
    position_label = f"第 {index} 张｜{position}" if position and index else "单张牌"
    file_key = hashlib.sha1(
        f"{orientation}:{name}:{position or 'single'}:{time.time_ns()}".encode("utf-8")
    ).hexdigest()[:16]
    path = _TAROT_CARD_DIR / f"tarot_{file_key}.png"

    w, h = 720, 1080
    img = Image.new("RGB", (w, h), bg)
    draw = ImageDraw.Draw(img)
    title_font = _tarot_font(54, bold=True)
    subtitle_font = _tarot_font(30, bold=False)
    symbol_font = _tarot_font(132, bold=True)
    body_font = _tarot_font(31, bold=False)
    small_font = _tarot_font(24, bold=False)

    draw.rounded_rectangle([28, 28, w - 28, h - 28], radius=36, outline=accent, width=6)
    draw.rounded_rectangle([48, 48, w - 48, h - 48], radius=28, outline=(235, 226, 188), width=2)
    draw.rounded_rectangle([80, 86, w - 80, 158], radius=18, fill=panel)
    _draw_centered_text(draw, (80, 86, w - 80, 158), position_label, subtitle_font, (238, 232, 209))

    draw.text((86, 196), orientation, font=subtitle_font, fill=accent)
    title = name if len(name) <= 6 else "\n".join(textwrap.wrap(name, width=5))
    draw.multiline_text((86, 240), title, font=title_font, fill=(250, 245, 218), spacing=10)

    center_box = [122, 390, w - 122, 700]
    draw.rounded_rectangle(center_box, radius=36, fill=(18, 18, 24), outline=accent, width=4)
    for offset in (24, 52, 82):
        draw.ellipse(
            [
                center_box[0] + offset,
                center_box[1] + offset,
                center_box[2] - offset,
                center_box[3] - offset,
            ],
            outline=tuple(min(255, c + 35) for c in panel),
            width=2,
        )
    _draw_centered_text(draw, center_box, _tarot_symbol(name), symbol_font, accent)
    if orientation == "逆位":
        draw.rounded_rectangle([456, 654, 610, 710], radius=14, fill=(92, 31, 45))
        _draw_centered_text(draw, (456, 654, 610, 710), "逆位", subtitle_font, (255, 224, 224))

    y = 748
    draw.text((86, y), "关键词", font=subtitle_font, fill=accent)
    y += 46
    for line in _wrapped_lines(card["keywords"], 15)[:5]:
        draw.text((100, y), line, font=body_font, fill=(238, 235, 220))
        y += 42

    draw.line([(86, h - 152), (w - 86, h - 152)], fill=accent, width=2)
    for line in textwrap.wrap(card["tone"], width=18)[:2]:
        draw.text((86, y + 16), line, font=small_font, fill=(218, 212, 194))
        y += 34
    _draw_centered_text(
        draw,
        (86, h - 122, w - 86, h - 78),
        "姜小妹塔罗",
        small_font,
        (180, 170, 150),
    )

    img.save(path, format="PNG", optimize=True)
    return str(path)


def _cleanup_old_tarot_images(max_age_seconds: int = 86400) -> None:
    try:
        if not _TAROT_CARD_DIR.exists():
            return
        cutoff = time.time() - max_age_seconds
        for item in _TAROT_CARD_DIR.glob("*.png"):
            try:
                if item.stat().st_mtime < cutoff:
                    item.unlink(missing_ok=True)
            except OSError:
                continue
    except Exception as exc:
        logger.debug("清理塔罗牌缓存失败: %s", exc)


def _tarot_chain_result(event: AstrMessageEvent, image_path: str, text: str):
    return event.chain_result(
        [
            Comp.Image.fromFileSystem(image_path),
            Comp.Plain(text),
        ]
    )


def _fetch_random_image(
    proxies: dict[str, str] | None = None,
    attempts: int = 2,
    timeout: int = 15,
):
    import requests as _req

    last_response = None
    for attempt in range(max(1, attempts)):
        for url in _RANDOM_IMAGE_URLS:
            detected_proxies = proxies or _req.utils.get_environ_proxies(url)
            routes = []
            if detected_proxies:
                routes.append(("proxy", True, proxies))
            routes.append(("direct", False, None))

            for route_name, trust_env, route_proxies in routes:
                session = _req.Session()
                session.trust_env = trust_env
                try:
                    resp = session.get(
                        url,
                        headers=_RANDOM_IMAGE_HEADERS,
                        proxies=route_proxies,
                        timeout=timeout,
                    )
                except _req.RequestException as exc:
                    logger.warning(
                        "随机图片源请求失败: source=%s route=%s error=%s: %s",
                        url,
                        route_name,
                        type(exc).__name__,
                        exc,
                    )
                    continue

                last_response = resp
                content_type = (resp.headers.get("Content-Type") or "").lower()
                if _is_valid_random_image_response(resp):
                    return resp
                logger.warning(
                    "随机图片源返回无效内容: source=%s route=%s status=%s "
                    "content_type=%s bytes=%s",
                    url,
                    route_name,
                    resp.status_code,
                    content_type or "unknown",
                    len(resp.content),
                )
        if attempt < attempts - 1:
            time.sleep(2)
    return last_response

# NoneBot 桩（ai_plugin 依赖 nonebot）
_PLUGINS_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import _nonebot_stubs
_nonebot_stubs.setup()

try:
    from jiang_capability_router.state import record_action as _record_action
except Exception:
    _record_action = None

# 将 ai_plugin 父目录加入 sys.path，复用现有模块
_AI_PLUGIN_PARENT = str(Path(__file__).resolve().parent.parent.parent.parent / "ai_plugin")
if _AI_PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _AI_PLUGIN_PARENT)

import ai_plugin.news as _news_mod
import ai_plugin.weather as _weather_mod
import ai_plugin.weather_image as _weather_image_mod
import ai_plugin.epic as _epic_mod
import ai_plugin.kfc as _kfc_mod
import ai_plugin.oilprice as _oilprice_mod
import ai_plugin.bilibili_dynamic as _bili_mod
import ai_plugin.help_card as _help_mod

# 共享模块不在本插件的热更新清理范围内；显式刷新本插件依赖的可重载模块。
_bili_mod = importlib.reload(_bili_mod)
_oilprice_mod = importlib.reload(_oilprice_mod)
_weather_mod = importlib.reload(_weather_mod)
_weather_image_mod = importlib.reload(_weather_image_mod)

fetch_hot_news = _news_mod.fetch_hot_news
format_news = _news_mod.format_news
fetch_weather = _weather_mod.fetch_weather
fetch_weather_alapi = _weather_mod.fetch_weather_alapi
fetch_weather_open_meteo = _weather_mod.fetch_weather_open_meteo
fetch_weather_target_date = _weather_mod.fetch_weather_target_date
merge_weather_supplements = _weather_mod.merge_weather_supplements
format_weather = _weather_mod.format_weather
warm_weather_client = _weather_mod.warm_weather_client
build_weather_card_data = _weather_image_mod.build_weather_card_data
render_weather_image = _weather_image_mod.render_weather_image
render_weather_target_image = _weather_image_mod.render_weather_target_image
warm_weather_renderer = _weather_image_mod.warm_weather_renderer
fetch_epic_free = _epic_mod.fetch_epic_free
format_epic_free = _epic_mod.format_epic_free
fetch_kfc_text = _kfc_mod.fetch_kfc_text
fetch_oilprice = _oilprice_mod.fetch_oilprice
format_oilprice = _oilprice_mod.format_oilprice
fetch_latest_dynamic_message = _bili_mod.fetch_latest_dynamic_message
generate_help_card = _help_mod.generate_help_card


def _record_event_action(
    event: AstrMessageEvent,
    capability_id: str,
    display_name: str,
    details: dict | None = None,
) -> None:
    if not _record_action:
        return
    try:
        _record_action(
            event.unified_msg_origin,
            capability_id,
            display_name,
            "command",
            details=details,
        )
    except Exception as exc:
        logger.warning(f"记录最近能力动作失败: {exc}")

# 预生成帮助卡片路径
_HELP_IMG = str(Path(__file__).parent / "help_card.png")
_IDEAL_VOICE_PATH = (
    Path(__file__).resolve().parents[3] / "docs" / "audio" / "lixiang-20s.silk"
)

# 帮助数据（与原 ai_plugin/__init__.py 一致）
HELP_DATA = {
    "常用命令": [
        ("\\新闻", "今日热点新闻"),
        ("\\城市天气", "如: \\绵阳天气、\\青岛天气902"),
        ("\\油价省份", "如: \\油价四川"),
        ("\\epic", "Epic喜加一"),
        ("\\kfc", "疯狂星期四文案"),
        ("\\理想", "发送20秒《理想进行曲》语音条"),
        ("\\燕云", "燕云十六声动态"),
    ],
    "AI 联网搜索": [
        ("@姜小妹 科普/查一下/介绍一下", "自动联网搜索总结"),
        ("@姜小妹 提问", "自由提问，AI 对话回复"),
    ],
    "AI 创作（直接说就行）": [
        ("@姜小妹 帮我画xxx", "文生图"),
        ("@姜小妹 P图/改图/图生图", "编辑图片（需引用图片）"),
    ],
    "塔罗牌": [
        ("\\占卜", "抽四张塔罗牌占卜"),
        ("\\塔罗牌", "抽一张塔罗牌"),
    ],
    "洛克王国": [
        ("\\洛克", "查看洛克全部指令"),
        ("\\洛克档案 \\战绩 \\背包", "查询角色数据"),
        ("\\洛克阵容 \\查蛋 \\配种", "阵容与宠物"),
        ("\\洛克交换大厅 \\远行商人", "交易与商店"),
        ("\\订阅远行商人 \\家园菜园", "订阅通知"),
    ],
    "定时任务（管理员）": [
        ("\\定时新闻 8点", "每日推送热点"),
        ("\\定时天气 绵阳 8点", "每日天气"),
        ("\\定时kfc 周四 12点", "每周KFC文案"),
        ("\\定时列表 \\定时删除", "管理定时任务"),
    ],
    "B站订阅（管理员）": [
        ("\\订阅动态 UID", "订阅B站用户动态"),
        ("\\取消订阅动态 UID", "取消订阅"),
        ("\\订阅列表", "查看当前订阅"),
    ],
    "视频解析": [
        ("发送链接", "抖音/TikTok/B站链接自动解析"),
    ],
    "管理员指令（\\前缀）": [
        ("\\启用 \\禁用 插件名", "插件开关"),
        ("\\插件列表", "查看所有插件"),
        ("\\切换模型 \\图片模型", "切换AI后端"),
        ("\\语音 文字", "TTS语音合成"),
    ],
    "米游社": [
        ("\\米游社帮助", "查看米游社相关功能"),
    ],
}


def _ensure_help_image():
    """确保帮助卡片图片存在"""
    if not os.path.exists(_HELP_IMG):
        try:
            data = generate_help_card(HELP_DATA, bot_name="姜小妹")
            with open(_HELP_IMG, "wb") as f:
                f.write(data)
            logger.info("已生成帮助卡片图片")
        except Exception as e:
            logger.warning(f"生成帮助卡片失败: {e}")


class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig) -> None:
        self.context = context
        self.config = config
        _apply_content_service_config(config)
        _ensure_help_image()
        try:
            task = asyncio.get_running_loop().create_task(warm_weather_renderer())
            task.add_done_callback(_log_weather_warmup)
            client_task = asyncio.get_running_loop().create_task(warm_weather_client())
            client_task.add_done_callback(_log_weather_warmup)
        except Exception as exc:
            logger.debug(f"天气卡片渲染器预热未启动: {exc}")

    # ── /新闻 ──────────────────────────────────────────
    @filter.command("新闻")
    async def news(self, event: AstrMessageEvent):
        """今日热点新闻"""
        try:
            items = await fetch_hot_news(20)
            msg = format_news(items)
            _record_event_action(event, "info.news", "热点新闻", {"count": 20})
            yield event.plain_result(msg)
        except Exception as e:
            logger.exception(f"/新闻 失败: {e}")
            yield event.plain_result(f"获取新闻失败: {e}")

    # ── /xx天气 ─────────────────────────────────────────
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def weather(self, event: AstrMessageEvent):
        """城市天气预报 — 图片卡片"""
        text = _strip_system_identity_prefix(event.get_message_str()).strip()
        if text.startswith(("/", "\\")):
            text = text[1:].strip()
        m = _WEATHER_PATTERN.match(text)
        if not m:
            return
        place = m.group(1).strip()
        date_suffix = (m.group(2) or "").strip()
        logger.info(f"[WEATHER] 原始 place: {place!r}")
        place = re.sub(r"^@.*?\s+", "", place).strip()
        if not place:
            yield event.plain_result("请指定城市名称，如: /绵阳天气")
            return
        target_offset: int | None = None
        if date_suffix:
            target_offset, date_error = _resolve_weather_date(date_suffix)
            if date_error:
                yield event.plain_result(date_error)
                return
        try:
            if target_offset is not None and target_offset > 6:
                daily_result = await fetch_weather(place)
                target_data = await fetch_weather_target_date(
                    daily_result,
                    target_offset,
                )
                if not target_data:
                    raise ValueError("指定日期天气数据暂不可用")
                target_data["city"] = (
                    daily_result.get("shi")
                    or daily_result.get("name")
                    or place
                )
                img_path = await render_weather_target_image(target_data, place)
                _record_event_action(
                    event,
                    "info.weather",
                    "指定日期天气",
                    {"city": place, "date_offset": target_offset},
                )
                yield event.image_result(img_path)
                return

            alapi_task = asyncio.create_task(fetch_weather_alapi(place))
            try:
                daily_result = await fetch_weather(place)
            except Exception:
                alapi_task.cancel()
                await asyncio.gather(alapi_task, return_exceptions=True)
                raise
            open_meteo_result, alapi_result = await asyncio.gather(
                fetch_weather_open_meteo(daily_result),
                alapi_task,
                return_exceptions=True,
            )
            supplemental_data = merge_weather_supplements(
                open_meteo_result if isinstance(open_meteo_result, dict) else None,
                alapi_result if isinstance(alapi_result, dict) else None,
            )
            card_data = build_weather_card_data(
                daily_result,
                place,
                supplemental_data,
            )
            card_data["selected_offset"] = target_offset
            img_path = await render_weather_image(card_data, daily_result)
            _record_event_action(
                event,
                "info.weather",
                "天气预报",
                {"city": place, "date_offset": target_offset},
            )
            yield event.image_result(img_path)
        except Exception as e:
            logger.exception(f"/{place}天气 失败: {e}")
            yield event.plain_result(f"获取天气失败: {e}")

    # ── /epic ──────────────────────────────────────────
    @filter.command("epic")
    async def epic(self, event: AstrMessageEvent):
        """Epic 喜加一"""
        try:
            games = await fetch_epic_free()
            msg = format_epic_free(games)
            _record_event_action(event, "info.epic", "Epic 喜加一")
            yield event.plain_result(msg)
        except Exception as e:
            logger.exception(f"/epic 失败: {e}")
            yield event.plain_result(f"获取 Epic 免费游戏失败: {e}")

    # ── /kfc ───────────────────────────────────────────
    @filter.command("kfc")
    async def kfc(self, event: AstrMessageEvent):
        """KFC 疯狂星期四文案"""
        try:
            text = await fetch_kfc_text()
            _record_event_action(event, "content.kfc", "疯狂星期四文案")
            yield event.plain_result(text)
        except Exception as e:
            logger.exception(f"/kfc 失败: {e}")
            yield event.plain_result("V我50！")

    # ── /理想 ───────────────────────────────────────────
    @filter.command("理想")
    async def ideal(self, event: AstrMessageEvent):
        """发送20秒《理想进行曲》语音条"""
        if not _IDEAL_VOICE_PATH.is_file():
            logger.error("/理想 语音素材不存在: %s", _IDEAL_VOICE_PATH)
            yield event.plain_result("理想语音素材丢了，暂时发不出来")
            return
        event.stop_event()
        payload = {
            "message": [
                {
                    "type": "record",
                    "data": {"file": str(_IDEAL_VOICE_PATH)},
                }
            ]
        }
        try:
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                result = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                result = await event.bot.call_action("send_private_msg", **payload)
        except Exception as exc:
            logger.exception("/理想 语音条发送失败: %s", exc)
            yield event.plain_result("理想语音条发送失败，稍后再试")
            return
        _record_event_action(event, "content.ideal_voice", "理想语音条")
        logger.info("/理想 语音条已发送: %s", result)

    # ── /油价xx ────────────────────────────────────────
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def oilprice(self, event: AstrMessageEvent):
        """今日油价"""
        text = _strip_system_identity_prefix(event.get_message_str()).strip()
        if text.startswith(("/", "\\")):
            text = text[1:].strip()
        m = _OILPRICE_PATTERN.match(text)
        if not m:
            return
        province = m.group(1).strip()
        if not province:
            yield event.plain_result("请指定省份，如: \\油价四川 或 \\油价 北京")
            return
        try:
            data = await fetch_oilprice(province)
            msg = format_oilprice(data)
            _record_event_action(
                event,
                "info.oilprice",
                "今日油价",
                {"province": province},
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.exception(f"/油价 失败: {e}")
            yield event.plain_result(f"获取油价失败: {e}")

    # ── /燕云 ──────────────────────────────────────────
    @filter.command("燕云")
    async def yanyun(self, event: AstrMessageEvent):
        """燕云十六声官方动态"""
        try:
            text, image_path = await fetch_latest_dynamic_message(
                "1567141152",
                title="燕云十六声",
            )
            _record_event_action(
                event,
                "info.yanyun_dynamic",
                "燕云十六声动态",
                {"count": 1},
            )
            chain = []
            if image_path:
                chain.append(Comp.Image.fromFileSystem(image_path))
            chain.append(Comp.Plain(text))
            try:
                yield event.chain_result(chain)
            finally:
                if image_path:
                    Path(image_path).unlink(missing_ok=True)
        except Exception as e:
            logger.exception(f"/燕云 失败: {e}")
            yield event.plain_result(f"获取燕云动态失败: {e}")

    # ── /塔罗牌 ────────────────────────────────────────
    @filter.command("塔罗牌")
    async def tarot_card(self, event: AstrMessageEvent):
        """抽一张塔罗牌"""
        try:
            _cleanup_old_tarot_images()
            card = _draw_tarot_cards(1)[0]
            _record_event_action(
                event,
                "content.tarot_card",
                "塔罗牌",
                {"count": 1, "card": card["name"], "orientation": card["orientation"]},
            )
            image_path = _render_tarot_card_image(card)
            yield _tarot_chain_result(event, image_path, _format_single_tarot(card))
        except Exception as e:
            logger.exception(f"/塔罗牌 失败: {e}")
            yield event.plain_result(f"抽牌失败: {e}")

    # ── /占卜 ──────────────────────────────────────────
    @filter.command("占卜")
    async def tarot_divination(self, event: AstrMessageEvent):
        """抽四张塔罗牌占卜"""
        try:
            _cleanup_old_tarot_images()
            cards = _draw_tarot_cards(4)
            _record_event_action(
                event,
                "content.tarot_divination",
                "塔罗占卜",
                {
                    "count": 4,
                    "cards": [
                        {
                            "name": card["name"],
                            "orientation": card["orientation"],
                        }
                        for card in cards
                    ],
                },
            )
            yield event.plain_result("牌阵起好了，给你抽四张。")
            for index, (position, card) in enumerate(zip(_TAROT_SPREAD, cards), start=1):
                if index > 1:
                    await asyncio.sleep(5)
                image_path = _render_tarot_card_image(card, position=position, index=index)
                yield _tarot_chain_result(
                    event,
                    image_path,
                    _format_tarot_spread_card(position, card, index),
                )
            await asyncio.sleep(5)
            yield event.plain_result("整体看，先抓住最亮的那条线，别被一时情绪带跑。")
        except Exception as e:
            logger.exception(f"/占卜 失败: {e}")
            yield event.plain_result(f"占卜失败: {e}")

    # ── /帮助 ──────────────────────────────────────────
    @filter.command("帮助")
    async def help(self, event: AstrMessageEvent):
        """显示帮助信息"""
        if os.path.exists(_HELP_IMG):
            yield event.image_result(_HELP_IMG)
        else:
            # 兜底：文本帮助
            lines = ["姜小妹命令指南："]
            for sect, items in HELP_DATA.items():
                lines.append(f"\n【{sect}】")
                for cmd, desc in items:
                    lines.append(f"  {cmd} — {desc}")
            yield event.plain_result("\n".join(lines))

    # ── 图来（随机图片，无需前缀） ─────────────────────
    @filter.event_message_type(filter.EventMessageType.ALL)
    async def random_image(self, event: AstrMessageEvent):
        """发送随机图片，强匹配 '图来'，无需 / 或 @"""
        text = _strip_system_identity_prefix(event.message_str).strip()
        if text.startswith(("/", "\\")):
            text = text[1:].strip()
        if text != "图来":
            return
        event.stop_event()
        try:
            resp = _fetch_random_image(
                _random_image_proxy_override(),
                attempts=2,
                timeout=15,
            )
            if _is_valid_random_image_response(resp):
                content_type = (resp.headers.get("Content-Type") or "").lower()
                suffix = ".jpg"
                if "png" in content_type:
                    suffix = ".png"
                elif "webp" in content_type:
                    suffix = ".webp"
                elif "gif" in content_type:
                    suffix = ".gif"

                with tempfile.NamedTemporaryFile(
                    prefix="jiang_random_",
                    suffix=suffix,
                    delete=False,
                ) as tmp:
                    tmp.write(resp.content)
                    tmp_path = tmp.name

                sent = await self._send_random_image_with_recall(event, tmp_path, 20)
                if not sent:
                    logger.info("[图来撤回] direct send failed; fallback to image_result without auto recall")
                    yield event.image_result(tmp_path)
            elif resp is not None and resp.status_code == 404:
                yield event.plain_result("图库空了，等会儿再来~")
            else:
                status_code = getattr(resp, "status_code", "unknown")
                yield event.plain_result(f"图片接口返回 {status_code}，稍后再试")
        except Exception as e:
            logger.exception(f"图来失败: {e}")
            yield event.plain_result("获取图片失败，网络开小差了")

    async def _send_random_image_with_recall(
        self, event: AstrMessageEvent, image_path: str, delay: int
    ) -> bool:
        """Send through the OneBot adapter so the WeChat layer can bind a trace before recall."""
        try:
            message = [{"type": "image", "data": {"file": image_path}}]
            payload = {"message": message, "auto_recall_delay": delay}
            if event.get_group_id():
                payload["group_id"] = event.get_group_id()
                result = await event.bot.call_action("send_group_msg", **payload)
            else:
                payload["user_id"] = event.get_sender_id()
                result = await event.bot.call_action("send_private_msg", **payload)
            logger.info(f"[图来撤回] direct send registered: delay={delay}s, result={result}")
            return True
        except Exception as e:
            if type(e).__name__ == "ApiNotAvailable":
                logger.debug("[图来撤回] OneBot API not ready for direct send")
                return False
            logger.warning(f"[图来撤回] direct send failed: {type(e).__name__}: {e}")
            return False
