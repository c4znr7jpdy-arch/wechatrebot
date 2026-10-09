"""
B站用户动态获取与推送模块
使用 crawlers 库的 BilibiliWebCrawler（带 w_rid 签名）
"""
import os
import tempfile
from pathlib import Path
from datetime import datetime

import httpx
from nonebot import logger
from nonebot.adapters.onebot.v12 import MessageSegment

from .douyin.crawlers.bilibili.web.web_crawler import BilibiliWebCrawler


_crawler = BilibiliWebCrawler()

_SUMMARIZE_PROMPT = """你是一个B站动态摘要助手。把下面的动态列表改写为简洁有趣的中文摘要，要求：
- 保留每条的序号、类型标签、时间
- 每条内容精简到一句话（20-50字），突出核心信息
- 语气轻松自然，像朋友分享给你看的
- 不要加emoji，不要加多余的开头结尾
- 直接输出改写后的列表，格式和原文一致"""


async def _ai_summarize(raw_text: str) -> str:
    """用 MiniMax 润色动态摘要，失败则返回原文"""
    api_key = os.getenv("MINIMAX_API_KEY", "")
    if not api_key:
        return raw_text
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                "https://api.minimaxi.com/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json={
                    "model": "MiniMax-M2.7-highspeed",
                    "messages": [
                        {"role": "system", "content": _SUMMARIZE_PROMPT},
                        {"role": "user", "content": raw_text},
                    ],
                    "stream": False,
                    "max_tokens": 600,
                    "temperature": 0.5,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                result = data["choices"][0]["message"]["content"].strip()
                if result:
                    return result
    except Exception as e:
        logger.debug(f"[BILI] AI润色失败，使用原文: {e}")
    return raw_text


async def fetch_dynamics(uid: str, count: int = 5) -> list[dict]:
    """通过 crawlers 库获取用户最新动态（带 w_rid 签名）"""
    resp = await _crawler.fetch_user_dynamic(uid=uid, offset="")
    if resp.get("code") != 0:
        raise RuntimeError(f"B站API错误: {resp.get('message', 'unknown')}")

    items = resp.get("data", {}).get("items", [])
    result = []
    for item in items:
        modules = item.get("modules", {})
        author_mod = modules.get("module_author", {})
        dynamic = modules.get("module_dynamic", {})
        major = dynamic.get("major") or {}
        desc = dynamic.get("desc") or {}
        dtype = item.get("type", "")

        entry = {
            "dynamic_id": item.get("id_str", ""),
            "timestamp": author_mod.get("pub_ts", 0),
            "author": author_mod.get("name", ""),
            "avatar": author_mod.get("face", ""),
        }

        if dtype == "DYNAMIC_TYPE_AV":
            archive = major.get("archive", {})
            entry["type"] = "video"
            entry["title"] = archive.get("title", "")
            entry["bvid"] = archive.get("bvid", "")
            entry["description"] = archive.get("desc", "") or desc.get("text", "")
            entry["cover"] = archive.get("cover", "")
        elif dtype == "DYNAMIC_TYPE_DRAW":
            draw = major.get("draw", {})
            pics = draw.get("items", [])
            entry["type"] = "image"
            entry["text"] = desc.get("text", "")
            entry["pictures"] = [p.get("src", "") for p in pics if p.get("src")]
        elif dtype == "DYNAMIC_TYPE_WORD":
            entry["type"] = "text"
            entry["text"] = desc.get("text", "")
        elif dtype == "DYNAMIC_TYPE_ARTICLE":
            article = major.get("article", {})
            entry["type"] = "article"
            entry["title"] = article.get("title", "")
            entry["text"] = desc.get("text", "") or article.get("desc", "")
            entry["covers"] = [
                cover for cover in article.get("covers", []) if cover
            ]
            entry["article_url"] = article.get("jump_url", "")
        else:
            continue

        result.append(entry)
        if len(result) >= count:
            break
    return result


