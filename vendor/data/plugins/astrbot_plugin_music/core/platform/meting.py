import json
from typing import Any, ClassVar
from urllib.parse import quote

import aiohttp

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class MetingMusic(BaseMusicPlayer):
    """Local Meting-Fixed adapter with ordered multi-platform fallback."""

    platform: ClassVar[Platform] = Platform(
        name="meting",
        display_name="Meting聚合",
        keywords=["Meting聚合", "聚合点歌", "Meting点歌", "meting点歌"],
    )

    SOURCE_LABELS: ClassVar[dict[str, str]] = {
        "netease": "网易云音乐",
        "tencent": "QQ音乐",
        "kugou": "酷狗音乐",
        "kuwo": "酷我音乐",
    }
    DEFAULT_ORDER: ClassVar[tuple[str, ...]] = (
        "netease",
        "tencent",
        "kugou",
        "kuwo",
    )

    def __init__(self, config: PluginConfig):
        super().__init__(config)
        self.base_url = (
            getattr(config, "meting_base_url", None) or "http://127.0.0.1:3301"
        ).rstrip("/")
        self.timeout = max(3, int(getattr(config, "meting_timeout", 20) or 20))
        self.search_platform_order = self._normalize_order(
            getattr(config, "search_platform_order", None)
        )

    @classmethod
    def _normalize_order(cls, value: Any) -> list[str]:
        if isinstance(value, str):
            raw_values = value.split(",")
        elif isinstance(value, (list, tuple)):
            raw_values = list(value)
        else:
            raw_values = []

        aliases = {
            "网易云音乐": "netease",
            "qq音乐": "tencent",
            "酷狗音乐": "kugou",
            "酷我音乐": "kuwo",
        }
        values: list[str] = []
        for item in raw_values:
            normalized = str(item).strip().lower()
            normalized = normalized.split("(", 1)[0].split("（", 1)[0].strip()
            values.append(aliases.get(normalized, normalized))

        result: list[str] = []
        for item in values + list(cls.DEFAULT_ORDER):
            if item in cls.SOURCE_LABELS and item not in result:
                result.append(item)
        return result

    async def _request_json(self, path: str, params: dict[str, Any]) -> dict | None:
        url = f"{self.base_url}/{path.lstrip('/')}"
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            async with self.session.get(url, params=params, timeout=timeout) as resp:
                if resp.status != 200:
                    logger.warning(
                        "Meting API %s 返回 HTTP %s: %s",
                        path,
                        resp.status,
                        (await resp.text())[:200],
                    )
                    return None
                data = await resp.json(content_type=None)
                return data if isinstance(data, dict) else None
        except Exception as e:
            logger.warning("Meting API %s 请求失败: %s", path, e)
            return None

    @staticmethod
    def _note_data(song: Song) -> dict[str, Any]:
        if not song.note:
            return {}
        try:
            value = json.loads(song.note)
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    @classmethod
    def _song_from_data(cls, item: dict[str, Any], source: str) -> Song | None:
        song_id = str(item.get("id") or item.get("url_id") or "").strip()
        if not song_id:
            return None
        artists = item.get("artists") or item.get("artist") or []
        if isinstance(artists, str):
            artist_text = artists
        else:
            artist_text = "、".join(str(name) for name in artists if name)
        note = {
            "meting_source": source,
            "url_id": str(item.get("url_id") or song_id),
            "lyric_id": str(item.get("lyric_id") or song_id),
            "pic_id": str(item.get("pic_id") or ""),
        }
        return Song(
            id=song_id,
            name=str(item.get("name") or "") or None,
            artists=artist_text or None,
            title=str(item.get("name") or "") or None,
            author=artist_text or None,
            note=json.dumps(note, ensure_ascii=False),
        )

    async def _search_source(
        self, source: str, keyword: str, limit: int
    ) -> list[Song]:
        result = await self._request_json(
            "/search",
            {
                "source": source,
                "keywords": keyword,
                "limit": max(1, min(limit, 20)),
                "page": 1,
            },
        )
        rows = result.get("data") if isinstance(result, dict) else None
        if not isinstance(rows, list):
            return []
        songs: list[Song] = []
        for item in rows:
            if isinstance(item, dict):
                song = self._song_from_data(item, source)
                if song:
                    songs.append(song)
        return songs

    async def fetch_songs(
        self, keyword: str, limit: int = 5, extra: str | None = None
    ) -> list[Song]:
        del extra
        # In single mode, a source only counts when its first playable result can
        # be resolved. This makes the configured order a true playable fallback.
        single_mode = "single" in (getattr(self.cfg, "select_mode", "") or "")
        for source in self.search_platform_order:
            songs = await self._search_source(source, keyword, limit)
            if not songs:
                continue
            if not single_mode:
                return songs
            for song in songs:
                await self.fetch_extra(song)
                if self._is_playable(song):
                    return [song]
            logger.info("Meting 平台 %s 搜索到歌曲但没有可播放音源，继续回退", source)
        return []

    @classmethod
    def _is_playable(cls, song: Song) -> bool:
        if not song.audio_url:
            return False
        note = cls._note_data(song)
        source = str(note.get("meting_source") or "").lower()
        # Meting's NetEase endpoint can expose a short 128 kbps preview for
        # subscription tracks. Reject obviously tiny sources before sending.
        if source == "netease":
            try:
                size = int(note.get("size") or 0)
                br = float(note.get("br") or 0)
            except (TypeError, ValueError):
                size, br = 0, 0
            if size and size < 1_500_000 and br <= 192:
                return False
        return True

    async def fetch_extra(self, song: Song) -> Song:
        note = self._note_data(song)
        source = str(note.get("meting_source") or "").lower()
        if source not in self.SOURCE_LABELS:
            source = self.search_platform_order[0]

        song_id = str(note.get("url_id") or song.id or "").strip()
        if not song_id:
            return song

        result = await self._request_json(
            "/url",
            {"source": source, "id": song_id, "br": 320},
        )
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and data.get("url"):
            song.audio_url = str(data["url"])
            note["br"] = data.get("br")
            note["size"] = data.get("size")

        if not song.cover_url:
            pic = await self._request_json(
                "/pic", {"source": source, "id": song_id, "size": 500}
            )
            pic_data = pic.get("data") if isinstance(pic, dict) else None
            if isinstance(pic_data, dict) and pic_data.get("url"):
                song.cover_url = str(pic_data["url"])

        song.note = json.dumps(
            {**note, "meting_source": source}, ensure_ascii=False
        )
        return song

    async def fetch_lyrics(self, song: Song) -> Song:
        note = self._note_data(song)
        source = str(note.get("meting_source") or self.search_platform_order[0])
        lyric_id = str(note.get("lyric_id") or song.id or "").strip()
        if not lyric_id:
            return song
        result = await self._request_json(
            "/lyric", {"source": source, "id": lyric_id}
        )
        data = result.get("data") if isinstance(result, dict) else None
        if isinstance(data, dict) and data.get("lyric"):
            song.lyrics = str(data["lyric"])
        return song

    @classmethod
    def source_name(cls, song: Song) -> str:
        note = cls._note_data(song)
        source = str(note.get("meting_source") or "").lower()
        return cls.SOURCE_LABELS.get(source, "Meting聚合")

    @classmethod
    def play_url(cls, song: Song) -> str:
        note = cls._note_data(song)
        source = str(note.get("meting_source") or "").lower()
        song_id = quote(str(note.get("url_id") or song.id or ""), safe="")
        if source == "netease":
            return f"https://music.163.com/song?id={song_id}"
        if source == "tencent":
            return f"https://i.y.qq.com/v8/playsong.html?songmid={song_id}"
        if source == "kugou":
            return f"https://www.kugou.com/song/#hash={song_id}"
        if source == "kuwo":
            return f"https://kuwo.cn/play_detail/{song_id}"
        return "https://github.com/ELDment/Meting-Fixed"
