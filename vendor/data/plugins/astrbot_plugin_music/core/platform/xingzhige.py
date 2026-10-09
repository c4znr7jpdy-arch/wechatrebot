import json
import re
from typing import Any, ClassVar
from urllib.parse import quote

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class XingzhigeKuwoMusic(BaseMusicPlayer):
    """Xingzhige Kuwo/Bodian music search with playable audio URLs."""

    platform: ClassVar[Platform] = Platform(
        name="xingzhige_kuwo",
        display_name="星之阁波点音乐",
        keywords=["星之阁点歌", "波点点歌", "xzg点歌"],
    )

    API_URL = "https://api.xingzhige.com/API/Kuwo_BD_new/"

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        result = await self._request(
            url=f"{self.API_URL}?name={quote(keyword)}",
            method="GET",
            ssl=False,
        )
        if not isinstance(result, dict) or result.get("code") != 0:
            logger.warning(f"星之阁波点音乐搜索失败: {result!r}")
            return []

        rows = result.get("data")
        if isinstance(rows, dict):
            rows = [rows]
        if not isinstance(rows, list):
            logger.warning(f"星之阁波点音乐返回异常: {result!r}")
            return []

        songs: list[Song] = []
        for index, item in enumerate(rows[:limit], start=1):
            if not isinstance(item, dict):
                continue
            song_id = str(item.get("id") or index)
            songs.append(
                Song(
                    id=song_id,
                    name=item.get("songname"),
                    artists=item.get("name"),
                    title=item.get("songname"),
                    author=item.get("name"),
                    cover_url=item.get("cover"),
                    note=json.dumps(
                        {
                            "query": keyword,
                            "n": index,
                            "songurl": item.get("songurl"),
                        },
                        ensure_ascii=False,
                    ),
                )
            )
        return songs

    async def fetch_extra(self, song: Song) -> Song:
        query, index = self._extra_query_and_index(song)
        if not query:
            return song

        result = await self._request(
            url=f"{self.API_URL}?name={quote(query)}&n={index}",
            method="GET",
            ssl=False,
        )
        if not isinstance(result, dict) or result.get("code") != 0:
            logger.warning(f"星之阁波点音乐详情失败: {result!r}")
            return song

        data = result.get("data")
        if not isinstance(data, dict):
            logger.warning(f"星之阁波点音乐详情异常: {result!r}")
            return song

        raw = data.get("raw") if isinstance(data.get("raw"), dict) else {}
        audio_url = raw.get("audioHttpsUrl") or data.get("src") or raw.get("audioUrl")
        if audio_url:
            song.audio_url = audio_url
        song.name = song.name or data.get("songname")
        song.artists = song.artists or data.get("name")
        song.title = song.title or data.get("songname")
        song.author = song.author or data.get("name")
        song.cover_url = song.cover_url or data.get("cover")
        song.duration = song.duration or self._duration_ms(
            raw.get("duration") or data.get("interval")
        )

        details = [
            "星之阁波点音乐",
            str(data.get("quality") or "").strip(),
            str(raw.get("format") or "").strip(),
        ]
        song.note = " ".join(part for part in details if part)
        return song

    @classmethod
    def play_url(cls, song: Song) -> str:
        note = cls._note_dict(song.note)
        return note.get("songurl") or f"https://kuwo.cn/play_detail/{song.id}"

    @classmethod
    def _extra_query_and_index(cls, song: Song) -> tuple[str | None, int]:
        note = cls._note_dict(song.note)
        query = note.get("query") or song.name or song.title
        try:
            index = int(note.get("n") or 1)
        except (TypeError, ValueError):
            index = 1
        return query, max(index, 1)

    @staticmethod
    def _note_dict(note: str | None) -> dict[str, Any]:
        if not note:
            return {}
        try:
            data = json.loads(note)
        except json.JSONDecodeError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _duration_ms(value: Any) -> int | None:
        if value is None:
            return None
        if isinstance(value, (int, float)):
            duration = float(value)
            return int(duration * 1000 if duration < 10000 else duration)
        if isinstance(value, str):
            match = re.fullmatch(r"\s*(?:(\d+)分)?(\d+)秒\s*", value)
            if match:
                minutes = int(match.group(1) or 0)
                seconds = int(match.group(2))
                return (minutes * 60 + seconds) * 1000
            try:
                duration = float(value)
                return int(duration * 1000 if duration < 10000 else duration)
            except ValueError:
                return None
        return None
