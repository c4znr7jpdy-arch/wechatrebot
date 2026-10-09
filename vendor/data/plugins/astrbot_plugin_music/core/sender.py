import asyncio
import json
import os
import random
import urllib.error
import urllib.request
from html import escape

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from astrbot.core.message.components import File, Image, Record
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)

from .config import PluginConfig
from .downloader import Downloader
from .model import Song
from .platform import (
    BaseMusicPlayer,
    MetingMusic,
    NetEaseMusic,
    NetEaseMusicNodeJS,
    QishuiMusic,
    XingzhigeKuwoMusic,
)
from .renderer import MusicRenderer


class MusicSender:
    def __init__(
        self, config: PluginConfig, renderer: MusicRenderer, downloader: Downloader
    ):
        self.cfg = config
        self.renderer = renderer
        self.downloader = downloader

    @staticmethod
    def _format_time(duration_ms):
        """格式化歌曲时长"""
        duration = duration_ms // 1000

        hours = duration // 3600
        minutes = (duration % 3600) // 60
        seconds = duration % 60

        if hours > 0:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            return f"{minutes:02d}:{seconds:02d}"

    @staticmethod
    async def send_msg(event: AiocqhttpMessageEvent, payloads: dict) -> int | None:
        if event.is_private_chat():
            payloads["user_id"] = event.get_sender_id()
            result = await event.bot.api.call_action("send_private_msg", **payloads)
        else:
            payloads["group_id"] = event.get_group_id()
            result = await event.bot.api.call_action("send_group_msg", **payloads)
        return result.get("message_id")

    async def send_song_selection(
        self, event: AstrMessageEvent, songs: list[Song], title: str | None = None
    ) -> None:
        """
        发送歌曲选择
        """
        formatted_songs = [
            f"{index + 1}. {song.name} - {song.artists}"
            for index, song in enumerate(songs)
        ]
        if title:
            formatted_songs.insert(0, title)

        msg = "\n".join(formatted_songs)
        if isinstance(event, AiocqhttpMessageEvent):
            payloads = {"message": [{"type": "text", "data": {"text": msg}}]}
            message_id = await self.send_msg(event, payloads)
            if message_id and self.cfg.timeout_recall:
                await asyncio.sleep(self.cfg.timeout)
                await event.bot.delete_msg(message_id=message_id)
        else:
            await event.send(event.plain_result(msg))

    async def send_comment(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发评论"""
        if not song.comments:
            await player.fetch_comments(song)
        if not song.comments:
            # 没有评论
            return False
        try:
            content = random.choice(song.comments).get("content")
            await event.send(event.plain_result(content))
            return True
        except Exception:
            return False

    async def send_lyrics(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发歌词"""
        if not song.lyrics:
            await player.fetch_lyrics(song)
        if song.lyrics:
            await player.resolve_lyrics(song)
        if not song.lyrics:
            logger.error(f"【{song.name}】歌词获取失败")
            return False
        try:
            image = self.renderer.draw_lyrics(song.lyrics)
            await event.send(MessageChain(chain=[Image.fromBytes(image)]))
            return True
        except Exception as e:
            logger.error(f"【{song.name}】歌词渲染/发送失败: {e}")
            return False

    async def send_card(
        self, event: AiocqhttpMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发卡片"""
        if isinstance(player, MetingMusic):
            return await self.send_meting_music_card(event, player, song)
        if isinstance(player, (NetEaseMusic, NetEaseMusicNodeJS)):
            return await self.send_netease_music_card(event, player, song)
        if isinstance(player, QishuiMusic):
            return await self.send_qishui_music_card(event, player, song)
        if isinstance(player, XingzhigeKuwoMusic):
            return await self.send_xingzhige_music_card(event, player, song)
        return False

    async def send_meting_music_card(
        self, event: AiocqhttpMessageEvent, player: MetingMusic, song: Song
    ) -> bool:
        """发送 Meting 聚合源解析后的微信可播放音乐卡片。"""
        try:
            song = await player.fetch_extra(song)
        except Exception as e:
            logger.warning(f"Meting 音乐详情补全失败: {e}")

        title = song.name or song.title or "Meting音乐"
        artists = song.artists or song.author or ""
        if not song.audio_url:
            logger.warning(f"Meting 音乐未返回可播放 URL，无法构造卡片: {title} - {artists}")
            return False

        source_name = player.source_name(song)
        appid = "wx8dd6ecd81906fd84"
        cover_url = song.cover_url or "https://s1.music.126.net/style/favicon.ico"
        xml = self._build_wechat_music_xml(
            appid=appid,
            appname=f"Meting-{source_name}",
            title=title,
            artists=artists,
            play_url=player.play_url(song),
            data_url=song.audio_url,
            cover_url=cover_url,
            lyrics=song.lyrics or "",
            duration=int(song.duration or 0),
            mid=f"meting_{source_name}_{song.id}",
            statextstr=self._build_statextstr(appid),
        )
        return await self._send_raw_music_card(event, xml, f"Meting-{source_name}")

    async def send_xingzhige_music_card(
        self, event: AiocqhttpMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """Send a playable WeChat music appmsg using Xingzhige's audio URL."""
        try:
            song = await player.fetch_extra(song)
        except Exception as e:
            logger.warning(f"星之阁波点音乐详情补全失败: {e!r}")

        title = song.name or song.title or "星之阁波点音乐"
        artists = song.artists or song.author or ""
        if not song.audio_url:
            logger.warning(f"星之阁波点音乐未返回可播放 URL，无法构造卡片: {title} - {artists}")
            return False

        appid = "wx8dd6ecd81906fd84"
        cover_url = song.cover_url or "https://www.xingzhige.com/logo.gif"
        play_url = XingzhigeKuwoMusic.play_url(song)
        xml = self._build_wechat_music_xml(
            appid=appid,
            appname="星之阁波点音乐",
            title=title,
            artists=artists,
            play_url=play_url,
            data_url=song.audio_url,
            cover_url=cover_url,
            lyrics=song.lyrics or "",
            duration=int(song.duration or 0),
            mid=f"xzg_kuwo_{song.id}",
            statextstr=self._build_statextstr(appid),
        )
        return await self._send_raw_music_card(event, xml, "星之阁波点音乐")

    async def send_qishui_music_card(
        self, event: AiocqhttpMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """Send a playable WeChat music appmsg with Qishui's resolved audio URL."""
        song_id = str(song.id or "").strip()
        if not song_id:
            return False

        try:
            song = await player.fetch_extra(song)
        except Exception as e:
            logger.warning(f"汽水音乐详情补全失败: {e}")

        title = song.name or song.title or "汽水音乐"
        artists = song.artists or song.author or ""
        if not song.audio_url:
            logger.warning(f"汽水音乐未解析到可播放 URL，无法构造卡片: {title} - {artists}")
            return False

        appid = "wx904fb3ecf62c7dea"
        cover_url = song.cover_url or "https://lf3-static.bytednsdoc.com/obj/eden-cn/pipieh7nupabozups/qishui/favicon.ico"
        play_url = song.note or f"https://www.qishui.com/share/track?track_id={song_id}&share_platform=wechat"
        xml = self._build_wechat_music_xml(
            appid=appid,
            appname="汽水音乐",
            title=title,
            artists=artists,
            play_url=play_url,
            data_url=song.audio_url,
            cover_url=cover_url,
            lyrics=song.lyrics or "",
            duration=int(song.duration or 0),
            mid=f"qishui_{song_id}",
            statextstr=self._build_statextstr(appid),
        )
        return await self._send_raw_music_card(event, xml, "汽水音乐")

    async def send_netease_music_card(
        self, event: AiocqhttpMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发送微信可播放的网易云音乐 appmsg 卡片。"""
        song_id = str(song.id or "").strip()
        if not song_id:
            return False

        play_url = f"https://music.163.com/song?id={song_id}"
        try:
            song = await player.fetch_extra(song)
        except Exception as e:
            logger.warning(f"网易云音乐详情补全失败: {e}")

        title = song.name or song.title or "网易云音乐"
        artists = song.artists or song.author or ""
        if not song.audio_url:
            logger.warning(
                f"网易云音乐未返回可播放 URL，无法构造可播放卡片："
                f"{title} - {artists}"
            )
            return False
        data_url = song.audio_url

        cover_url = song.cover_url or "https://s1.music.126.net/style/favicon.ico"
        xml = self._build_wechat_music_xml(
            appid="wx8dd6ecd81906fd84",
            appname="网易云音乐",
            title=title,
            artists=artists,
            play_url=play_url,
            data_url=data_url,
            cover_url=cover_url,
            lyrics=song.lyrics or "",
            duration=int(song.duration or 0),
            mid=song_id,
            statextstr=self._build_statextstr("wx8dd6ecd81906fd84"),
        )
        return await self._send_raw_music_card(event, xml, "网易云音乐")

    @staticmethod
    def _build_netease_outer_url(song_id: str) -> str:
        return f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"

    @staticmethod
    def _build_statextstr(appid: str) -> str:
        import base64

        appid_bytes = appid.encode("utf-8")
        if len(appid_bytes) > 127:
            return ""
        payload = b"\x1a" + bytes([len(appid_bytes) + 2]) + b"\x0a" + bytes([len(appid_bytes)]) + appid_bytes
        return base64.b64encode(payload).decode("ascii")

    @staticmethod
    async def _send_link_music_card(
        *,
        event: AiocqhttpMessageEvent,
        title: str,
        artists: str,
        play_url: str,
        cover_url: str,
    ) -> bool:
        card_seg = {
            "type": "wechat_link_card",
            "data": {
                "title": title,
                "desc": artists or "点击播放音乐",
                "url": play_url,
                "image_url": cover_url,
            },
        }
        payloads = {"message": [card_seg]}
        if event.is_private_chat():
            payloads["user_id"] = event.get_sender_id()
        else:
            payloads["group_id"] = event.get_group_id()

        try:
            result = await event.bot.api.call_action("send_msg", **payloads)
            if result is None:
                return True
            if isinstance(result, dict):
                return (
                    result.get("retcode") == 0
                    or result.get("status") == "ok"
                    or "message_id" in result
                )
            return True
        except Exception as e:
            logger.warning(f"网易云音乐链接卡片通过 send_msg 发送失败，尝试平台动作: {e!r}")

        fallback_payloads = {"message": [card_seg]}
        if event.is_private_chat():
            fallback_payloads["user_id"] = event.get_sender_id()
            action = "send_private_msg"
        else:
            fallback_payloads["group_id"] = event.get_group_id()
            action = "send_group_msg"

        try:
            result = await event.bot.api.call_action(action, **fallback_payloads)
            if result is None:
                return True
            if isinstance(result, dict):
                return (
                    result.get("retcode") == 0
                    or result.get("status") == "ok"
                    or "message_id" in result
                )
            return True
        except Exception as e:
            logger.exception(f"网易云音乐链接卡片发送失败: {e!r}")
            return False

    @staticmethod
    def _build_wechat_music_xml(
        *,
        appid: str,
        appname: str,
        title: str,
        artists: str,
        play_url: str,
        data_url: str,
        cover_url: str,
        lyrics: str,
        duration: int,
        mid: str,
        statextstr: str = "",
    ) -> str:
        statext_node = (
            f"\t\t<statextstr>{escape(statextstr)}</statextstr>\n"
            if statextstr
            else ""
        )
        return (
            f'<appmsg appid="{escape(appid)}" sdkver="0">\n'
            f"\t\t<title>{escape(title)}</title>\n"
            f"\t\t<des>{escape(artists)}</des>\n"
            "\t\t<action>view</action>\n"
            "\t\t<type>3</type>\n"
            f"\t\t<url>{escape(play_url)}</url>\n"
            f"\t\t<dataurl>{escape(data_url)}</dataurl>\n"
            "\t\t<androidsource>2</androidsource>\n"
            f"{statext_node}"
            f"\t\t<songalbumurl>{escape(cover_url)}</songalbumurl>\n"
            f"\t\t<thumburl>{escape(cover_url)}</thumburl>\n"
            f"\t\t<songlyric>{escape(lyrics)}</songlyric>\n"
            "\t\t<musicShareItem>\n"
            f"\t\t\t<mvSingerName>{escape(artists)}</mvSingerName>\n"
            "\t\t\t<mvAlbumName></mvAlbumName>\n"
            "\t\t\t<mvIssueDate>0</mvIssueDate>\n"
            f"\t\t\t<musicDuration>{duration}</musicDuration>\n"
            f"\t\t\t<mid>getlinkclisdkmid_{escape(mid)}</mid>\n"
            "\t\t</musicShareItem>\n"
            "\t\t<finderLiveProductShare>\n"
            "\t\t\t<isPriceBeginShow>false</isPriceBeginShow>\n"
            "\t\t</finderLiveProductShare>\n"
            "\t\t<gameshare>\n"
            "\t\t\t<appbrandext>\n"
            "\t\t\t\t<priority>-1</priority>\n"
            "\t\t\t</appbrandext>\n"
            "\t\t\t<duration>-1</duration>\n"
            "\t\t</gameshare>\n"
            "\t\t<appattach />\n"
            "\t\t<commenturl></commenturl>\n"
            "</appmsg>"
        )

    @staticmethod
    async def _send_raw_music_card(
        event: AiocqhttpMessageEvent, xml: str, appname: str
    ) -> bool:
        if await MusicSender._send_raw_music_card_via_onebot(event, xml, appname):
            return True
        return await MusicSender._send_raw_music_card_via_debug_control(
            event, xml, appname
        )

    @staticmethod
    async def _send_raw_music_card_via_onebot(
        event: AiocqhttpMessageEvent, xml: str, appname: str
    ) -> bool:
        payload = {
            "content": xml,
            "send_type": 11214,
        }
        self_id = event.get_self_id()
        if self_id:
            payload["self_id"] = self_id
        if event.is_private_chat():
            payload["user_id"] = event.get_sender_id()
        else:
            payload["group_id"] = event.get_group_id()

        try:
            result = await event.bot.api.call_action("send_raw_xml", **payload)
        except Exception as e:
            target = payload.get("group_id") or payload.get("user_id") or ""
            logger.warning(
                f"{appname} OneBot send_raw_xml 调用失败，尝试 debug-control: "
                f"self_id={payload.get('self_id')!r}, target={target!r}, error={e!r}"
            )
            return False

        if result is None:
            return True
        if isinstance(result, dict):
            ok = (
                result.get("retcode") == 0
                or result.get("status") == "ok"
                or result.get("ok") is True
            )
            if not ok:
                logger.warning(f"{appname} OneBot send_raw_xml 返回失败，尝试 debug-control: {result!r}")
            return ok
        return True

    @staticmethod
    async def _send_raw_music_card_via_debug_control(
        event: AiocqhttpMessageEvent, xml: str, appname: str
    ) -> bool:
        base_url = os.environ.get("WECHAT_DEBUG_CONTROL_URL", "http://127.0.0.1:5000")
        url = base_url.rstrip("/") + "/send_raw_xml"
        payload = {
            "content": xml,
            "send_type": 11214,
        }
        if event.is_private_chat():
            payload["user_id"] = event.get_sender_id()
        else:
            payload["group_id"] = event.get_group_id()

        def post_raw_xml() -> tuple[int, dict]:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json; charset=utf-8"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(body) if body else {}
                except json.JSONDecodeError:
                    parsed = {"raw": body}
                return resp.status, parsed

        try:
            status, result = await asyncio.to_thread(post_raw_xml)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            logger.error(
                f"{appname} 11214 原始 XML 发送失败: HTTP {e.code}, body={body!r}"
            )
            return False
        except Exception as e:
            logger.exception(f"{appname} 11214 原始 XML 发送异常: {e!r}")
            return False

        if 200 <= status < 300:
            if result.get("ok") is False:
                logger.error(f"{appname} 11214 原始 XML 发送失败: result={result!r}")
                return False
            return True

        logger.error(f"{appname} 11214 原始 XML 发送失败: status={status}, result={result!r}")
        return False

    async def send_record(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发语音"""
        if not song.audio_url:
            song = await player.fetch_extra(song)
        if not song.audio_url:
            await event.send(event.plain_result(f"【{song.name}】音频获取失败"))
            return False
        try:
            logger.debug(f"正在发送【{song.name}】音频: {song.audio_url}")
            seg = Record.fromURL(song.audio_url)
            await event.send(event.chain_result([seg]))
            return True
        except Exception as e:
            logger.error(f"【{song.name}】音频发送失败: {e}")
            return False

    async def send_file(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ):
        """发文件"""
        if not song.audio_url:
            song = await player.fetch_extra(song)
        if not song.audio_url:
            await event.send(event.plain_result(f"【{song.name}】音频获取失败"))
            return False

        file_path = await self.downloader.download_song(song.audio_url)

        async def send_by_url():
            try:
                # 默认使用 mp3 后缀
                file_name_url = f"{song.name}_{song.artists}.mp3"
                if song.audio_url:
                    seg_url = File(name=file_name_url, url=song.audio_url)
                    await event.send(event.chain_result([seg_url]))
                    return True
            except Exception as e_url:
                logger.error(f"URL 发送失败: {e_url}")
                return False

        if not file_path:
            logger.warning(f"【{song.name}】下载失败，尝试直接发送 URL")
            if await send_by_url():
                return True
            await event.send(
                event.plain_result(f"【{song.name}】音频文件下载和发送均失败")
            )
            return False

        try:
            file_name = f"{song.name}_{song.artists}{file_path.suffix}"
            seg = File(name=file_name, file=str(file_path.resolve()))
            await event.send(event.chain_result([seg]))
            return True
        except Exception as e:
            logger.warning(f"【{song.name}】本地文件发送失败: {e}，尝试直接发送 URL")
            if await send_by_url():
                return True

            await event.send(
                event.plain_result(f"【{song.name}】音频文件发送失败: {e}")
            )
            return False

    async def send_text(
        self, event: AstrMessageEvent, player: BaseMusicPlayer, song: Song
    ) -> bool:
        """发文本"""
        try:
            info = f"🎶{song.name} - {song.artists} {self._format_time(song.duration)}"
            song = await player.fetch_extra(song)
            info = song.to_lines()
            await event.send(event.plain_result(info))
            return True
        except Exception as e:
            logger.error(f"发送歌曲信息失败: {e}")
            return False

    def _get_sender(self, mode: str):
        return {
            "card": self.send_card,
            "record": self.send_record,
            "file": self.send_file,
            "text": self.send_text,
        }.get(mode)

    def _is_mode_supported(
        self, mode: str, event: AstrMessageEvent, player: BaseMusicPlayer
    ) -> bool:
        platform = event.get_platform_name()
        match mode:
            case "text":
                return True
            case "card":
                return platform == "aiocqhttp" and isinstance(
                    player,
                    (
                        MetingMusic,
                        NetEaseMusic,
                        NetEaseMusicNodeJS,
                        QishuiMusic,
                        XingzhigeKuwoMusic,
                    ),
                )
            case "record":
                return platform in self.cfg.record_supported
            case "file":
                return platform in self.cfg.file_supported
            case _:
                return False

    async def send_song(
        self,
        event: AstrMessageEvent,
        player: BaseMusicPlayer,
        song: Song,
        modes: list[str] | None = None,
    ):
        logger.debug(
            f"{event.get_sender_name()}（{event.get_sender_id()}）点歌："
            f"{player.platform.display_name} -> {song.name}_{song.artists}"
        )

        sent = False
        target_modes = modes if modes is not None else self.cfg.real_send_modes

        for mode in target_modes:
            if not self._is_mode_supported(mode, event, player):
                logger.debug(f"{mode} 不支持，跳过")
                continue

            sender = self._get_sender(mode)
            if not sender:
                continue

            try:
                ok = await sender(event, player, song)
            except Exception as e:
                logger.exception(f"{mode} 发送异常: {e!r}")
                ok = False

            if ok:
                logger.debug(f"{mode} 发送成功")
                sent = True
                break
            else:
                logger.debug(f"{mode} 发送失败，尝试下一种")

        if not sent:
            await event.send(event.plain_result("歌曲发送失败"))

        # 附加内容不影响主流程
        if sent and self.cfg.enable_comments:
            await self.send_comment(event, player, song)

        if sent and self.cfg.enable_lyrics:
            await self.send_lyrics(event, player, song)
