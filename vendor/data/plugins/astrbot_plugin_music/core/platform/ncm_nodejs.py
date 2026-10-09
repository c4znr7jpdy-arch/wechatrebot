from typing import ClassVar
from urllib.parse import quote

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class NetEaseMusicNodeJS(BaseMusicPlayer):
    """NetEase Cloud Music NodeJS API."""

    platform: ClassVar[Platform] = Platform(
        name="netease_nodejs",
        display_name="网易云NodeJS版",
        keywords=["nj点歌", "网易nj"],
    )

    QUALITY_FALLBACKS = ("exhigh", "higher", "standard")
    MIN_REASONABLE_DURATION_MS = 90_000
    MIN_REASONABLE_SIZE_BYTES = 1_500_000

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    @staticmethod
    def _extract_cover_url(song_data: dict) -> str | None:
        album = song_data.get("al") or song_data.get("album") or {}
        return (
            album.get("picUrl")
            or album.get("blurPicUrl")
            or song_data.get("picUrl")
            or song_data.get("pic")
            or None
        )

    @staticmethod
    def _extract_artists(song_data: dict) -> str | None:
        artists = song_data.get("ar") or song_data.get("artists") or []
        names = [
            a.get("name", "")
            for a in artists
            if isinstance(a, dict) and a.get("name")
        ]
        return "、".join(names) if names else None

    @classmethod
    def _reject_reason(cls, info: dict, song: Song) -> str | None:
        if not info.get("url"):
            return "empty url"

        trial_info = info.get("freeTrialInfo")
        if isinstance(trial_info, dict) and trial_info.get("end"):
            return f"trial fragment end={trial_info.get('end')}"

        trial_privilege = info.get("freeTrialPrivilege")
        if isinstance(trial_privilege, dict):
            if (
                trial_privilege.get("resConsumable") is True
                and trial_privilege.get("userConsumable") is False
            ):
                return "trial privilege only"

        actual_ms = int(info.get("time") or 0)
        expected_ms = int(song.duration or 0)
        if actual_ms and expected_ms:
            min_expected = max(cls.MIN_REASONABLE_DURATION_MS, int(expected_ms * 0.8))
            if actual_ms < min_expected:
                return f"too short actual={actual_ms} expected={expected_ms}"

        size = int(info.get("size") or 0)
        if (
            not expected_ms
            and actual_ms
            and actual_ms < cls.MIN_REASONABLE_DURATION_MS
            and size
            and size < cls.MIN_REASONABLE_SIZE_BYTES
        ):
            return f"too small actual={actual_ms} size={size}"

        return None

    async def fetch_songs(self, keyword: str, limit: int = 5, extra=None) -> list[Song]:
        result = await self._request(
            url=(
                f"{self.cfg.nodejs_base_url}/search"
                f"?keywords={quote(keyword)}&limit={limit}&type=1&offset=0"
            ),
            method="GET",
        )
        if (
            not isinstance(result, dict)
            or "result" not in result
            or "songs" not in result["result"]
        ):
            logger.error(f"NetEase NodeJS search returned unexpected data: {result}")
            return []

        songs = result.get("result", {}).get("songs", [])[:limit]
        return [
            Song(
                id=s.get("id"),
                name=s.get("name"),
                artists="、".join(a["name"] for a in s["artists"]),
                duration=s.get("duration"),
                cover_url=self._extract_cover_url(s),
            )
            for s in songs
        ]

    async def fetch_comments(self, song: Song) -> Song:
        if song.comments:
            return song
        result = await self._request(
            url=f"{self.cfg.nodejs_base_url}/comment/hot",
            method="POST",
            data={"id": song.id, "type": 0},
        )
        if not isinstance(result, dict) or "hotComments" not in result:
            logger.error(f"NetEase NodeJS comments returned unexpected data: {result}")
            return song
        if comments := result.get("hotComments"):
            song.comments = comments
        return song

    async def fetch_lyrics(self, song: Song) -> Song:
        if song.lyrics:
            return song
        result = await self._request(f"{self.cfg.nodejs_base_url}/lyric?id={song.id}")
        if not isinstance(result, dict) or "lrc" not in result:
            logger.error(f"NetEase NodeJS lyric returned unexpected data: {result}")
            return song
        lyric = result["lrc"].get("lyric")
        if lyric:
            song.lyrics = lyric
        return song

    async def _fill_song_detail(self, song: Song) -> None:
        try:
            detail = await self._request(
                url=f"{self.cfg.nodejs_base_url}/song/detail?ids={song.id}",
                method="GET",
            )
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} song/detail failed: {e}")
            return

        details = detail.get("songs") if isinstance(detail, dict) else None
        if not details:
            return

        info = details[0]
        if not song.name:
            song.name = info.get("name")
        if not song.artists:
            song.artists = self._extract_artists(info)
        if not song.duration:
            song.duration = info.get("dt") or info.get("duration")
        if not song.cover_url:
            song.cover_url = self._extract_cover_url(info)

    async def _fetch_url_v1(self, song: Song) -> dict | None:
        for quality in self.QUALITY_FALLBACKS:
            try:
                result = await self._request(
                    url=(
                        f"{self.cfg.nodejs_base_url}/song/url/v1"
                        f"?id={song.id}&level={quality}"
                    ),
                    method="GET",
                )
            except Exception as e:
                logger.warning(
                    f"{self.__class__.__name__} song/url/v1 {quality} failed: {e}"
                )
                continue

            data = result.get("data") if isinstance(result, dict) else None
            if not data:
                continue

            info = data[0]
            reason = self._reject_reason(info, song)
            if reason:
                logger.warning(
                    f"NetEase NodeJS rejected source: id={song.id}, "
                    f"quality={quality}, reason={reason}"
                )
                continue

            info["_quality"] = quality
            return info
        return None

    async def _fetch_legacy_url(self, song: Song) -> dict | None:
        try:
            result = await self._request(
                url=f"{self.cfg.nodejs_base_url}/song/url?id={song.id}",
                method="GET",
            )
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} song/url failed: {e}")
            return None

        data = result.get("data") if isinstance(result, dict) else None
        if not data:
            return None

        info = data[0]
        reason = self._reject_reason(info, song)
        if reason:
            logger.warning(
                f"NetEase NodeJS rejected legacy source: id={song.id}, reason={reason}"
            )
            return None

        info["_quality"] = info.get("level") or "legacy"
        return info

    async def fetch_extra(self, song: Song) -> Song:
        if not str(song.id or "").strip():
            return song

        await self._fill_song_detail(song)

        info = await self._fetch_url_v1(song)
        if not info:
            info = await self._fetch_legacy_url(song)
        if not info:
            song.audio_url = None
            return song

        song.audio_url = info.get("url")
        song.note = (
            f"netease_nodejs level={info.get('_quality')}, "
            f"time={info.get('time')}, size={info.get('size')}"
        )
        return song
