import json
from typing import ClassVar
from urllib.parse import quote

from astrbot.api import logger

from ..config import PluginConfig
from ..model import Platform, Song
from .base import BaseMusicPlayer


class NetEaseMusic(BaseMusicPlayer):
    """
    网易云音乐（Web API）
    """

    platform: ClassVar[Platform] = Platform(
        name="netease",
        display_name="网易云音乐",
        keywords=["网易云", "网易点歌"],
    )

    def __init__(self, config: PluginConfig):
        super().__init__(config)

    MIN_REASONABLE_DURATION_MS = 90_000
    MIN_REASONABLE_SIZE_BYTES = 1_500_000

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

    async def fetch_songs(self, keyword: str, limit=5, extra=None) -> list[Song]:
        result = await self._request(
            url="http://music.163.com/api/search/get/web",
            method="POST",
            data={"s": keyword, "limit": limit, "type": 1, "offset": 0},
            cookies={"appver": "2.0.2"},
        )
        if (
            not isinstance(result, dict)
            or "result" not in result
            or "songs" not in result["result"]
        ):
            logger.error(f"返回了意料之外数据：{result}")
            return []

        songs = result["result"]["songs"][:limit]

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

    async def fetch_extra(self, song: Song) -> Song:
        song_id = str(song.id or "").strip()
        if not song_id:
            return song

        try:
            detail_query = quote(json.dumps([{"id": int(song_id)}], separators=(",", ":")))
            detail = await self._request(
                url=(
                    "https://music.163.com/api/v3/song/detail"
                    f"?id={song_id}&c={detail_query}"
                ),
                method="GET",
            )
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} song/detail 失败: {e}")
            detail = None

        songs = detail.get("songs") if isinstance(detail, dict) else None
        if songs:
            info = songs[0]
            if not song.name:
                song.name = info.get("name")
            if not song.artists:
                artists = info.get("ar") or info.get("artists") or []
                song.artists = "、".join(a.get("name", "") for a in artists if a.get("name"))
            if not song.duration:
                song.duration = info.get("dt") or info.get("duration")
            if not song.cover_url:
                song.cover_url = self._extract_cover_url(info)

        try:
            play_url_result = await self._request(
                url=(
                    "https://music.163.com/api/song/enhance/player/url"
                    f"?ids={quote(f'[{song_id}]')}&br=320000"
                ),
                method="GET",
            )
        except Exception as e:
            logger.warning(f"{self.__class__.__name__} player/url 失败: {e}")
            play_url_result = None

        play_data = play_url_result.get("data") if isinstance(play_url_result, dict) else None
        if play_data:
            info = play_data[0]
            audio_url = info.get("url")
            if audio_url:
                reason = self._reject_reason(info, song)
                if reason:
                    logger.warning(
                        f"NetEase rejected trial source: id={song_id}, reason={reason}"
                    )
                    song.audio_url = None
                else:
                    song.audio_url = audio_url
                    song.note = (
                        f"netease br={info.get('br')}, "
                        f"time={info.get('time')}, size={info.get('size')}"
                    )

        return song
