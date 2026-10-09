import json
import re
from html import unescape
from typing import Any, ClassVar
from urllib.parse import parse_qs, quote, urlparse

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class QishuiMusic(BaseMusicPlayer):
    """Resolve Qishui/Soda Music share pages into playable audio URLs."""

    platform: ClassVar[Platform] = Platform(
        name="qishui",
        display_name="汽水音乐",
        keywords=["汽水点歌", "qs点歌", "qishui点歌"],
    )

    SHARE_HOSTS = {"www.qishui.com", "qishui.com"}
    ROUTER_MARKER = "_ROUTER_DATA"

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    @classmethod
    def looks_like_qishui_source(cls, text: str) -> bool:
        return cls._extract_track_id(text) is not None

    @classmethod
    def _extract_track_id(cls, text: str) -> str | None:
        raw = unescape((text or "").strip())
        if not raw:
            return None

        if raw.isdigit() and len(raw) >= 10:
            return raw

        try:
            parsed = urlparse(raw)
        except Exception:
            parsed = None

        if parsed and parsed.netloc:
            host = parsed.netloc.lower()
            query_track_id = parse_qs(parsed.query).get("track_id")
            if host in cls.SHARE_HOSTS and query_track_id:
                return query_track_id[0]

            match = re.search(r"/qishui/song/(\d+)", parsed.path)
            if match:
                return match.group(1)

        match = re.search(r"(?:track_id=|/qishui/song/)(\d{10,})", raw)
        if match:
            return match.group(1)
        return None

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        track_id = self._extract_track_id(keyword)
        if not track_id:
            logger.warning("汽水音乐目前需要 track_id 或分享链接，暂不支持纯歌名搜索")
            return []

        song = await self._fetch_track(track_id=track_id, source=keyword)
        return [song] if song else []

    async def fetch_extra(self, song: Song) -> Song:
        if song.audio_url and song.cover_url:
            return song

        track_id = str(song.id or "").strip()
        if not track_id:
            return song

        fresh = await self._fetch_track(track_id=track_id, source=song.note)
        if not fresh:
            return song

        for attr in ("name", "artists", "duration", "title", "author", "cover_url", "audio_url", "lyrics", "note"):
            value = getattr(fresh, attr)
            if value and not getattr(song, attr):
                setattr(song, attr, value)
        return song

    async def _fetch_track(self, track_id: str, source: str | None = None) -> Song | None:
        share_url = self._build_share_url(track_id, source)
        html = await self._request_text(share_url)
        if not html:
            return None

        router_data = self._extract_router_data(html)
        if not router_data:
            logger.warning(f"汽水音乐分享页未找到 ROUTER_DATA: {track_id}")
            return None

        option = self._find_audio_option(router_data)
        if not isinstance(option, dict):
            logger.warning(f"汽水音乐分享页未找到 audioWithLyricsOption: {track_id}")
            return None

        audio_url = option.get("url")
        if not audio_url:
            logger.warning(f"汽水音乐分享页没有可播放音频 URL: {track_id}")
            return None

        track_info = option.get("trackInfo") if isinstance(option.get("trackInfo"), dict) else {}
        title = (
            track_info.get("name")
            or option.get("name")
            or self._find_first(router_data, ("title", "trackName"))
            or f"汽水音乐 {track_id}"
        )
        artists = (
            option.get("artistName")
            or self._artists_from_track_info(track_info)
            or self._find_first(router_data, ("artistName", "authorName"))
            or ""
        )
        cover_url = (
            option.get("coverURL")
            or track_info.get("coverURL")
            or self._find_first(router_data, ("coverURL", "coverUrl", "imageUrl"))
        )
        duration = self._duration_ms(option.get("duration") or track_info.get("duration"))

        logger.info(
            f"汽水音乐解析成功: {title} - {artists}, duration={duration}, track_id={track_id}"
        )
        return Song(
            id=track_id,
            name=title,
            artists=artists,
            duration=duration,
            title=title,
            author=artists,
            cover_url=cover_url,
            audio_url=audio_url,
            note=share_url,
        )

    @classmethod
    def _build_share_url(cls, track_id: str, source: str | None = None) -> str:
        raw = unescape((source or "").strip())
        if raw.startswith(("http://", "https://")) and "qishui.com/share/track" in raw:
            return raw
        return (
            "https://www.qishui.com/share/track"
            f"?track_id={quote(track_id)}&share_platform=wechat&auto_play_bgm=1"
        )

    async def _request_text(self, url: str) -> str | None:
        headers = {
            **self.HEADERS,
            "User-Agent": (
                "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 "
                "MicroMessenger/8.0.49"
            ),
            "Referer": "https://www.qishui.com/",
        }
        try:
            async with self.session.get(url, headers=headers, ssl=False) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"汽水音乐分享页请求返回 {resp.status}: {text[:200]}")
                    return None
                return text
        except Exception as e:
            logger.warning(f"汽水音乐分享页请求失败: {e!r}")
            return None

    @classmethod
    def _extract_router_data(cls, html: str) -> dict[str, Any] | None:
        marker_index = html.find(cls.ROUTER_MARKER)
        if marker_index < 0:
            return None

        start = html.find("{", marker_index)
        if start < 0:
            return None

        try:
            data, _ = json.JSONDecoder().raw_decode(html[start:])
        except json.JSONDecodeError as e:
            logger.warning(f"汽水音乐 ROUTER_DATA 解析失败: {e}")
            return None

        return data if isinstance(data, dict) else None

    @classmethod
    def _find_audio_option(cls, data: Any) -> dict[str, Any] | None:
        if isinstance(data, dict):
            option = data.get("audioWithLyricsOption")
            if isinstance(option, dict):
                return option
            for value in data.values():
                found = cls._find_audio_option(value)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = cls._find_audio_option(item)
                if found:
                    return found
        return None

    @classmethod
    def _find_first(cls, data: Any, keys: tuple[str, ...]) -> str | None:
        if isinstance(data, dict):
            for key in keys:
                value = data.get(key)
                if isinstance(value, str) and value:
                    return value
            for value in data.values():
                found = cls._find_first(value, keys)
                if found:
                    return found
        elif isinstance(data, list):
            for item in data:
                found = cls._find_first(item, keys)
                if found:
                    return found
        return None

    @staticmethod
    def _artists_from_track_info(track_info: dict[str, Any]) -> str | None:
        artists = track_info.get("artists")
        if isinstance(artists, list):
            names = [a.get("name") for a in artists if isinstance(a, dict) and a.get("name")]
            if names:
                return "、".join(names)
        return None

    @staticmethod
    def _duration_ms(value: Any) -> int | None:
        if value is None:
            return None
        try:
            duration = float(value)
        except (TypeError, ValueError):
            return None
        if duration <= 0:
            return None
        return int(duration if duration > 10000 else duration * 1000)
