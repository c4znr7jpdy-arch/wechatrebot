"""
抖音/TikTok/B站 视频解析插件
当用户在聊天中发送分享链接时，自动解析并返回视频信息+下载链接
"""
import re
import sys
from pathlib import Path

# 让 crawlers 包内的 from crawlers.xxx import ... 能正常工作
_douyin_dir = Path(__file__).parent
if str(_douyin_dir) not in sys.path:
    sys.path.insert(0, str(_douyin_dir))

from nonebot import logger
from .config import inject_cookies

# 延迟导入 — 首次调用时才加载爬虫（确保 Cookie 已注入）
_crawler = None
_crawler_ready = False

# URL 匹配模式
# 转发事件中的换行可能是字面量 ``\n``，所以 URL 不能使用会吞掉反斜杠的 ``\S+``。
# B 站长链接只接受爬虫实际支持的 BV 视频页；/read/cv... 专栏留给普通 AI 分析。
_DOUYIN_RE = re.compile(
    r"""https?://(?:v\.douyin\.com|www\.douyin\.com|douyin\.com)/[^\s\\<>"']+""",
    re.IGNORECASE,
)
_TIKTOK_RE = re.compile(
    r"""https?://(?:www\.tiktok\.com|tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com)/[^\s\\<>"']+""",
    re.IGNORECASE,
)
_BILIBILI_RE = re.compile(
    r"""https?://(?:(?:www\.|m\.)?bilibili\.com/(?:video/)?BV[A-Za-z0-9]+"""
    r"""|(?:b23\.tv|b22\.tv)/[^\s\\<>"']+)[^\s\\<>"']*""",
    re.IGNORECASE,
)
VIDEO_URL_PATTERN = (
    rf"(?i:{_DOUYIN_RE.pattern}|{_TIKTOK_RE.pattern}|{_BILIBILI_RE.pattern})"
)
# B站用户空间: space.bilibili.com/UID 或 bilibili.com/space/UID（协议可选）
_BILIBILI_SPACE_RE = re.compile(
    r"(?:https?://)?(?:space\.bilibili\.com|(?:www\.)?bilibili\.com/space)/(\d+)",
    re.IGNORECASE,
)
# 抖音分享口令格式: "7.43 pda:/ ... https://v.douyin.com/xxx ..."
_SHARE_DOUYIN_RE = re.compile(r"https?://v\.douyin\.com/\S+", re.IGNORECASE)


def extract_url(text: str) -> str | None:
    """从消息文本中提取抖音/TikTok/B站 URL"""
    for pat in [_DOUYIN_RE, _TIKTOK_RE, _BILIBILI_RE, _SHARE_DOUYIN_RE]:
        m = pat.search(text)
        if m:
            url = m.group(0)
            url = url.rstrip(".,，。!！?？;；）)")
            return url
    return None


def extract_bilibili_uid(text: str) -> str | None:
    """从消息中提取 B站用户空间 UID"""
    m = _BILIBILI_SPACE_RE.search(text)
    return m.group(1) if m else None


def _ensure_crawler():
    global _crawler, _crawler_ready
    if not _crawler_ready:
        inject_cookies()
        from crawlers.hybrid.hybrid_crawler import HybridCrawler

        _crawler = HybridCrawler()
        _crawler_ready = True
        logger.info("[DOUYIN] HybridCrawler 已初始化")
    return _crawler


async def parse_url(url: str) -> dict:
    """解析抖音/TikTok/B站 URL，返回格式化数据"""
    crawler = _ensure_crawler()
    result = await crawler.hybrid_parsing_single_video(url, minimal=True)
    return result


async def fetch_user_dynamics(uid: str, count: int = 5) -> list[dict]:
    """获取 B站用户最近动态（图文+视频）- 使用新版API"""
    import os as _os
    import httpx

    cookie = _os.getenv("BILIBILI_COOKIE", "")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Referer": f"https://space.bilibili.com/{uid}/dynamic",
        "Origin": "https://space.bilibili.com",
        "Cookie": cookie,
    }
    url = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
    params = {"host_mid": uid, "offset": "", "timezone_offset": "-480"}

    async with httpx.AsyncClient(headers=headers, timeout=30.0) as client:
        resp = await client.get(url, params=params)
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") != 0:
            raise RuntimeError(f"B站API错误: {data.get('message', 'unknown')}")

    items = data.get("data", {}).get("items", [])
    result = []
    for item in items:
        modules = item.get("modules", {})
        author = modules.get("module_author", {})
        dynamic = modules.get("module_dynamic", {})
        major = dynamic.get("major", {})
        desc = dynamic.get("desc") or {}
        dtype = item.get("type", "")

        entry = {
            "dynamic_id": item.get("id_str", ""),
            "timestamp": author.get("pub_ts", 0),
        }

        if dtype == "DYNAMIC_TYPE_AV":  # 视频
            archive = major.get("archive", {})
            entry["type"] = "video"
            entry["title"] = archive.get("title", "")
            entry["bvid"] = archive.get("bvid", "")
            entry["play"] = archive.get("stat", {}).get("play", 0)
            entry["duration"] = archive.get("duration_text", "")
            entry["description"] = archive.get("desc", "")
        elif dtype == "DYNAMIC_TYPE_DRAW":  # 图文
            draw = major.get("draw", {})
            pics = draw.get("items", [])
            entry["type"] = "image"
            entry["description"] = desc.get("text", "")
            entry["pictures"] = [p.get("src", "") for p in pics]
        elif dtype == "DYNAMIC_TYPE_WORD":  # 纯文字
            entry["type"] = "text"
            entry["content"] = desc.get("text", "")
        elif dtype == "DYNAMIC_TYPE_ARTICLE":  # 专栏
            article = major.get("article", {})
            entry["type"] = "text"
            entry["content"] = f"[专栏] {article.get('title', '')}"
        else:
            entry["type"] = "unknown"
        if entry.get("type") != "unknown":
            result.append(entry)
        if len(result) >= count:
            break
    return result