async def download_image(url: str) -> str | None:
    """下载图片到临时文件，返回本地路径"""
    if not url:
        return None
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url, headers={
                "Referer": "https://www.bilibili.com",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            })
            resp.raise_for_status()
            suffix = ".jpg"
            if "png" in resp.headers.get("content-type", ""):
                suffix = ".png"
            elif "webp" in resp.headers.get("content-type", ""):
                suffix = ".webp"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix, prefix="bili_")
            tmp.write(resp.content)
            tmp.close()
            return tmp.name
    except Exception as e:
        logger.warning(f"[BILI] 下载图片失败: {url} -> {e}")
        return None


def _ts_to_str(ts) -> str:
    if not ts:
        return ""
    try:
        return datetime.fromtimestamp(int(ts)).strftime("%m-%d %H:%M")
    except (ValueError, TypeError, OSError):
        return ""


def _truncate(text: str, limit: int = 100) -> str:
    text = text.replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit] + "..."
    return text


def _compact_summary(text: str, limit: int = 64) -> str:
    """提取适合微信气泡展示的一句话摘要。"""
    text = " ".join(str(text or "").split())
    if not text:
        return ""

    sentence_end = min(
        (
            index
            for mark in "。！？"
            if (index := text.find(mark)) >= 0
        ),
        default=-1,
    )
    if 12 <= sentence_end < limit:
        return text[: sentence_end + 1]
    if len(text) > limit:
        return text[:limit].rstrip("，,；;：: ") + "…"
    return text


def _normalize_url(url: str) -> str:
    url = str(url or "").strip()
    if url.startswith("//"):
        return f"https:{url}"
    if url.startswith("http://"):
        return f"https://{url.removeprefix('http://')}"
    return url


def dynamic_image_url(item: dict) -> str:
    """返回最能代表动态内容的一张图片，必要时回退到作者头像。"""
    if item.get("type") == "image":
        pictures = item.get("pictures", [])
        if pictures:
            return _normalize_url(pictures[0])
    if item.get("type") == "video" and item.get("cover"):
        return _normalize_url(item["cover"])
    if item.get("type") == "article":
        covers = item.get("covers", [])
        if covers:
            return _normalize_url(covers[0])
    return _normalize_url(item.get("avatar", ""))


def dynamic_link(item: dict) -> str:
    """返回动态对应的 B站页面。"""
    if item.get("type") == "video" and item.get("bvid"):
        return f"https://www.bilibili.com/video/{item['bvid']}"
    if item.get("type") == "article" and item.get("article_url"):
        return _normalize_url(item["article_url"])
    dynamic_id = str(item.get("dynamic_id", "") or "").strip()
    return f"https://t.bilibili.com/{dynamic_id}" if dynamic_id else ""


def format_latest_dynamic(item: dict, title: str = "") -> str:
    """将最新一条动态格式化成适合放在封面下方的短文案。"""
    dtype = item.get("type", "")
    type_tag = {
        "video": "视频",
        "image": "图文",
        "text": "动态",
        "article": "专栏",
    }.get(dtype, "动态")
    headline = str(item.get("title", "") or "").strip()
    body = str(
        item.get("text", "")
        or item.get("description", "")
        or ""
    ).strip()

    author = str(item.get("author", "") or "").strip()
    display_title = str(title or "").strip()
    if author and (
        not display_title
        or display_title.startswith("B站用户 ")
    ):
        display_title = author
    display_title = display_title or "B站"

    lines = [f"{display_title}｜最新动态"]
    if headline:
        lines.append(f"{type_tag} · {_compact_summary(headline, 56)}")
        summary = _compact_summary(body, 64)
        if summary and summary != headline:
            lines.extend(["", summary])
    else:
        lines.append(f"{type_tag} · {_compact_summary(body, 72) or '暂无文字内容'}")

    time_str = _ts_to_str(item.get("timestamp", 0))
    if time_str:
        lines.append(time_str)
    link = dynamic_link(item)
    if link:
        lines.append(f"查看原文：{link}")
    return "\n".join(lines)


