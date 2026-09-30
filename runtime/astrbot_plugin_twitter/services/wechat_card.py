"""沿用 E:/Project 的微信 Share 卡片格式与页面桥接。"""

import unicodedata
from datetime import datetime, timedelta, timezone

import astrbot.api.message_components as Comp

from .tweet_page_server import TweetPageBridge


def compact_summary(text: str, max_width: int = 24) -> str:
    summary = " ".join(str(text or "").split()).strip()
    if not summary:
        return "点击查看完整推文"
    def width(char):
        return 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    if sum(map(width, summary)) <= max_width:
        return summary
    result, used = [], 0
    for char in summary:
        if used + width(char) > max_width - 2:
            break
        result.append(char)
        used += width(char)
    return f"{''.join(result).rstrip()}…"


def format_tweet_time(tweet: dict) -> str:
    published = str(tweet.get("date") or "").strip()
    china_tz = timezone(timedelta(hours=8))
    parsed = None
    if published:
        try:
            parsed = datetime.fromisoformat(published.replace("Z", "+00:00"))
        except ValueError:
            normalized = " ".join(published.replace("·", " ").split())
            is_utc = normalized.upper().endswith((" UTC", " GMT"))
            if is_utc:
                normalized = normalized[:-4].strip()
            for pattern in ("%b %d, %Y %I:%M %p", "%b %d, %Y %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
                try:
                    parsed = datetime.strptime(normalized, pattern).replace(
                        tzinfo=timezone.utc if is_utc else china_tz
                    )
                    break
                except ValueError:
                    continue
    if parsed is None:
        tweet_id = str(tweet.get("tweet_id") or "")
        if tweet_id.isdigit():
            try:
                milliseconds = (int(tweet_id) >> 22) + 1288834974657
                parsed = datetime.fromtimestamp(milliseconds / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                pass
    if parsed is None:
        return published or "时间未知"
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=china_tz)
    return parsed.astimezone(china_tz).strftime("%Y/%m/%d %H:%M")


async def build_card(username: str, tweet: dict, translated_text: str | None = None, *, avatar_cache=None):
    bridge = TweetPageBridge()
    snapshot = dict(tweet, username=tweet.get("username") or username,
                    display_date=format_tweet_time(tweet))
    if translated_text:
        snapshot["translated_text"] = translated_text
    url, avatar = await bridge.create_urls(snapshot, avatar_cache)
    author = str(tweet.get("username") or username).lstrip("@")
    summary = compact_summary(translated_text or tweet.get("text") or "")
    return Comp.Share(
        url=url,
        title=f"@{author}发了新推文",
        content=f"📝 {summary}\n🕒 {format_tweet_time(tweet)}",
        image=avatar,
    )