def format_result(data: dict) -> str:
    """将解析结果格式化为微信消息文本"""
    platform_names = {"douyin": "抖音", "tiktok": "TikTok", "bilibili": "B站"}
    type_names = {"video": "视频", "image": "图集"}

    platform = data.get("platform", "unknown")
    media_type = data.get("type", "video")
    desc = data.get("desc", "") or ""
    # 截断过长描述
    if len(desc) > 120:
        desc = desc[:120] + "..."

    # 作者
    author = data.get("author", {})
    if platform == "bilibili":
        author_name = author.get("name", "未知")
    else:
        author_name = author.get("nickname", "未知")

    # 统计
    stats = data.get("statistics", {})
    stat_lines = _format_stats(platform, stats)

    # 下载链接
    video_data = data.get("video_data", {})
    image_data = data.get("image_data", {})

    lines = [
        f"🎬 {platform_names.get(platform, platform)} {type_names.get(media_type, media_type)}",
        f"作者: {author_name}",
    ]
    if desc:
        lines.append(f"简介: {desc}")
    lines.extend(stat_lines)

    if media_type == "video" and video_data:
        nwm = video_data.get("nwm_video_url_HQ") or video_data.get("nwm_video_url") or ""
        if nwm:
            lines.append(f"无水印: {nwm}")
        wm = video_data.get("wm_video_url") or ""
        if wm and wm != nwm:
            lines.append(f"有水印: {wm}")
    elif media_type == "image" and image_data:
        nwm_list = image_data.get("no_watermark_image_list", [])
        lines.append(f"无水印图片 ×{len(nwm_list)} 张")
        for i, img_url in enumerate(nwm_list[:4], 1):
            lines.append(f"  [{i}] {img_url}")
        if len(nwm_list) > 4:
            lines.append(f"  ... 共 {len(nwm_list)} 张")

    return "\n".join(lines)


def _format_stats(platform: str, stats: dict) -> list[str]:
    """格式化统计数据"""
    if not stats:
        return []
    parts = []
    if platform == "bilibili":
        view = stats.get("view", 0)
        danmaku = stats.get("danmaku", 0)
        like = stats.get("like", 0)
        coin = stats.get("coin", 0)
        favorite = stats.get("favorite", 0)
        share = stats.get("share", 0)
        if view:
            parts.append(f"播放 {_fmt_num(view)}")
        if danmaku:
            parts.append(f"弹幕 {_fmt_num(danmaku)}")
        if like:
            parts.append(f"点赞 {_fmt_num(like)}")
        if coin:
            parts.append(f"硬币 {_fmt_num(coin)}")
        if favorite:
            parts.append(f"收藏 {_fmt_num(favorite)}")
        if share:
            parts.append(f"分享 {_fmt_num(share)}")
    elif platform == "tiktok":
        play = stats.get("play_count", 0)
        digg = stats.get("digg_count", 0)
        comment = stats.get("comment_count", 0)
        share = stats.get("share_count", 0)
        if play:
            parts.append(f"播放 {_fmt_num(play)}")
        if digg:
            parts.append(f"点赞 {_fmt_num(digg)}")
        if comment:
            parts.append(f"评论 {_fmt_num(comment)}")
        if share:
            parts.append(f"分享 {_fmt_num(share)}")
    else:  # douyin
        digg = stats.get("digg_count", 0)
        comment = stats.get("comment_count", 0)
        share = stats.get("share_count", 0)
        if digg:
            parts.append(f"点赞 {_fmt_num(digg)}")
        if comment:
            parts.append(f"评论 {_fmt_num(comment)}")
        if share:
            parts.append(f"分享 {_fmt_num(share)}")
    return [" · ".join(parts)] if parts else []


def _fmt_num(n: int) -> str:
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


def format_user_dynamics(items: list[dict], author: str = "") -> str:
    """格式化 B站用户动态列表（图文+视频）"""
    if not items:
        return "该用户暂无动态"

    label = f"B站 · {author} " if author else "B站动态 "
    lines = [f"{label}最近动态 (共{len(items)}条)"]
    for i, it in enumerate(items, 1):
        if it.get("type") == "image":
            desc = it.get("description", "")
            pics = it.get("pictures", [])
            lines.append(f"{i}. [图文] {desc}")
            lines.append(f"   图片×{len(pics)}")
        elif it.get("type") == "video":
            title = it.get("title", "")
            bvid = it.get("bvid", "")
            play = _fmt_num(it.get("play", 0))
            duration = it.get("duration", "?")
            lines.append(f"{i}. [视频] {title}")
            lines.append(f"   BV: {bvid} | 播放: {play} | 时长: {duration}")
        elif it.get("type") == "text":
            content = it.get("content", "")
            lines.append(f"{i}. [文字] {content}")
    return "\n".join(lines)
