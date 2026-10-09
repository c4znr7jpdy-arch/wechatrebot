from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import replace
from html import unescape
from logging.handlers import RotatingFileHandler
import logging
from pathlib import Path
import socket
import re
import subprocess
import time
from typing import Any, Callable
import uuid
import xml.etree.ElementTree as ET

from aiohttp import ClientConnectionError, ClientSession, web
from PIL import Image, UnidentifiedImageError

from .callback_server import create_callback_app
from .config import Settings
from .event_normalizer import integer, normalize_event, scalar
from .hook_client import HookApiError, HookClient
from .history_store import MessageHistoryStore
from .inbound_image import (
    ImageCdnCandidate,
    callback_image_candidates_from_payload,
    parse_quoted_message,
)
from .launcher import start_injector
from .media import MediaStore
from .message_codec import encode_group_member_notice, encode_inbound
from .models import GroupMemberIdentity, NormalizedEvent, RecallRecord, ReplyTarget
from .onebot_client import OneBotActionRouter, OneBotClient
from .state import BridgeState


logger = logging.getLogger(__name__)


class RedactingFilter(logging.Filter):
    _base64 = re.compile(r"base64://[^\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._base64.sub("base64://<redacted>", record.msg)
            if len(record.msg) > 1000:
                record.msg = f"{record.msg[:1000]}…"
        return True


def configure_logging(settings: Settings) -> None:
    log_dir = settings.project_root / "var" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    package_logger = logging.getLogger("hook_bridge")
    package_logger.setLevel(getattr(logging, settings.log_level))
    package_logger.propagate = False
    for handler in list(package_logger.handlers):
        package_logger.removeHandler(handler)
        handler.close()
    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    redactor = RedactingFilter()
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(redactor)
    rotating = RotatingFileHandler(
        log_dir / "bridge.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    rotating.setFormatter(formatter)
    rotating.addFilter(redactor)
    package_logger.addHandler(console)
    package_logger.addHandler(rotating)


class BridgeApplication:
    def __init__(
        self,
        settings: Settings,
        *,
        launch_injector: bool = True,
        hook: Any = None,
        media: Any = None,
        state: BridgeState | None = None,
        history: Any = None,
        router: Any = None,
        onebot: Any = None,
        injector_factory: Callable[[Settings], subprocess.Popen[bytes]] = start_injector,
        hook_health_interval_seconds: float = 5.0,
        hook_health_failure_limit: int = 6,
        group_member_poll_interval_seconds: float = 15.0,
    ) -> None:
        self.settings = settings
        self.launch_injector = launch_injector
        self.hook = hook
        self.media = media
        self.state = state or BridgeState(
            settings.dedup_ttl_seconds, settings.dedup_capacity
        )
        self.history = history
        self.router = router
        self.onebot = onebot
        self.injector_factory = injector_factory
        self.hook_health_interval_seconds = max(0.0, hook_health_interval_seconds)
        self.hook_health_failure_limit = max(1, hook_health_failure_limit)
        self.group_member_poll_interval_seconds = max(
            0.0, group_member_poll_interval_seconds
        )
        self.event_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=settings.queue_size
        )
        self._session: ClientSession | None = None
        self._owns_session = False
        self._callback_runner: web.AppRunner | None = None
        self._ws_task: asyncio.Task[None] | None = None
        self._consumer_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._injector_watch_task: asyncio.Task[None] | None = None
        self._hook_watch_task: asyncio.Task[None] | None = None
        self._group_member_watch_task: asyncio.Task[None] | None = None
        # The 4.1.8.27 video endpoint may stop accepting new HTTP connections
        # while a native download is running.  Keep the health watcher from
        # treating that expected interval as a fatal hook failure.
        self._active_video_downloads = 0
        # Keep the query-derived baseline separate from BridgeState.  Direct
        # callbacks can arrive before /api/get_room_members refreshes its
        # cache; sharing the same map would turn that temporary stale result
        # into a false inverse notification on the next poll.
        self._polled_group_members: dict[
            str, dict[str, GroupMemberIdentity]
        ] = {}
        self._polled_group_names: dict[str, str] = {}
        self._injector: subprocess.Popen[bytes] | Any | None = None
        self._stop_event = asyncio.Event()
        self._hook_ready = False

    async def _ensure_components(self) -> None:
        if self.hook is None or self.media is None:
            self._session = ClientSession()
            self._owns_session = True
        if self.hook is None:
            assert self._session is not None
            self.hook = HookClient(
                self.settings.hook_base_url,
                self._session,
                timeout=self.settings.request_timeout_seconds,
                image_path_field=self.settings.image_path_field,
                video_download_timeout_seconds=self.settings.video_download_timeout_seconds,
                video_upload_timeout_seconds=self.settings.video_upload_timeout_seconds,
            )
        if self.media is None:
            assert self._session is not None
            self.media = MediaStore(
                self.settings.media_root,
                self._session,
                self.settings.media_max_bytes,
                timeout=self.settings.request_timeout_seconds,
            )
        if self.router is None:
            if self.history is None:
                self.history = MessageHistoryStore(
                    self.settings.project_root / "var" / "message-history.db"
                )
            self.router = OneBotActionRouter(
                self.hook,
                self.media,
                self.state,
                video_send_mode=self.settings.video_send_mode,
                history=self.history,
            )
        if self.onebot is None:
            self.onebot = OneBotClient(
                self.settings.astrbot_ws_url,
                self.router,
                reconnect_min=self.settings.reconnect_min_seconds,
                reconnect_max=self.settings.reconnect_max_seconds,
                event_queue_size=self.settings.queue_size,
            )

    async def _start_callback_server(self) -> None:
        app = create_callback_app(
            self.event_queue,
            self.settings.callback_max_body_bytes,
            self.settings.callback_path,
            action_router=self.router,
            status_provider=self._health_snapshot,
        )
        self._callback_runner = web.AppRunner(app, access_log=None)
        await self._callback_runner.setup()
        site = web.TCPSite(
            self._callback_runner,
            self.settings.callback_host,
            self.settings.callback_port,
        )
        await site.start()
        logger.info("HOOK callback listening on %s", self.settings.callback_url)

    async def _stop_callback_server(self) -> None:
        if self._callback_runner is not None:
            await self._callback_runner.cleanup()
            self._callback_runner = None

    async def _wait_hook_ready(self) -> None:
        deadline = asyncio.get_running_loop().time() + self.settings.hook_start_timeout_seconds
        while asyncio.get_running_loop().time() < deadline:
            process = self._injector
            if (
                process is not None
                and process.poll() is not None
                and process.returncode != 0
            ):
                stderr = b""
                if getattr(process, "stderr", None) is not None:
                    stderr = await asyncio.to_thread(process.stderr.read, 2000)
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"injector exited before HOOK API became ready: "
                    f"code={process.returncode}, stderr={detail[:500]}"
                )
            if await self._hook_api_accepting_connections():
                logger.info("HOOK API is accepting connections")
                return
            await asyncio.sleep(0.2)
        injector_status = (
            "running"
            if self._injector is not None and self._injector.poll() is None
            else f"exited with code {getattr(self._injector, 'returncode', 'unknown')}"
        )
        raise TimeoutError(
            f"HOOK API did not listen on {self.settings.hook_api_host}:"
            f"{self.settings.hook_api_port} within "
            f"{self.settings.hook_start_timeout_seconds:g}s; injector {injector_status}"
        )

    async def _hook_api_accepting_connections(self) -> bool:
        try:
            reader, writer = await asyncio.open_connection(
                self.settings.hook_api_host, self.settings.hook_api_port
            )
            del reader
            writer.close()
            await writer.wait_closed()
            return True
        except OSError:
            return False

    async def _health_snapshot(self) -> dict[str, object]:
        onebot_connected = bool(getattr(self.onebot, "connected", False))
        heartbeat_fresh = bool(getattr(self.onebot, "heartbeat_fresh", False))
        hook_ready = self._hook_ready and await self._hook_api_accepting_connections()
        if self._hook_ready and not hook_ready:
            logger.info("HOOK API is no longer accepting connections")
            self._hook_ready = False
        wechat_identity = self.state.bot_wxid if hook_ready else ""
        return {
            "service_ready": True,
            "status_available": True,
            "hook_ready": hook_ready,
            "wechat_logged_in": bool(wechat_identity),
            "wechat_identity": wechat_identity,
            "upstream_connected": onebot_connected,
            "heartbeat_fresh": heartbeat_fresh and hook_ready,
        }

    def _find_free_hook_api_port(self) -> int:
        for _ in range(8):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                sock.bind((self.settings.hook_api_host, 0))
                candidate = int(sock.getsockname()[1])
            if candidate != self.settings.callback_port:
                return candidate
        raise RuntimeError("failed to select a free hook_api_port")

    async def _select_dedicated_hook_api_port(self) -> None:
        if self.settings.allow_existing_hook_reuse:
            return
        if await self._hook_api_accepting_connections():
            new_port = self._find_free_hook_api_port()
            logger.info(
                "HOOK API is already accepting connections on %s; starting "
                "dedicated bot WeChat on alternate hook_api_port=%s",
                self.settings.hook_base_url,
                new_port,
            )
            self.settings = replace(self.settings, hook_api_port=new_port)

    async def _consume_events(self) -> None:
        while True:
            payload = await self.event_queue.get()
            try:
                event = normalize_event(payload)
                self._observe_outgoing_image_receipt(payload, event)
                if event.kind == "login":
                    data = payload.get("data")
                    details = data if isinstance(data, dict) else payload
                    avatar = scalar(details.get("head") or details.get("avatar"))
                    self.state.set_identity(
                        event.account_id, event.sender_name, avatar
                    )
                    await self.onebot.set_identity(
                        event.account_id, event.sender_name
                    )
                elif event.kind == "message":
                    if not self.state.accept_message(event.message_id):
                        logger.debug("ignored duplicate callback message %s", event.message_id)
                        continue
                    self_id = self.state.bot_wxid or event.account_id or "wechat_bot"
                    event = await self._enrich_group_sender(event)
                    if (
                        event.conversation_id.endswith("@chatroom")
                        and not event.group_name
                    ):
                        event = replace(
                            event,
                            group_name=self._polled_group_names.get(
                                event.conversation_id, ""
                            ),
                        )
                    await self._record_inbound_history(event)
                    event = await self._prepare_inbound_media(event)
                    event = self._ensure_quoted_wake_mention(event, self_id)
                    quoted_reply_id: int | None = None
                    if event.quoted_message_type in {1, 3}:
                        quoted_reply_id = self.state.next_message_id()
                        quoted_event = self._build_quoted_onebot_event(
                            event, self_id, quoted_reply_id
                        )
                        self.state.remember_message(quoted_reply_id, quoted_event)
                    onebot_message_id = self.state.next_message_id()
                    onebot_event = encode_inbound(
                        event,
                        self_id,
                        onebot_message_id,
                        quoted_reply_id,
                    )
                    self.state.remember_message(onebot_message_id, onebot_event)
                    self.state.remember_group_message(onebot_event)
                    if (
                        not event.conversation_id.endswith("@chatroom")
                        or self_id in event.mentions
                        or event.content.lstrip().startswith(("/", "\\"))
                    ):
                        self.state.remember_reply_target(
                            event.conversation_id,
                            ReplyTarget(
                                message_id=event.message_id,
                                create_time=event.create_time,
                                sender_id=event.sender_id,
                                sender_name=event.sender_name
                                or event.wechat_name
                                or event.group_nickname,
                                content=event.content,
                            ),
                            message_id=onebot_message_id,
                        )
                    await self.onebot.send_event(onebot_event)
                elif event.kind == "group_member_update":
                    self.state.remember_group_member_identity(
                        event.conversation_id,
                        event.sender_id,
                        GroupMemberIdentity(
                            event.wechat_name,
                            event.group_nickname,
                        ),
                    )
                elif event.kind in {"group_increase", "group_decrease"}:
                    event = await self._resolve_group_member_notice(event)
                    if not event.conversation_id or not event.sender_id:
                        raw_data = event.raw.get("data")
                        raw_data = raw_data if isinstance(raw_data, Mapping) else event.raw
                        raw_member = raw_data.get("memberlist") or raw_data.get("memberList")
                        raw_member = raw_member if isinstance(raw_member, Mapping) else {}
                        logger.warning(
                            "ignored incomplete group member notice kind=%s room=%r member=%r "
                            "payload_keys=%s data_keys=%s member_keys=%s",
                            event.kind,
                            event.conversation_id,
                            event.sender_id,
                            sorted(event.raw),
                            sorted(raw_data),
                            sorted(raw_member),
                        )
                        continue
                    if not self._callback_matches_polled_member_transition(event):
                        continue
                    event = await self._enrich_group_member_notice(event)
                    if event.kind == "group_increase" and not (
                        event.wechat_name.strip()
                        or event.group_nickname.strip()
                    ):
                        # A 4.1.8.27 callback can arrive before its documented
                        # member-list endpoint exposes the new record.  Do not
                        # consume the de-dup key or send an "unknown member"
                        # card: the polling diff will emit the same join once
                        # the authoritative member record becomes available.
                        logger.info(
                            "deferred group increase callback until member profile is available "
                            "room=%s member=%s",
                            event.conversation_id,
                            event.sender_id,
                        )
                        continue
                    await self._forward_group_member_notice(event, source="callback")
                elif event.kind == "unknown":
                    nested = payload.get("data")
                    nested_mapping = nested if isinstance(nested, Mapping) else {}
                    callback_keys = set(payload) | set(nested_mapping)
                    callback_labels = tuple(
                        payload.get(key)
                        for key in (
                            "event_type",
                            "even_type",
                            "messageType",
                            "event_desc",
                            "eventDesc",
                            "messageDesc",
                            "even_desc",
                        )
                        if payload.get(key) not in (None, "")
                    )
                    if {"memberlist", "member_list"} & callback_keys:
                        logger.warning(
                            "ignored unrecognized HOOK group member callback labels=%r keys=%s",
                            callback_labels,
                            sorted(callback_keys),
                        )
                    else:
                        logger.debug(
                            "ignored unknown HOOK callback event_type=%r keys=%s",
                            payload.get(
                                "event_type",
                                payload.get("even_type", payload.get("messageType")),
                            ),
                            sorted(payload),
                        )
            except Exception:
                logger.exception("failed to process one HOOK callback")
            finally:
                self.event_queue.task_done()

    async def _record_inbound_history(self, event: NormalizedEvent) -> None:
        recorder = getattr(self.history, "record_inbound", None)
        if not callable(recorder):
            return
        try:
            await asyncio.to_thread(recorder, event)
        except Exception:
            logger.exception(
                "failed to persist inbound message history message_id=%s",
                event.message_id,
            )

    def _ensure_quoted_wake_mention(
        self, event, self_id: str
    ):
        """Recover @bot from type=57 title when HOOK omits msgSource atuserlist."""
        if (
            event.quoted_message_type not in {1, 3}
            or not event.conversation_id.endswith("@chatroom")
        ):
            return event
        title = event.content.strip()
        nickname = self.state.bot_nickname.strip()
        current_title_mentions_bot = self._title_mentions_bot(title, nickname)

        # msgSource/atuserlist is copied from the nested refermsg by some HOOK
        # builds.  It therefore describes the quoted message, not necessarily
        # this type=57 wrapper.  Never let that stale bot id wake the bot.
        current_mentions = tuple(
            item for item in event.mentions if str(item) != str(self_id)
        )
        if current_title_mentions_bot:
            current_mentions += (self_id,)
        if current_mentions == event.mentions:
            return event
        return replace(event, mentions=current_mentions)

    @staticmethod
    def _title_mentions_bot(title: str, nickname: str) -> bool:
        """Match a real leading @mention, not a name such as ``@姜小妹子``."""
        if not title or not nickname or not title.startswith("@"):
            return False
        remainder = title[1:]
        if not remainder.startswith(nickname):
            return False
        suffix = remainder[len(nickname) :]
        if not suffix:
            return True
        # WeChat normally inserts U+2005 or another whitespace character after
        # a mention.  Punctuation is also a valid boundary for a bare mention.
        return suffix[0].isspace() or suffix[0] in "，。！？：:,.!?、)]）】》>"

    @staticmethod
    def _build_quoted_onebot_event(
        event, self_id: str, message_id: int
    ) -> dict[str, Any]:
        """Create the synthetic get_msg payload used by AstrBot's Reply parser."""
        sender_id = event.quoted_sender_id or event.sender_id
        sender_name = event.quoted_sender_name or event.sender_name
        is_group = event.conversation_id.endswith("@chatroom")
        quoted_text = event.quoted_content.strip()
        if event.quoted_message_type == 3:
            if event.media_path:
                message = [
                    {
                        "type": "image",
                        "data": {"file": event.media_path.resolve().as_uri()},
                    }
                ]
                raw_message = "[图片]"
            else:
                message = [
                    {
                        "type": "text",
                        "data": {"text": "[引用图片下载失败，无法识图，请勿猜测图片内容]"},
                    }
                ]
                raw_message = "[图片下载失败]"
        else:
            message = [{"type": "text", "data": {"text": quoted_text}}]
            raw_message = quoted_text
        payload: dict[str, Any] = {
            "time": event.create_time or int(time.time()),
            "self_id": self_id,
            "post_type": "message",
            "message_type": "group" if is_group else "private",
            "sub_type": "normal" if is_group else "friend",
            "message_id": message_id,
            "user_id": sender_id,
            "message": message,
            "raw_message": raw_message,
            "font": 0,
            "sender": {
                "user_id": sender_id,
                "nickname": sender_name,
                "display_name": sender_name,
                "card": sender_name if is_group else "",
                "sex": "unknown",
                "age": 0,
            },
        }
        if is_group:
            payload["group_id"] = event.conversation_id
        return payload

    @staticmethod
    def _group_member_identities(
        members: list[Mapping[str, Any]],
    ) -> dict[str, GroupMemberIdentity]:
        identities: dict[str, GroupMemberIdentity] = {}
        for member in members:
            member_wxid = scalar(
                member.get("userName")
                or member.get("wxid")
                or member.get("user_id")
            ).strip()
            if not member_wxid:
                continue
            identities[member_wxid] = GroupMemberIdentity(
                wechat_name=scalar(
                    member.get("nickName") or member.get("nickname")
                ).strip(),
                group_nickname=scalar(
                    member.get("displayName")
                    or member.get("display_name")
                    or member.get("card")
                ).strip(),
                avatar=scalar(
                    member.get("smallHeadImgUrl")
                    or member.get("bigHeadImgUrl")
                    or member.get("avatar")
                    or member.get("avatar_url")
                ).strip(),
            )
        return identities

    async def _enrich_group_sender(self, event):
        room_id = event.conversation_id
        member_wxid = event.sender_id
        if not room_id.endswith("@chatroom") or not member_wxid:
            return event

        cached = self.state.group_member_identity(room_id, member_wxid)
        needs_load = (
            not self.state.has_group_member_snapshot(room_id) or cached is None
        )
        if needs_load:
            try:
                members = await self.hook.get_group_detail_members(room_id)
                if not members:
                    # Some 4.1.8.27 builds return an empty detail cache while
                    # /api/get_room_members already has the authoritative list.
                    members = await self.hook.get_group_members(room_id)
                if not members:
                    # Preserve compatibility with HOOK builds where the list
                    # endpoint only refreshes the cache and the second detail
                    # read is the one that returns the records.
                    members = await self.hook.get_group_detail_members(room_id)
                self.state.replace_group_member_snapshot(
                    room_id, self._group_member_identities(members)
                )
                cached = self.state.group_member_identity(room_id, member_wxid)
                if cached is None:
                    members = await self.hook.get_group_members(room_id)
                    self.state.replace_group_member_snapshot(
                        room_id, self._group_member_identities(members)
                    )
                    cached = self.state.group_member_identity(room_id, member_wxid)
            except Exception:
                logger.exception(
                    "failed to resolve group sender name room=%s member=%s",
                    room_id,
                    member_wxid,
                )

        if cached is None:
            return event
        return replace(
            event,
            wechat_name=event.wechat_name or cached.wechat_name,
            group_nickname=event.group_nickname or cached.group_nickname,
        )

    async def _resolve_group_member_notice(self, event):
        """Fill a missing callback member id from the current group snapshot.

        A few 4.1.8.27 builds send a leave callback with a valid room but an
        empty ``memberlist.userName``.  Once the bridge has a pre-event member
        snapshot, the single member missing from the fresh list is the only
        safe identity to use.  Ambiguous diffs remain suppressed rather than
        guessing a member.
        """
        if event.sender_id or not event.conversation_id.endswith("@chatroom"):
            return event
        room_id = event.conversation_id
        try:
            members = await self.hook.get_group_members(room_id)
        except Exception:
            logger.exception(
                "failed to refresh group members for incomplete notice room=%s",
                room_id,
            )
            return event
        current = self._group_member_identities(members)
        previous = dict(self.state.group_members.get(room_id, {}))
        if not self.state.has_group_member_snapshot(room_id):
            if current:
                self.state.replace_group_member_snapshot(room_id, current)
                logger.info(
                    "initialized group member baseline while resolving incomplete notice room=%s members=%s",
                    room_id,
                    len(current),
                )
            return event

        if event.kind == "group_decrease":
            candidates = set(previous) - set(current)
            identity_source = previous
        else:
            candidates = set(current) - set(previous)
            identity_source = current
        if len(candidates) != 1:
            logger.warning(
                "cannot resolve incomplete group member notice room=%s kind=%s candidates=%s",
                room_id,
                event.kind,
                len(candidates),
            )
            self.state.replace_group_member_snapshot(room_id, current)
            return event

        member_id = next(iter(candidates))
        identity = identity_source.get(member_id, GroupMemberIdentity())
        self.state.replace_group_member_snapshot(room_id, current)
        return replace(
            event,
            sender_id=member_id,
            sender_name=event.sender_name or identity.wechat_name,
            wechat_name=event.wechat_name or identity.wechat_name,
            group_nickname=event.group_nickname or identity.group_nickname,
            member_avatar=event.member_avatar or identity.avatar,
        )

    def _callback_matches_polled_member_transition(self, event) -> bool:
        """Reject callbacks that contradict the last query-derived baseline.

        HOOK 4.1.8.27 can report an existing member as a fresh join when a
        different member enters the room.  The polling snapshot is kept
        independently specifically so it can validate that a callback really
        changes membership without being mutated by the callback itself.
        """
        previous = self._polled_group_members.get(event.conversation_id)
        if previous is None:
            return True

        was_present = event.sender_id in previous
        if event.kind == "group_increase" and was_present:
            logger.info(
                "ignored stale group member callback kind=%s room=%s member=%s "
                "reason=already_present",
                event.kind,
                event.conversation_id,
                event.sender_id,
            )
            return False
        if event.kind == "group_decrease" and not was_present:
            logger.info(
                "ignored stale group member callback kind=%s room=%s member=%s "
                "reason=already_absent",
                event.kind,
                event.conversation_id,
                event.sender_id,
            )
            return False
        return True

    async def _enrich_group_member_notice(self, event):
        """Fill callback card metadata from documented member-list results."""
        room_id = event.conversation_id
        member_id = event.sender_id
        group_name = event.group_name or self._polled_group_names.get(room_id, "")

        # Departed members are no longer returned by the live API, so prefer
        # their identity from the pre-event polling baseline.  State is useful
        # as a fallback before the first polling baseline has been established.
        identity = self._polled_group_members.get(room_id, {}).get(member_id)
        if identity is None:
            identity = self.state.group_member_identity(room_id, member_id)

        needs_profile = not (
            event.wechat_name.strip() or event.group_nickname.strip()
        )
        if event.kind == "group_increase" and needs_profile:
            try:
                current = self._group_member_identities(
                    await self.hook.get_group_members(room_id)
                )
                identity = current.get(member_id) or identity
            except Exception:
                logger.exception(
                    "failed to load group member profile for callback room=%s member=%s",
                    room_id,
                    member_id,
                )

        if identity is None:
            return replace(event, group_name=group_name)
        return replace(
            event,
            sender_name=event.sender_name or identity.wechat_name,
            wechat_name=(
                event.wechat_name
                or identity.wechat_name
                or event.sender_name
            ),
            group_nickname=event.group_nickname or identity.group_nickname,
            group_name=group_name,
            member_avatar=event.member_avatar or identity.avatar,
        )

    async def _forward_group_member_notice(self, event, *, source: str) -> bool:
        """Send one native OneBot member notice with cross-source de-duplication."""
        room_id = event.conversation_id
        member_id = event.sender_id
        if not room_id or not member_id:
            return False

        identity = GroupMemberIdentity(
            event.wechat_name,
            event.group_nickname,
            event.member_avatar,
        )
        if event.kind == "group_increase":
            self.state.remember_group_member_identity(room_id, member_id, identity)

        # Callback and polling events often describe the same change at
        # different times.  Do not include create_time in this key so the
        # normal bridge de-dup TTL covers both sources.
        dedup_key = f"group-member:{event.kind}:{room_id}:{member_id}"
        if not self.state.accept_message(dedup_key):
            logger.info(
                "ignored duplicate group member notice source=%s kind=%s room=%s member=%s",
                source,
                event.kind,
                room_id,
                member_id,
            )
            return False

        self_id = self.state.bot_wxid or event.account_id or "wechat_bot"
        await self.onebot.send_event(encode_group_member_notice(event, self_id))
        logger.info(
            "forwarded group member notice source=%s kind=%s room=%s member=%s subtype=%s",
            source,
            event.kind,
            room_id,
            member_id,
            event.notice_sub_type,
        )
        return True

    @staticmethod
    def _group_identity(group: Mapping[str, Any]) -> tuple[str, str]:
        room_id = scalar(
            group.get("wxid")
            or group.get("userName")
            or group.get("username")
            or group.get("group_id")
            or group.get("roomid")
            or group.get("room_id")
        ).strip()
        group_name = scalar(
            group.get("nickname")
            or group.get("nickName")
            or group.get("nick_name")
            or group.get("group_name")
            or group.get("roomname")
        ).strip()
        return room_id, group_name

    async def _poll_group_member_changes_once(self) -> int:
        """Refresh documented group-member lists and emit snapshot diffs."""
        groups = await self.hook.get_groups()
        emitted = 0
        for raw_group in groups:
            if not isinstance(raw_group, Mapping):
                continue
            room_id, group_name = self._group_identity(raw_group)
            if not room_id.endswith("@chatroom"):
                continue
            if group_name:
                self._polled_group_names[room_id] = group_name

            try:
                members = await self.hook.get_group_members(room_id)
            except Exception:
                logger.exception(
                    "failed to poll HOOK group members room=%s", room_id
                )
                continue
            current = self._group_member_identities(members)
            # A bot that belongs to the group cannot have a genuinely empty
            # member list.  Treat empty as an unrefreshed/error response so a
            # transient API failure cannot announce that everybody left.
            if not current:
                logger.warning(
                    "ignored empty HOOK group member poll room=%s", room_id
                )
                continue

            previous = self._polled_group_members.get(room_id)
            if previous is None:
                self._polled_group_members[room_id] = current
                self.state.replace_group_member_snapshot(room_id, current)
                logger.info(
                    "initialized HOOK group member baseline room=%s members=%s",
                    room_id,
                    len(current),
                )
                continue

            added = set(current) - set(previous)
            removed = set(previous) - set(current)
            changed = len(added) + len(removed)
            suspicious_limit = max(2, len(previous) // 2)
            if len(previous) >= 3 and changed > suspicious_limit:
                logger.warning(
                    "ignored suspicious HOOK group member poll room=%s previous=%s current=%s changed=%s",
                    room_id,
                    len(previous),
                    len(current),
                    changed,
                )
                continue

            self._polled_group_members[room_id] = current
            self.state.replace_group_member_snapshot(room_id, current)
            resolved_group_name = group_name or self._polled_group_names.get(
                room_id, ""
            )
            for kind, member_ids, identities, subtype in (
                ("group_increase", sorted(added), current, "approve"),
                ("group_decrease", sorted(removed), previous, "leave"),
            ):
                for member_id in member_ids:
                    identity = identities.get(member_id, GroupMemberIdentity())
                    event = NormalizedEvent(
                        kind=kind,
                        account_id=self.state.bot_wxid,
                        conversation_id=room_id,
                        sender_id=member_id,
                        sender_name=identity.wechat_name,
                        wechat_name=identity.wechat_name,
                        group_nickname=identity.group_nickname,
                        message_id=f"{kind}:{room_id}:{member_id}",
                        create_time=int(time.time()),
                        group_name=resolved_group_name,
                        member_avatar=identity.avatar,
                        notice_sub_type=subtype,
                    )
                    if await self._forward_group_member_notice(
                        event, source="member_poll"
                    ):
                        emitted += 1
        return emitted

    async def _watch_group_member_changes(self) -> None:
        # Let login and the local HOOK cache settle before taking the first
        # baseline.  Subsequent refreshes provide a fallback when the DLL does
        # not deliver its documented join/leave callbacks.
        await asyncio.sleep(min(3.0, self.group_member_poll_interval_seconds))
        while True:
            try:
                await self._poll_group_member_changes_once()
            except (HookApiError, ClientConnectionError, asyncio.TimeoutError) as exc:
                diagnostics = getattr(exc, "diagnostics", {})
                logger.warning(
                    "HOOK group member polling delayed; will retry: error=%s diagnostics=%s",
                    exc,
                    diagnostics,
                )
            except Exception:
                logger.exception("HOOK group member polling failed")
            await asyncio.sleep(self.group_member_poll_interval_seconds)

    def _observe_outgoing_image_receipt(self, payload: Mapping[str, Any], event) -> None:
        observer = getattr(self.router, "observe_outgoing_image", None)
        if not callable(observer):
            return
        embedded = payload.get("data")
        embedded_is_envelope = isinstance(embedded, Mapping) and any(
            key in embedded
            for key in (
                "msgType",
                "type",
                "newMsgId",
                "NewMsgId",
                "fromUserName",
                "toUserName",
            )
        )
        data = embedded if embedded_is_envelope else payload
        account_id = scalar(payload.get("account_wxid") or data.get("account_wxid"))
        message_name = scalar(payload.get("messageType")).strip()
        message_desc = scalar(
            payload.get("messageDesc") or data.get("messageDesc")
        ).strip()
        documented_callback = (
            "个人发送图片消息" in message_name
            or "自己发送的图片消息" in message_desc
        )
        # The 4.1.8.27 DLL calls this callback
        # ``个人发送图片消息服务器响应`` and does not guarantee that the
        # numeric message type is present at the top level.  If a type is
        # present, still require the image value; otherwise the documented
        # callback name is the type discriminator.
        raw_message_type = data.get("msgType", data.get("type"))
        if raw_message_type not in (None, "") and integer(raw_message_type) != 3:
            return
        self_sync = bool(
            event.kind == "ignored"
            and account_id
            and event.sender_id == account_id
        )
        if not self_sync and event.kind not in {"outgoing", "send_receipt"}:
            if not documented_callback:
                return

        message_id = scalar(
            data.get("newMsgId")
            or data.get("NewMsgId")
            or data.get("msgid")
            or data.get("msg_id")
            or data.get("MsgSvrID")
            or data.get("msgSvrId")
        )
        create_time = integer(data.get("createTime", data.get("create_time")))
        if not message_id or not create_time:
            logger.info(
                "outgoing image callback has no recall receipt keys=%s",
                sorted(str(key) for key in data),
            )
            return

        candidates = (
            event.conversation_id,
            scalar(data.get("toUserName") or data.get("to_user_name")),
            scalar(data.get("fromUserName") or data.get("from_user_name")),
        )
        target = next(
            (value for value in candidates if value and value != account_id), ""
        )
        if not target:
            return
        record = RecallRecord(message_id, create_time, target)
        if observer(record):
            logger.info(
                "matched outgoing image receipt target=%s message_id=%s",
                target,
                message_id,
            )

    @staticmethod
    def _field(data: Mapping[str, Any], *names: str, default: Any = "") -> Any:
        for name in names:
            if name in data and data[name] not in (None, ""):
                value = data[name]
                if isinstance(value, Mapping):
                    return value.get("String", default)
                return value
        return default

    @staticmethod
    def _hook_response_summary(response: Any) -> dict[str, Any]:
        if not isinstance(response, Mapping):
            return {"response_type": type(response).__name__}
        summary: dict[str, Any] = {}
        for key in ("code", "errCode", "status", "success"):
            if key in response:
                summary[key] = response[key]
        for key in ("errMsg", "msg", "message"):
            if key not in response:
                continue
            value = response[key]
            if isinstance(value, Mapping):
                summary[f"{key}_keys"] = sorted(str(item) for item in value)
            else:
                summary[key] = str(value)[:240]
        data = response.get("data")
        if isinstance(data, Mapping):
            summary["data_keys"] = sorted(str(key) for key in data)
        elif data is not None:
            summary["data_type"] = type(data).__name__
        return summary

    @classmethod
    def _video_xml_metadata(cls, data: Mapping[str, Any]) -> dict[str, Any]:
        for field in ("content_xml", "real_content", "content"):
            raw = str(cls._field(data, field, default="")).strip()
            if not raw:
                continue
            decoded = raw
            for _ in range(5):
                next_value = unescape(decoded)
                if next_value == decoded:
                    break
                decoded = next_value
            try:
                root = ET.fromstring(decoded)
            except ET.ParseError:
                continue
            video = next(
                (
                    node
                    for node in root.iter()
                    if str(node.tag).rsplit("}", 1)[-1].lower() == "videomsg"
                ),
                None,
            )
            if video is None:
                continue
            attributes = {
                str(key).rsplit("}", 1)[-1].replace("_", "").lower(): str(value)
                for key, value in video.attrib.items()
            }

            def positive_integer(*names: str) -> int:
                for name in names:
                    try:
                        value = int(attributes.get(name, ""))
                    except (TypeError, ValueError):
                        continue
                    if value > 0:
                        return value
                return 0

            return {
                "length": positive_integer("length", "rawlength"),
                "raw_length": positive_integer("rawlength"),
                "play_length": positive_integer("playlength"),
                "has_cdn_url": bool(attributes.get("cdnvideourl")),
            }
        return {}

    async def _wait_for_media_download(
        self,
        destination: Path,
        expected_size: int,
        *,
        timeout_seconds: float | None = None,
    ) -> tuple[bool, int | None]:
        loop = asyncio.get_running_loop()
        timeout_seconds = (
            self.settings.request_timeout_seconds
            if timeout_seconds is None
            else max(0.001, float(timeout_seconds))
        )
        if expected_size <= 0:
            timeout_seconds = min(timeout_seconds, 0.75)
        deadline = loop.time() + timeout_seconds
        poll_interval = min(
            0.1,
            max(0.005, self.settings.request_timeout_seconds / 10),
        )
        previous_size: int | None = None
        stable_checks = 0
        while True:
            actual_size = destination.stat().st_size if destination.is_file() else None
            if expected_size > 0:
                if actual_size is not None and actual_size >= expected_size:
                    return True, actual_size
            elif actual_size is not None and actual_size > 0:
                if actual_size == previous_size:
                    stable_checks += 1
                else:
                    stable_checks = 0
                if stable_checks >= 2:
                    return True, actual_size
            previous_size = actual_size
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False, actual_size
            await asyncio.sleep(min(poll_interval, remaining))

    @staticmethod
    def _valid_image(path: Path) -> bool:
        try:
            with Image.open(path) as image:
                image.verify()
            return True
        except (OSError, ValueError, UnidentifiedImageError):
            return False

    @staticmethod
    def _image_attempt_path(destination: Path, route: str) -> Path:
        return destination.with_name(
            f"{destination.stem}-{route}-{uuid.uuid4().hex}{destination.suffix}"
        )

    @staticmethod
    def _discard_media(path: Path, message_id: str) -> None:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "could not remove incomplete inbound media message=%s media_name=%s",
                message_id,
                path.name,
            )

    @staticmethod
    def _promote_image(source: Path, destination: Path, message_id: str) -> bool:
        try:
            source.replace(destination)
            return True
        except OSError:
            logger.exception(
                "could not promote validated inbound image message=%s media_name=%s",
                message_id,
                source.name,
            )
            return False

    async def _download_cdn_candidates(
        self,
        destination: Path,
        candidates: tuple[ImageCdnCandidate, ...],
        message_id: str,
    ) -> bool:
        for candidate in candidates:
            attempt = self._image_attempt_path(
                destination, f"cdn-{candidate.image_type}"
            )
            try:
                response = await self.hook.download_cdn(
                    candidate.file_id,
                    candidate.aes_key,
                    candidate.image_type,
                    attempt,
                )
                complete, actual_size = await self._wait_for_media_download(
                    attempt, 0
                )
                logger.info(
                    "inbound image CDN download message=%s imgType=%s response=%s "
                    "actual_size=%s complete=%s",
                    message_id,
                    candidate.image_type,
                    self._hook_response_summary(response),
                    actual_size,
                    complete,
                )
                if (
                    complete
                    and actual_size is not None
                    and actual_size <= self.settings.media_max_bytes
                    and self._valid_image(attempt)
                    and self._promote_image(attempt, destination, message_id)
                ):
                    return True
            except Exception:
                logger.exception(
                    "inbound image CDN download failed message=%s imgType=%s",
                    message_id,
                    candidate.image_type,
                )
            self._discard_media(attempt, message_id)
        return False

    async def _prepare_inbound_media(self, event):
        if event.message_type not in {3, 34, 43, 47, 49}:
            return event
        embedded = event.raw.get("data")
        data = embedded if isinstance(embedded, Mapping) else event.raw
        quoted_message = None
        if event.message_type == 49:
            for field in ("content_xml", "real_content", "content"):
                raw_xml = str(self._field(data, field, default="")).strip()
                if not raw_xml:
                    continue
                quoted_message = parse_quoted_message(raw_xml)
                if quoted_message is not None:
                    break
        suffix_by_type = {
            3: ".jpg",
            34: ".silk",
            43: ".mp4",
            47: ".gif",
            49: ".jpg" if quoted_message is not None and quoted_message.quote_type == 3 else ".bin",
        }
        destination_root = self.settings.media_root.resolve()
        destination_root.mkdir(parents=True, exist_ok=True)
        destination = destination_root / f"inbound-{uuid.uuid4().hex}{suffix_by_type[event.message_type]}"
        try:
            if quoted_message is not None and quoted_message.quote_type in {1, 3}:
                event = replace(
                    event,
                    content=quoted_message.user_text or event.content,
                    quoted_image=quoted_message.quote_type == 3,
                    quoted_message_type=quoted_message.quote_type,
                    quoted_content=quoted_message.content,
                    quoted_sender_id=quoted_message.sender_id,
                    quoted_sender_name=quoted_message.sender_name,
                    quoted_message_id=quoted_message.message_id,
                )
                if quoted_message.quote_type == 1:
                    destination.unlink(missing_ok=True)
                    return event
                if await self._download_cdn_candidates(
                    destination,
                    quoted_message.candidates,
                    event.message_id,
                ):
                    return replace(event, media_path=destination)
                destination.unlink(missing_ok=True)
                logger.warning("quoted image download failed for message %s", event.message_id)
                return event
            video_metadata = (
                self._video_xml_metadata(data) if event.message_type == 43 else {}
            )
            image_buffer = data.get("imgBuf")
            image_buffer_data = image_buffer if isinstance(image_buffer, Mapping) else {}
            image_buffer_size = int(self._field(image_buffer_data, "iLen", default=0))
            advertised_size = int(
                self._field(
                    data,
                    "total_len",
                    "totalLen",
                    "dataLen",
                    "videoLength",
                    default=video_metadata.get("length", image_buffer_size),
                )
            )
            if event.message_type == 43 and advertised_size <= 0:
                # A callback may include totalLen=0 while its videomsg XML
                # still carries the real length.  Never let an unknown video
                # size bypass the media cap and reach the native downloader.
                advertised_size = int(video_metadata.get("length", 0) or 0)
                if advertised_size <= 0:
                    logger.warning(
                        "rejected inbound video with unknown size for message %s",
                        event.message_id,
                    )
                    return event
            if advertised_size > self.settings.media_max_bytes:
                logger.warning(
                    "rejected inbound media larger than %s bytes for message %s",
                    self.settings.media_max_bytes,
                    event.message_id,
                )
                return event
            if event.message_type == 3:
                standard_destination = self._image_attempt_path(
                    destination, "standard"
                )
                candidate_source = event.raw if isinstance(event.raw, Mapping) else data
                cdn_candidates = callback_image_candidates_from_payload(candidate_source)
                total_len = int(
                    self._field(
                        data,
                        "total_len",
                        "totalLen",
                        "dataLen",
                        default=image_buffer_size,
                    )
                )
                payload = {
                    "to_user": self._field(data, "to_user", "ToUserName", "toUserName"),
                    "from_user": self._field(data, "from_user", "FromUserName", "fromUserName"),
                    "start_pos": int(self._field(data, "start_pos", "startPos", default=0)),
                    "total_len": total_len,
                    "data_len": int(
                        self._field(
                            data,
                            "data_len",
                            "dataLen",
                            "totalLen",
                            default=total_len,
                        )
                    ),
                    "compress_type": int(self._field(data, "compress_type", "compressType", default=0)),
                    "MsgId": int(self._field(data, "MsgId", "msgId", default=0)),
                    "path": str(standard_destination),
                }
                complete = False
                actual_size = None
                valid = False
                try:
                    await self.hook.download_image(payload)
                    complete, actual_size = await self._wait_for_media_download(
                        standard_destination, total_len
                    )
                    valid = (
                        complete
                        and actual_size is not None
                        and actual_size <= self.settings.media_max_bytes
                        and self._valid_image(standard_destination)
                    )
                except Exception:
                    logger.exception(
                        "standard inbound image download failed for message %s",
                        event.message_id,
                    )
                if valid and self._promote_image(
                    standard_destination, destination, event.message_id
                ):
                    return replace(event, media_path=destination)
                self._discard_media(standard_destination, event.message_id)
                if await self._download_cdn_candidates(
                    destination,
                    cdn_candidates,
                    event.message_id,
                ):
                    return replace(event, media_path=destination)
                logger.warning(
                    "inbound image download failed message=%s expected_size=%s "
                    "actual_size=%s complete=%s cdn_candidates=%s",
                    event.message_id,
                    total_len,
                    actual_size,
                    complete,
                    len(cdn_candidates),
                )
                return event
            elif event.message_type == 47:
                total_len = int(
                    self._field(
                        data,
                        "total_len",
                        "totalLen",
                        "dataLen",
                        default=image_buffer_size,
                    )
                )
                payload = {
                    "to_user": self._field(data, "to_user", "ToUserName", "toUserName"),
                    "from_user": self._field(data, "from_user", "FromUserName", "fromUserName"),
                    "start_pos": int(self._field(data, "start_pos", "startPos", default=0)),
                    "total_len": total_len,
                    "data_len": int(
                        self._field(
                            data,
                            "data_len",
                            "dataLen",
                            "totalLen",
                            default=total_len,
                        )
                    ),
                    "compress_type": int(self._field(data, "compress_type", "compressType", default=0)),
                    "MsgId": int(self._field(data, "MsgId", "msgId", default=0)),
                    "path": str(destination),
                }
                await self.hook.download_image(payload)
            elif event.message_type == 34:
                payload = {
                    "newMsgId": event.message_id,
                    "length": str(self._field(data, "length", "voiceLength", default="")),
                    "MsgId": str(self._field(data, "MsgId", "msgId", default="")),
                    "path": str(destination),
                }
                await self.hook.download_voice(payload)
            elif event.message_type == 43:
                payload = {
                    "total_len": advertised_size,
                    "NewMsgId": int(self._field(data, "NewMsgId", "newMsgId", default=0)),
                    "path": str(destination),
                    "MsgId": int(self._field(data, "MsgId", "msgId", default=0)),
                }
                logger.info(
                    "[VIDEO DEBUG] inbound download request message=%s conversation=%s "
                    "callback_keys=%s video_xml=%s payload=%s",
                    event.message_id,
                    event.conversation_id,
                    sorted(str(key) for key in data),
                    video_metadata,
                    {
                        key: value
                        for key, value in payload.items()
                        if key != "path"
                    }
                    | {"media_name": destination.name},
                )
                self._active_video_downloads += 1
                try:
                    try:
                        response = await self.hook.download_video(payload)
                    except (ClientConnectionError, HookApiError, asyncio.TimeoutError) as exc:
                        # The native 4.1.8.27 endpoint can close its HTTP
                        # listener after accepting a download.  If it has
                        # already created the destination, keep waiting for
                        # that in-flight operation instead of retrying the
                        # request (which can crash the DLL) or discarding the
                        # completed media.
                        if not destination.is_file():
                            await asyncio.sleep(0.2)
                        if not destination.is_file():
                            raise
                        logger.warning(
                            "HOOK video endpoint disconnected after starting message=%s; "
                            "waiting for the native download to finish error=%s",
                            event.message_id,
                            type(exc).__name__,
                        )
                        response = {
                            "status": "download_in_progress_after_disconnect",
                            "error_type": type(exc).__name__,
                        }
                    complete, actual_size = await self._wait_for_media_download(
                        destination,
                        payload["total_len"],
                        timeout_seconds=self.settings.video_download_timeout_seconds,
                    )
                finally:
                    self._active_video_downloads -= 1
                logger.info(
                    "[VIDEO DEBUG] inbound download response message=%s "
                    "response=%s destination_exists=%s destination_size=%s "
                    "expected_size=%s complete=%s",
                    event.message_id,
                    self._hook_response_summary(response),
                    destination.is_file(),
                    actual_size,
                    payload["total_len"],
                    complete,
                )
                if not complete:
                    destination.unlink(missing_ok=True)
                    logger.warning(
                        "HOOK video download did not complete for message %s: "
                        "expected_size=%s actual_size=%s",
                        event.message_id,
                        payload["total_len"],
                        actual_size,
                    )
                    return event
            else:
                payload = {
                    "from_user": self._field(data, "from_user", "fromUserName"),
                    "total_len": str(self._field(data, "total_len", "totalLen", default="")),
                    "MsgId": self._field(data, "MsgId", "msgId"),
                    "path": str(destination),
                    "attachid": str(self._field(data, "attachid", "attachId")),
                    "type": str(self._field(data, "type", "appType", default="6")),
                }
                await self.hook.download_file(payload)
            if destination.is_file() and destination.resolve().is_relative_to(destination_root):
                actual_size = destination.stat().st_size
                if actual_size == 0:
                    destination.unlink(missing_ok=True)
                    logger.warning(
                        "deleted empty inbound media for message %s",
                        event.message_id,
                    )
                    return event
                if actual_size > self.settings.media_max_bytes:
                    destination.unlink(missing_ok=True)
                    logger.warning(
                        "deleted inbound media larger than %s bytes for message %s",
                        self.settings.media_max_bytes,
                        event.message_id,
                    )
                    return event
                return replace(event, media_path=destination)
            logger.warning(
                "HOOK media download returned without creating destination for message %s",
                event.message_id,
            )
        except Exception:
            destination.unlink(missing_ok=True)
            logger.exception("HOOK media download failed for message %s", event.message_id)
        return event

    async def _periodic_media_cleanup(self) -> None:
        while True:
            try:
                removed = self.media.cleanup_expired(
                    self.settings.media_retention_hours * 3600
                )
                if removed:
                    logger.info("removed %s expired media files", removed)
            except Exception:
                logger.exception("media cleanup failed")
            await asyncio.sleep(30 * 60)

    async def _watch_injector(self) -> None:
        while True:
            process = self._injector
            if process is None:
                return
            returncode = process.poll()
            if returncode is not None:
                if returncode == 0:
                    logger.info("injector completed successfully")
                    return
                stderr = b""
                if getattr(process, "stderr", None) is not None:
                    stderr = await asyncio.to_thread(process.stderr.read, 2000)
                detail = stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(
                    f"injector exited while bridge was running: "
                    f"code={returncode}, stderr={detail[:500]}"
                )
            await asyncio.sleep(0.2)

    async def _watch_hook_api(self) -> None:
        consecutive_failures = 0
        while True:
            await asyncio.sleep(self.hook_health_interval_seconds)
            if self._active_video_downloads:
                logger.debug(
                    "HOOK health check deferred while %s inbound video download(s) are active",
                    self._active_video_downloads,
                )
                continue
            if await self._hook_api_accepting_connections():
                consecutive_failures = 0
                self._hook_ready = True
                continue
            consecutive_failures += 1
            if consecutive_failures >= self.hook_health_failure_limit:
                self._hook_ready = False
                raise RuntimeError(
                    "HOOK API became unavailable at "
                    f"{self.settings.hook_api_host}:{self.settings.hook_api_port} "
                    f"for {consecutive_failures} consecutive checks"
                )

    async def _wait_for_stop_or_runtime_failure(self) -> None:
        watchers = {
            task
            for task in (self._injector_watch_task, self._hook_watch_task)
            if task is not None
        }
        if not watchers:
            await self._stop_event.wait()
            return
        stop_wait = asyncio.create_task(self._stop_event.wait())
        try:
            while watchers:
                done, _ = await asyncio.wait(
                    {stop_wait, *watchers}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_wait in done:
                    return
                completed_watchers = done & watchers
                for watcher in completed_watchers:
                    watchers.remove(watcher)
                    await watcher
            await stop_wait
        finally:
            if not stop_wait.done():
                stop_wait.cancel()
                await asyncio.gather(stop_wait, return_exceptions=True)

    async def run(self) -> int:
        configure_logging(self.settings)
        code = 0
        try:
            if self.launch_injector:
                await self._select_dedicated_hook_api_port()
            await self._ensure_components()
            await self._start_callback_server()
            self._ws_task = asyncio.create_task(
                self.onebot.run(), name="astrbot-onebot-client"
            )
            self._consumer_task = asyncio.create_task(
                self._consume_events(), name="hook-event-consumer"
            )
            self._cleanup_task = asyncio.create_task(
                self._periodic_media_cleanup(), name="media-cleanup"
            )
            await asyncio.sleep(0)
            if self.launch_injector:
                if await self._hook_api_accepting_connections():
                    if self.settings.allow_existing_hook_reuse:
                        self._hook_ready = True
                        logger.info(
                            "HOOK API is already accepting connections; "
                            "reusing the existing injected WeChat instance because "
                            "allow_existing_hook_reuse is enabled"
                        )
                    else:
                        raise RuntimeError(
                            "HOOK API is already accepting connections on "
                            f"{self.settings.hook_base_url}; failed to reserve a "
                            "dedicated hook_api_port for the bot WeChat."
                        )
                else:
                    self._injector = self.injector_factory(self.settings)
                    await self._wait_hook_ready()
                    self._hook_ready = True
                    self._injector_watch_task = asyncio.create_task(
                        self._watch_injector(), name="injector-monitor"
                    )
                self._hook_watch_task = asyncio.create_task(
                    self._watch_hook_api(), name="hook-api-monitor"
                )
            if (
                self.group_member_poll_interval_seconds > 0
                and callable(getattr(self.hook, "get_groups", None))
                and callable(getattr(self.hook, "get_group_members", None))
            ):
                self._group_member_watch_task = asyncio.create_task(
                    self._watch_group_member_changes(),
                    name="group-member-monitor",
                )
            await self._wait_for_stop_or_runtime_failure()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("bridge stopped because startup or runtime failed")
            code = 1
        finally:
            self._hook_ready = False
            await self._shutdown()
        return code

    async def stop(self) -> None:
        self._stop_event.set()

    async def _shutdown(self) -> None:
        for task in (
            self._consumer_task,
            self._cleanup_task,
            self._injector_watch_task,
            self._hook_watch_task,
            self._group_member_watch_task,
        ):
            if task is not None:
                task.cancel()
        if self.onebot is not None:
            await self.onebot.close()
        if self._ws_task is not None:
            await asyncio.gather(self._ws_task, return_exceptions=True)
        tasks = [
            task
            for task in (
                self._consumer_task,
                self._cleanup_task,
                self._injector_watch_task,
                self._hook_watch_task,
                self._group_member_watch_task,
            )
            if task is not None
        ]
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self.router is not None:
            await self.router.close()
        await self._stop_callback_server()
        if self._injector is not None and self._injector.poll() is None:
            self._injector.terminate()
            try:
                await asyncio.to_thread(self._injector.wait, 3)
            except subprocess.TimeoutExpired:
                self._injector.kill()
                await asyncio.to_thread(self._injector.wait, 3)
        if self._owns_session and self._session is not None:
            await self._session.close()
            self._session = None