async def fetch_latest_dynamic_message(
    uid: str,
    title: str = "",
) -> tuple[str, str | None]:
    """获取最新一条动态，并准备文本和一张本地图片。"""
    # 空间动态流可能包含置顶旧动态，多取几条后按发布时间选择真正最新的一条。
    items = await fetch_dynamics(uid, count=10)
    if not items:
        return f"{title or f'B站用户 {uid}'}暂无动态", None

    item = max(items, key=lambda value: int(value.get("timestamp", 0) or 0))
    text = format_latest_dynamic(item, title=title)
    image_path = await download_image(dynamic_image_url(item))
    return text, image_path


def _format_item(idx: int, item: dict) -> str:
    """格式化单条动态为文本行"""
    time_str = _ts_to_str(item.get("timestamp", 0))
    dtype = item["type"]
    type_tag = {"video": "视频", "image": "图文", "text": "动态", "article": "专栏"}.get(dtype, "动态")

    if dtype == "video":
        title = item.get("title", "")
        desc = item.get("description", "")
        content = title
        if desc and desc != title:
            content = f"{title} | {desc}"
        bvid = item.get("bvid", "")
        line = f"{idx}. [{type_tag}] {_truncate(content)}"
        if bvid:
            line += f"\n   bilibili.com/video/{bvid}"
    elif dtype == "article":
        title = item.get("title", "")
        text = item.get("text", "")
        content = title
        if text and text != title:
            content = f"{title} | {text}"
        line = f"{idx}. [{type_tag}] {_truncate(content)}"
    elif dtype == "image":
        text = item.get("text", "")
        pic_count = len(item.get("pictures", []))
        content = text or "(无文字)"
        line = f"{idx}. [{type_tag}] {_truncate(content)} (图×{pic_count})"
    else:
        text = item.get("text", "")
        line = f"{idx}. [{type_tag}] {_truncate(text or '(无内容)')}"

    return f"[{time_str}] {line}" if time_str else line


async def send_dynamics_list(bot, target_id: str, is_group: bool,
                             items: list[dict], title: str = ""):
    """只发送最新一条动态，并附带一张相关图片。"""
    detail_type = "group" if is_group else "private"
    kwargs = {
        "detail_type": detail_type,
        "user_id": target_id if not is_group else None,
        "group_id": target_id if is_group else None,
    }

    if not items:
        await bot.send_message(**kwargs, message=f"{title} 暂无动态")
        return

    latest = max(items, key=lambda x: int(x.get("timestamp", 0) or 0))
    await bot.send_message(
        **kwargs,
        message=format_latest_dynamic(latest, title=title),
    )
    image_path = await download_image(dynamic_image_url(latest))
    if image_path:
        try:
            await bot.send_message(
                **kwargs,
                message=MessageSegment("image", {"file_id": image_path}),
            )
        finally:
            Path(image_path).unlink(missing_ok=True)


async def send_dynamic(bot, target_id: str, is_group: bool, item: dict):
    """推送单条新动态（用于定时轮询推送）"""
    detail_type = "group" if is_group else "private"
    kwargs = {
        "detail_type": detail_type,
        "user_id": target_id if not is_group else None,
        "group_id": target_id if is_group else None,
    }
    author = item.get("author", "")
    line = _format_item(1, item).lstrip("1. ")
    header = f"📢 {author} 发布了新动态" if author else "📢 新动态"
    msg = f"{header}\n\n{line}"

    await bot.send_message(**kwargs, message=msg)

    # 每条新动态只附带一张最相关图片；无内容图时使用作者头像兜底。
    image_path = await download_image(dynamic_image_url(item))
    if image_path:
        try:
            await bot.send_message(
                **kwargs,
                message=MessageSegment("image", {"file_id": image_path}),
            )
        except Exception as e:
            logger.warning(f"[BILI] 发送图片失败: {e}")
        finally:
            Path(image_path).unlink(missing_ok=True)
