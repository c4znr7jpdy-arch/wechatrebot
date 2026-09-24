from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Mapping, Sequence
import json
import logging
from pathlib import Path
import re
import time
from typing import Any
import uuid

import websockets
from websockets.asyncio.client import ClientConnection

from .hook_client import HookApiError
from .media import MediaError, MediaStore
from .message_codec import UnsupportedSegmentError, decode_outbound
from .models import OutboundCommand, RecallRecord, ReplyTarget, SendResult
from .state import BridgeState


logger = logging.getLogger(__name__)


def _safe_hook_diagnostics(error: HookApiError) -> str:
    parts: list[str] = []
    for key in (
        "http_status",
        "content_type",
        "body_length",
        "parse_error",
        "json_type",
        "api_code",
        "data_ret_code",
        "root_keys",
        "data_keys",
        "data_field_shapes",
        "video_exists",
        "video_length",
        "thumb_exists",
        "thumb_length",
        "cgi_request_count",
        "cgi_response_count",
        "cgi_video_hits",
    ):
        if key not in error.diagnostics:
            continue
        value = error.diagnostics[key]
        if key in {"root_keys", "data_keys", "data_field_shapes", "cgi_video_hits"}:
            value = list(value) if isinstance(value, (list, tuple)) else []
        parts.append(f"{key}={value}")
    return " ".join(parts)


def _video_response_value(raw: Mapping[str, Any], *keys: str) -> Any:
    data = raw.get("data")
    nested = data if isinstance(data, Mapping) else {}
    for key in keys:
        if key in nested:
            return nested[key]
        if key in raw:
            return raw[key]
    return None


def _safe_integer(value: Any) -> int | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"true", "1"}:
            return True
        if normalized in {"false", "0"}:
            return False
    return None


def _secret_scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        value = value.get("String", value.get("string", ""))
    return "" if value is None else str(value)


def _safe_secret_shape(value: str) -> str:
    return (
        f"present={bool(value)},length={len(value)},"
        f"is_hex={bool(re.fullmatch(r'[0-9a-fA-F]+', value))}"
    )


def _safe_video_forward_diagnostics(
    raw: Mapping[str, Any],
    *,
    url_file_key: str,
    video_length: int,
    thumbnail_length: int,
) -> str:
    action_flag = _safe_integer(_video_response_value(raw, "actionFlag"))
    video_need_reupload = _safe_boolean(
        _video_response_value(raw, "videoNeedReupload")
    )
    raw_video_need_reupload = _safe_boolean(
        _video_response_value(raw, "rawVideoNeedReupload")
    )
    video_start_pos = _safe_integer(_video_response_value(raw, "videoStartPos"))
    thumb_start_pos = _safe_integer(_video_response_value(raw, "thumbStartPos"))
    aeskey = _secret_scalar(_video_response_value(raw, "aeskey", "aesKey"))
    raw_aeskey = _secret_scalar(
        _video_response_value(raw, "rawAeskey", "rawAesKey")
    )
    return " ".join(
        (
            f"action_flag={action_flag}",
            f"video_need_reupload={video_need_reupload}",
            f"raw_video_need_reupload={raw_video_need_reupload}",
            f"video_start_pos={video_start_pos}",
            f"video_length_matches={video_start_pos == video_length}",
            f"thumb_start_pos={thumb_start_pos}",
            f"thumb_length_matches={thumb_start_pos == thumbnail_length}",
            f"aeskey_shape={_safe_secret_shape(aeskey)}",
            f"raw_aeskey_shape={_safe_secret_shape(raw_aeskey)}",
            f"aeskey_matches_filekey={bool(aeskey and url_file_key and aeskey == url_file_key)}",
            f"raw_aeskey_matches_filekey={bool(raw_aeskey and url_file_key and raw_aeskey == url_file_key)}",
        )
    )


def _video_forward_confirmed(
    raw: Mapping[str, Any],
    *,
    video_length: int,
    thumbnail_length: int,
) -> bool:
    return (
        _safe_integer(_video_response_value(raw, "actionFlag")) == 0
        and _safe_boolean(_video_response_value(raw, "videoNeedReupload")) is False
        and _safe_boolean(_video_response_value(raw, "rawVideoNeedReupload"))
        is False
    )


def _value(item: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, Mapping):
            value = value.get("String", "")
        if value not in (None, ""):
            return str(value)
    return ""


def _normalize_group_id(value: Any) -> str:
    group_id = str(value or "")
    if not group_id or group_id.endswith("@chatroom"):
        return group_id
    return f"{group_id}@chatroom"


def _outbound_reply_message_id(message: Any) -> str | None:
    """Return AstrBot's explicit Reply target, if the result contains one."""
    if isinstance(message, (str, bytes)) or not isinstance(message, Sequence):
        return None
    for segment in message:
        if not isinstance(segment, Mapping) or str(segment.get("type", "")) != "reply":
            continue
        data = segment.get("data")
        if not isinstance(data, Mapping):
            continue
        message_id = data.get("id")
        if message_id not in (None, ""):
            return str(message_id)
    return None


def _expand_forward_nodes(raw: Any) -> list[list[Mapping[str, Any]]]:
    if isinstance(raw, (str, bytes)) or not isinstance(raw, list):
        raise UnsupportedSegmentError("forward messages must be a node list")

    expanded: list[list[Mapping[str, Any]]] = []
    for node in raw:
        if not isinstance(node, Mapping):
            raise UnsupportedSegmentError("forward node must be an object")
        data = node.get("data", node)
        if not isinstance(data, Mapping):
            raise UnsupportedSegmentError("forward node data must be an object")
        name = str(data.get("name") or data.get("nickname") or "").strip()
        content = data.get("content", "")
        prefix = f"{name}:\n" if name else ""
        if isinstance(content, str):
            expanded.append(
                [{"type": "text", "data": {"text": prefix + content}}]
            )
            continue
        if isinstance(content, list):
            node_segments: list[Mapping[str, Any]] = []
            if prefix:
                node_segments.append({"type": "text", "data": {"text": prefix}})
            node_segments.extend(
                item for item in content if isinstance(item, Mapping)
            )
            expanded.append(node_segments)
            continue
        raise UnsupportedSegmentError("forward node content is unsupported")
    return expanded


class OneBotActionRouter:
    def __init__(
        self,
        hook,
        media: MediaStore,
        state: BridgeState,
        *,
        video_send_mode: str = "native",
        history: Any = None,
    ) -> None:
        self.hook = hook
        self.media = media
        self.state = state
        self.history = history
        if video_send_mode not in {"file", "native"}:
            raise ValueError("video_send_mode must be file or native")
        self.video_send_mode = video_send_mode
        self._scheduled: set[asyncio.Task[None]] = set()
        self._pending_image_receipts: dict[
            str, deque[asyncio.Future[RecallRecord]]
        ] = {}

    @staticmethod
    def _ok(data: Any, echo: Any) -> dict[str, Any]:
        return {
            "status": "ok",
            "retcode": 0,
            "data": data,
            "message": "",
            "echo": echo,
        }

    @staticmethod
    def _failed(message: str, echo: Any, retcode: int = 10002) -> dict[str, Any]:
        return {
            "status": "failed",
            "retcode": retcode,
            "data": None,
            "message": message,
            "echo": echo,
        }

    @staticmethod
    def _params(request: Mapping[str, Any]) -> Mapping[str, Any]:
        params = request.get("params", request.get("data", {}))
        return params if isinstance(params, Mapping) else {}

    @staticmethod
    def _target(action: str, params: Mapping[str, Any]) -> tuple[str, bool]:
        if action in {"send_group_msg", "send_group_forward_msg"}:
            return _normalize_group_id(params.get("group_id")), True
        if action in {"send_private_msg", "send_private_forward_msg"}:
            return str(params.get("user_id", "")), False
        group_id = str(params.get("group_id", ""))
        if group_id:
            return _normalize_group_id(group_id), True
        target = str(
            params.get("user_id")
            or params.get("to_wxid")
            or params.get("detail_type") == "group" and params.get("group_id")
            or ""
        )
        return target, target.endswith("@chatroom")

    async def _execute_command(
        self,
        command: OutboundCommand,
        reply_target: ReplyTarget | None = None,
    ) -> SendResult:
        if command.kind == "text":
            send_reply_text = getattr(self.hook, "send_reply_text", None)
            if reply_target is not None and callable(send_reply_text):
                try:
                    result = await send_reply_text(
                        command.target, command.text, reply_target
                    )
                    if result.ok:
                        return result
                    logger.warning(
                        "native WeChat reply rejected; falling back to plain text: %s",
                        result.error or "HOOK rejected request",
                    )
                except Exception:
                    logger.exception(
                        "native WeChat reply failed; falling back to plain text"
                    )
            return await self.hook.send_text(command.target, command.text)
        if command.kind == "at_text":
            return await self.hook.send_at_text(
                command.target, command.text, command.mentions
            )
        if command.kind == "app_xml":
            return await self.hook.send_app_xml(
                command.target, command.text, command.app_type
            )
        if command.kind == "link_card":
            return await self.hook.send_link_card(
                command.target,
                command.title,
                command.description,
                command.thumb_url,
                command.url,
            )
        if command.kind == "video":
            if self.video_send_mode == "file":
                path = await self.media.materialize(command.source, ".mp4")
                logger.info(
                    "[VIDEO SEND] mode=file target=%s media_name=%s",
                    command.target,
                    path.name,
                )
                return await self.hook.send_file(command.target, path)

            video_trace = uuid.uuid4().hex[:12]
            stage = "materialize"
            logger.info(
                "[VIDEO SEND] trace=%s stage=%s target=%s",
                video_trace,
                stage,
                command.target,
            )
            try:
                path = await self.media.materialize(command.source, ".mp4")
                stage = "prepare"
                prepared = await self.media.prepare_video(path)
                logger.info(
                    "[VIDEO SEND] trace=%s stage=%s target=%s media_name=%s "
                    "video_length=%s thumb_length=%s play_length=%s",
                    video_trace,
                    stage,
                    command.target,
                    prepared.path.name,
                    prepared.video_length,
                    prepared.thumbnail_length,
                    prepared.play_length,
                )
                stage = "upload"
                upload = await self.hook.upload_video(
                    prepared.path, prepared.thumbnail_path
                )
                logger.info(
                    "[VIDEO SEND] trace=%s stage=%s target=%s complete=True "
                    "response_keys=%s data_keys=%s credential_shapes=%s",
                    video_trace,
                    stage,
                    command.target,
                    list(upload.response_keys),
                    list(upload.data_keys),
                    list(upload.credential_shapes),
                )
                stage = "forward"
                result = await self.hook.forward_video(
                    command.target,
                    upload,
                    video_length=prepared.video_length,
                    thumbnail_length=prepared.thumbnail_length,
                    play_length=prepared.play_length,
                )
                logger.info(
                    "[VIDEO SEND] trace=%s stage=%s target=%s ok=%s "
                    "response_keys=%s %s",
                    video_trace,
                    stage,
                    command.target,
                    result.ok,
                    sorted(str(key) for key in result.raw),
                    _safe_video_forward_diagnostics(
                        result.raw,
                        url_file_key=upload.url_file_key,
                        video_length=prepared.video_length,
                        thumbnail_length=prepared.thumbnail_length,
                    ),
                )
                if result.ok and not _video_forward_confirmed(
                    result.raw,
                    video_length=prepared.video_length,
                    thumbnail_length=prepared.thumbnail_length,
                ):
                    return SendResult(
                        ok=False,
                        error="HOOK video forward did not confirm delivery",
                        raw=result.raw,
                    )
                return result
            except BaseException as exc:
                diagnostics = (
                    _safe_hook_diagnostics(exc)
                    if isinstance(exc, HookApiError)
                    else ""
                )
                if isinstance(exc, asyncio.TimeoutError) and stage == "upload":
                    upload_timeout = getattr(
                        self.hook, "video_upload_timeout_seconds", None
                    )
                    if upload_timeout is not None:
                        diagnostics = (
                            f"timeout_seconds={upload_timeout:g}"
                            + (f" {diagnostics}" if diagnostics else "")
                        )
                logger.warning(
                    "[VIDEO SEND] trace=%s stage=%s target=%s failed=%s%s",
                    video_trace,
                    stage,
                    command.target,
                    type(exc).__name__,
                    f" {diagnostics}" if diagnostics else "",
                )
                # A native C2C video upload can fail after the media has been
                # downloaded and prepared (for example when the current
                # WeChat account has no usable CDN credentials).  Keep the
                # actual MP4 deliverable by falling back to the ordinary file
                # interface.  Do not do this after the forward stage: a
                # forward failure can be ambiguous and a second delivery
                # could duplicate a video that WeChat already accepted.
                if stage == "upload" and isinstance(exc, Exception):
                    prepared_path = locals().get("prepared")
                    prepared_file = getattr(prepared_path, "path", None)
                    if isinstance(prepared_file, Path):
                        try:
                            file_result = await self.hook.send_file(
                                command.target, prepared_file
                            )
                        except Exception:
                            logger.exception(
                                "[VIDEO SEND] trace=%s file fallback failed "
                                "target=%s",
                                video_trace,
                                command.target,
                            )
                        else:
                            if file_result.ok:
                                logger.warning(
                                    "[VIDEO SEND] trace=%s fallback=file "
                                    "target=%s media_name=%s",
                                    video_trace,
                                    command.target,
                                    prepared_file.name,
                                )
                                return file_result
                raise
        path = await self.media.materialize(command.source)
        if command.kind == "image":
            return await self.hook.send_image(command.target, path)
        if command.kind == "file":
            return await self.hook.send_file(command.target, path)
        if command.kind == "emotion":
            emotion_path = await self.media.prepare_emotion(path)
            return await self.hook.send_emotion(command.target, emotion_path)
        if command.kind == "voice":
            prepare_voice = getattr(self.media, "prepare_voice", None)
            voice_path = (
                await prepare_voice(path)
                if callable(prepare_voice)
                else path
            )
            try:
                if voice_path.suffix.lower() in {".silk", ".slik"}:
                    return await self.hook.send_silk_voice(command.target, voice_path)
                return await self.hook.send_mp3_voice(command.target, voice_path)
            finally:
                if voice_path != path:
                    voice_path.unlink(missing_ok=True)
        raise UnsupportedSegmentError(f"unsupported command kind: {command.kind}")

    def _new_image_receipt_waiter(
        self, target: str
    ) -> asyncio.Future[RecallRecord]:
        waiter = asyncio.get_running_loop().create_future()
        self._pending_image_receipts.setdefault(target, deque()).append(waiter)
        return waiter

    def _discard_image_receipt_waiter(
        self, target: str, waiter: asyncio.Future[RecallRecord]
    ) -> None:
        waiters = self._pending_image_receipts.get(target)
        if waiters is None:
            return
        try:
            waiters.remove(waiter)
        except ValueError:
            pass
        if not waiters:
            self._pending_image_receipts.pop(target, None)

    def _cancel_image_receipt_waiter(
        self, target: str, waiter: asyncio.Future[RecallRecord]
    ) -> None:
        if not waiter.done():
            waiter.cancel()
        self._discard_image_receipt_waiter(target, waiter)

    def observe_outgoing_image(self, record: RecallRecord) -> bool:
        waiters = self._pending_image_receipts.get(record.to_user_name)
        if waiters is None:
            return False
        waiter = next((item for item in waiters if not item.done()), None)
        if waiter is None:
            return False
        waiter.set_result(record)
        return True

    async def _recall_when_image_receipt_arrives(
        self,
        target: str,
        waiter: asyncio.Future[RecallRecord],
        onebot_id: int,
        deadline: float,
    ) -> None:
        try:
            record = await asyncio.wait_for(asyncio.shield(waiter), timeout=60.0)
            self.state.remember_recall(onebot_id, record)
            remaining = max(0.0, deadline - asyncio.get_running_loop().time())
            logger.info(
                "scheduled image recall bound target=%s message_id=%s remaining=%.1fs",
                target,
                record.message_id,
                remaining,
            )
            await self._delayed_recall(record, remaining)
        except asyncio.TimeoutError:
            logger.warning(
                "outgoing image receipt did not arrive within 60s for %s", target
            )
        finally:
            if not waiter.done():
                waiter.cancel()
            self._discard_image_receipt_waiter(target, waiter)

    async def _send_message(
        self, action: str, params: Mapping[str, Any], echo: Any
    ) -> dict[str, Any]:
        target, is_group = self._target(action, params)
        if not target:
            return self._failed("message target is required", echo, 10003)
        message = params.get("message", params.get("content", ""))
        try:
            commands = decode_outbound(message, target, is_group)
        except UnsupportedSegmentError as exc:
            return self._failed(str(exc), echo, 10003)
        if not commands:
            return self._failed("message is empty", echo, 10003)

        reply_target: ReplyTarget | None = None
        if commands[0].kind == "text":
            explicit_reply_id = _outbound_reply_message_id(message)
            if explicit_reply_id is not None:
                # AstrBot's result decorator supplies the exact inbound
                # OneBot message id.  Never fall back to a different message
                # when that explicit id is unknown.
                reply_target = self.state.take_reply_target_by_message_id(
                    explicit_reply_id
                )
            else:
                reply_target = self.state.take_reply_target(target)

        auto_recall_delay: float | None = None
        if "auto_recall_delay" in params:
            try:
                auto_recall_delay = float(params["auto_recall_delay"])
            except (TypeError, ValueError):
                return self._failed("auto_recall_delay must be a number", echo, 10003)

        completed = 0
        latest_receipt: SendResult | None = None
        pending_auto_recalls: list[
            tuple[str, asyncio.Future[RecallRecord], float]
        ] = []
        for index, command in enumerate(commands, start=1):
            pending_waiter: asyncio.Future[RecallRecord] | None = None
            pending_deadline = 0.0
            if auto_recall_delay is not None and command.kind == "image":
                pending_waiter = self._new_image_receipt_waiter(command.target)
                logger.info(
                    "waiting for outgoing image receipt target=%s auto_recall_delay=%.1fs",
                    command.target,
                    auto_recall_delay,
                )
            try:
                result = await self._execute_command(
                    command,
                    reply_target if index == 1 else None,
                )
            except (HookApiError, MediaError, OSError, asyncio.TimeoutError) as exc:
                if pending_waiter is not None:
                    self._cancel_image_receipt_waiter(command.target, pending_waiter)
                for pending_target, waiter, _ in pending_auto_recalls:
                    self._cancel_image_receipt_waiter(pending_target, waiter)
                return self._failed(
                    f"segment {index} failed after {completed} completed: {exc}", echo
                )
            if not result.ok:
                if pending_waiter is not None:
                    self._cancel_image_receipt_waiter(command.target, pending_waiter)
                for pending_target, waiter, _ in pending_auto_recalls:
                    self._cancel_image_receipt_waiter(pending_target, waiter)
                detail = result.error or "HOOK rejected request"
                return self._failed(
                    f"segment {index} failed after {completed} completed: {detail}",
                    echo,
                )
            completed += 1
            if (
                result.message_id
                and result.create_time is not None
                and result.to_user_name
            ):
                latest_receipt = result
                if pending_waiter is not None and not pending_waiter.done():
                    pending_waiter.set_result(
                        RecallRecord(
                            result.message_id,
                            result.create_time,
                            result.to_user_name,
                        )
                    )
            if pending_waiter is not None:
                pending_deadline = (
                    asyncio.get_running_loop().time()
                    + max(0.0, auto_recall_delay or 0.0)
                )
                pending_auto_recalls.append(
                    (command.target, pending_waiter, pending_deadline)
                )

        onebot_id = self.state.next_message_id()
        if latest_receipt is not None:
            recall_record = RecallRecord(
                latest_receipt.message_id or "",
                latest_receipt.create_time or 0,
                latest_receipt.to_user_name or target,
            )
            self.state.remember_recall(onebot_id, recall_record)
        for pending_target, waiter, deadline in pending_auto_recalls:
            task = asyncio.create_task(
                self._recall_when_image_receipt_arrives(
                    pending_target,
                    waiter,
                    onebot_id,
                    deadline,
                )
            )
            self._scheduled.add(task)
            task.add_done_callback(self._scheduled.discard)
        recorder = getattr(self.history, "record_outbound", None)
        if callable(recorder):
            try:
                await asyncio.to_thread(
                    recorder,
                    target,
                    is_group,
                    commands,
                    message_id=(latest_receipt.message_id if latest_receipt else "") or "",
                    create_time=latest_receipt.create_time if latest_receipt else None,
                    sender_id=self.state.bot_wxid,
                    sender_name=self.state.bot_nickname,
                )
            except Exception:
                logger.exception(
                    "failed to persist outbound message history target=%s", target
                )
        return self._ok({"message_id": onebot_id}, echo)

    async def _recall_record(
        self, record: RecallRecord | None, echo: Any
    ) -> dict[str, Any]:
        if record is None:
            return self._failed("recall record is unavailable", echo, 10004)
        result = await self.hook.recall(
            record.message_id, record.create_time, record.to_user_name
        )
        if not result.ok:
            return self._failed(result.error or "HOOK recall failed", echo)
        return self._ok(None, echo)

    async def _delayed_recall(self, record: RecallRecord, delay: float) -> None:
        await asyncio.sleep(max(0.0, delay))
        try:
            result = await self.hook.recall(
                record.message_id, record.create_time, record.to_user_name
            )
            if result.ok:
                logger.info(
                    "scheduled recall completed target=%s message_id=%s",
                    record.to_user_name,
                    record.message_id,
                )
            else:
                logger.warning(
                    "scheduled recall rejected target=%s message_id=%s error=%s",
                    record.to_user_name,
                    record.message_id,
                    result.error or "HOOK rejected request",
                )
        except Exception:
            logger.exception("scheduled recall failed for %s", record.to_user_name)

    async def handle(self, request: Mapping[str, Any]) -> dict[str, Any]:
        action = str(request.get("action", ""))
        echo = request.get("echo")
        params = self._params(request)

        if action in {"send_group_forward_msg", "send_private_forward_msg"}:
            target, is_group = self._target(action, params)
            if not target:
                return self._failed("message target is required", echo, 10003)
            try:
                nodes = _expand_forward_nodes(
                    params.get("messages", params.get("message", []))
                )
            except UnsupportedSegmentError as exc:
                return self._failed(str(exc), echo, 10003)
            if not nodes:
                return self._failed("forward messages are empty", echo, 10003)

            response: dict[str, Any] | None = None
            target_key = "group_id" if is_group else "user_id"
            send_action = "send_group_msg" if is_group else "send_private_msg"
            for index, node_message in enumerate(nodes, start=1):
                response = await self._send_message(
                    send_action,
                    {target_key: target, "message": node_message},
                    echo,
                )
                if response["status"] != "ok":
                    response["message"] = (
                        f"forward node {index} failed after {index - 1} completed: "
                        f"{response['message']}"
                    )
                    return response
            assert response is not None
            return response

        if action in {
            "send_message",
            "send_msg",
            "send_private_msg",
            "send_group_msg",
        }:
            return await self._send_message(action, params, echo)
        if action == "send_voice":
            target, _ = self._target(action, params)
            source = str(
                params.get("file")
                or params.get("file_id")
                or params.get("path")
                or params.get("silk_file")
                or params.get("slik_file")
                or ""
            )
            return await self._send_message(
                action,
                {
                    "user_id": target,
                    "message": [{"type": "record", "data": {"file": source}}],
                },
                echo,
            )
        if action == "send_raw_xml":
            target, _ = self._target(action, params)
            if not target or not params.get("content"):
                return self._failed("to_wxid and content are required", echo, 10003)
            content = str(params["content"])
            explicit_type = params.get("type")
            if explicit_type is not None:
                app_type = int(explicit_type)
            else:
                type_match = re.search(r"<type>\s*(\d+)\s*</type>", content)
                app_type = int(type_match.group(1)) if type_match else 3
            result = await self.hook.send_app_xml(
                target, content, app_type
            )
            return self._ok(None, echo) if result.ok else self._failed(
                result.error or "HOOK rejected XML", echo
            )
        if action == "upload_group_file":
            return await self._send_message(
                "send_group_msg",
                {
                    "group_id": _normalize_group_id(params.get("group_id")),
                    "message": [
                        {
                            "type": "file",
                            "data": {
                                "file": params.get("file") or params.get("url") or ""
                            },
                        }
                    ],
                },
                echo,
            )
        if action == "upload_file":
            source = str(params.get("file") or params.get("url") or "")
            try:
                path = await self.media.materialize(source)
            except (MediaError, OSError) as exc:
                return self._failed(str(exc), echo, 10003)
            return self._ok({"file_id": str(path)}, echo)
        if action == "get_login_info":
            return self._ok(
                {
                    "user_id": self.state.bot_wxid,
                    "nickname": self.state.bot_nickname,
                },
                echo,
            )
        if action == "get_self_info":
            return self._ok(
                {
                    "user_id": self.state.bot_wxid,
                    "nickname": self.state.bot_nickname,
                    "user_displayname": self.state.bot_nickname,
                },
                echo,
            )
        if action == "get_status":
            return self._ok(
                {"good": bool(self.state.bot_wxid), "online": bool(self.state.bot_wxid)},
                echo,
            )
        if action == "get_version_info":
            return self._ok(
                {
                    "app_name": "hook-41827-astrbot-bridge",
                    "app_version": "0.1.0",
                    "protocol_version": "v11",
                },
                echo,
            )
        if action == "get_version":
            return self._ok(
                {"impl": "hook-41827-astrbot-bridge", "version": "0.1.0", "onebot_version": "11"},
                echo,
            )
        if action in {"can_send_image", "can_send_record"}:
            return self._ok({"yes": True}, echo)
        if action == "get_msg":
            try:
                message_id = int(params.get("message_id", 0))
            except (TypeError, ValueError):
                return self._failed("message_id must be an integer", echo, 10003)
            message = self.state.get_message(message_id)
            if message is None:
                return self._failed("message not found", echo, 10004)
            return self._ok(message, echo)
        if action == "get_group_msg_history":
            room_id = _normalize_group_id(params.get("group_id"))
            try:
                count = int(params.get("count", 100))
            except (TypeError, ValueError):
                return self._failed("count must be an integer", echo, 10003)
            return self._ok(
                {"messages": self.state.group_history(room_id, count)},
                echo,
            )
        if action == "get_friend_list":
            friends = await self.hook.get_friends()
            data = []
            for item in friends:
                contact = item.get("contact")
                if not isinstance(contact, Mapping):
                    contact = item
                data.append(
                    {
                        "user_id": _value(
                            contact, "wxid", "userName", "username", "user_id"
                        ),
                        "nickname": _value(
                            contact, "nickname", "nickName", "nick_name"
                        ),
                        "remark": _value(contact, "remark", "remarkName"),
                    }
                )
            return self._ok(data, echo)
        if action == "get_group_list":
            groups = await self.hook.get_groups()
            data = [
                {
                    "group_id": _normalize_group_id(
                        _value(item, "wxid", "userName", "username", "group_id")
                    ),
                    "group_name": _value(
                        item, "nickname", "nickName", "nick_name", "group_name"
                    ),
                }
                for item in groups
            ]
            return self._ok(data, echo)
        if action == "get_group_info":
            room_id = _normalize_group_id(params.get("group_id"))
            groups = await self.hook.get_groups()
            match = next(
                (
                    item
                    for item in groups
                    if _normalize_group_id(
                        _value(item, "wxid", "userName", "username", "group_id")
                    )
                    == room_id
                ),
                None,
            )
            if match is None:
                return self._failed("group not found", echo, 10004)
            raw_count = _value(
                match, "memberCount", "member_count", "memberNum", "member_num"
            )
            try:
                member_count = int(raw_count or 0)
            except ValueError:
                member_count = 0
            return self._ok(
                {
                    "group_id": room_id,
                    "group_name": _value(
                        match, "nickname", "nickName", "nick_name", "group_name"
                    ),
                    "member_count": member_count,
                    "max_member_count": 0,
                },
                echo,
            )
        if action in {"get_stranger_info", "get_user_info"}:
            user_id = str(params.get("user_id", ""))
            item = await self.hook.get_contact(user_id)
            return self._ok(
                {
                    "user_id": user_id,
                    "nickname": _value(item, "nickname", "nickName") or user_id,
                    "sex": "unknown",
                },
                echo,
            )
        if action in {"get_group_member_info", "get_group_member_list"}:
            room_id = _normalize_group_id(
                params.get("group_id", params.get("room_id", ""))
            )
            members = await self.hook.get_group_members(room_id)
            shaped = [
                {
                    "group_id": room_id,
                    "user_id": _value(item, "wxid", "userName", "user_id"),
                    "nickname": _value(item, "nickname", "nickName"),
                    "card": _value(item, "displayName", "card"),
                    "sex": "unknown",
                    "age": 0,
                    "role": "member",
                }
                for item in members
            ]
            if action == "get_group_member_list":
                return self._ok(shaped, echo)
            user_id = str(params.get("user_id", ""))
            match = next((item for item in shaped if item["user_id"] == user_id), None)
            if match is None:
                return self._failed("group member not found", echo, 10004)
            return self._ok(match, echo)
        if action == "get_group_member_cache":
            room_id = _normalize_group_id(
                params.get("group_id", params.get("room_id", ""))
            )
            members = await self.hook.get_group_members(room_id)
            shaped: dict[str, dict[str, Any]] = {}
            for item in members:
                user_id = _value(item, "wxid", "userName", "user_id")
                if not user_id:
                    continue
                nickname = _value(item, "nickname", "nickName", "user_name")
                display_name = _value(
                    item, "displayName", "display_name", "card", "remark"
                )
                shaped[user_id] = {
                    "user_id": user_id,
                    "wxid": user_id,
                    "nickname": nickname,
                    "display_name": display_name,
                    "card": display_name,
                    "avatar": _value(
                        item,
                        "avatar",
                        "smallHeadImgUrl",
                        "small_head_img_url",
                    ),
                }
            return self._ok(
                {"room_wxid": room_id, "members": shaped},
                echo,
            )
        if action == "delete_msg":
            try:
                message_id = int(params.get("message_id", 0))
            except (TypeError, ValueError):
                return self._failed("message_id must be an integer", echo, 10003)
            return await self._recall_record(self.state.get_recall(message_id), echo)
        if action == "recall_last_msg":
            target, _ = self._target(action, params)
            return await self._recall_record(self.state.latest_recall(target), echo)
        if action == "auto_recall_last_msg":
            target, _ = self._target(action, params)
            record = self.state.latest_recall(target)
            if record is None:
                return self._failed("recall record is unavailable", echo, 10004)
            try:
                delay = float(params.get("delay", 20))
            except (TypeError, ValueError):
                return self._failed("delay must be a number", echo, 10003)
            task = asyncio.create_task(self._delayed_recall(record, delay))
            self._scheduled.add(task)
            task.add_done_callback(self._scheduled.discard)
            return self._ok(None, echo)
        return self._failed(f"unsupported action: {action}", echo, 1404)

    async def close(self) -> None:
        for task in list(self._scheduled):
            task.cancel()
        if self._scheduled:
            await asyncio.gather(*self._scheduled, return_exceptions=True)
        self._scheduled.clear()


class OneBotClient:
    def __init__(
        self,
        url: str,
        router: OneBotActionRouter,
        *,
        reconnect_min: float = 3.0,
        reconnect_max: float = 3.0,
        heartbeat_interval: float = 30.0,
        event_queue_size: int = 256,
    ) -> None:
        self.url = url
        self.router = router
        self.reconnect_min = reconnect_min
        self.reconnect_max = reconnect_max
        self.heartbeat_interval = heartbeat_interval
        self._events: asyncio.Queue[dict[str, Any]] = asyncio.Queue(
            maxsize=event_queue_size
        )
        self._connection: ClientConnection | None = None
        self._connected_identity = ""
        self._connected_at = 0.0
        self._last_heartbeat_at = 0.0
        self._self_id = ""
        self._nickname = ""
        self._closed = False
        self._closed_event = asyncio.Event()
        self._run_task: asyncio.Task[None] | None = None
        self._send_lock = asyncio.Lock()
        self._retry_event: dict[str, Any] | None = None

    async def _send_json(self, payload: Mapping[str, Any]) -> None:
        connection = self._connection
        if connection is None:
            raise ConnectionError("AstrBot WebSocket is disconnected")
        async with self._send_lock:
            await connection.send(json.dumps(dict(payload), ensure_ascii=False))

    async def _receive_loop(self, connection: ClientConnection) -> None:
        async for raw in connection:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            try:
                request = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning("ignored invalid JSON from AstrBot")
                continue
            if not isinstance(request, Mapping) or not request.get("action"):
                continue
            try:
                response = await self.router.handle(request)
            except Exception as exc:
                logger.exception("OneBot action failed: %s", request.get("action"))
                response = {
                    "status": "failed",
                    "retcode": 1500,
                    "data": None,
                    "message": str(exc),
                    "echo": request.get("echo"),
                }
            await self._send_json(response)

    async def _sender_loop(self) -> None:
        while not self._closed:
            event = self._retry_event
            if event is None:
                event = await self._events.get()
            try:
                await self._send_json(event)
            except BaseException:
                self._retry_event = event
                raise
            else:
                self._retry_event = None
                self._events.task_done()

    async def _heartbeat_loop(self) -> None:
        while not self._closed:
            await asyncio.sleep(self.heartbeat_interval)
            if self._self_id and self._connection is not None:
                await self._send_json(
                    {
                        "time": int(time.time()),
                        "self_id": self._self_id,
                        "post_type": "meta_event",
                        "meta_event_type": "heartbeat",
                        "status": {"good": True, "online": True},
                        "interval": int(self.heartbeat_interval * 1000),
                    }
                )
                self._last_heartbeat_at = time.time()

    async def _run_connection(self, connection: ClientConnection) -> None:
        if self._self_id:
            await self._send_json(
                {
                    "time": int(time.time()),
                    "self_id": self._self_id,
                    "post_type": "meta_event",
                    "meta_event_type": "lifecycle",
                    "sub_type": "connect",
                }
            )
        tasks = {
            asyncio.create_task(self._receive_loop(connection)),
            asyncio.create_task(self._sender_loop()),
            asyncio.create_task(self._heartbeat_loop()),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception

    async def run(self) -> None:
        self._run_task = asyncio.current_task()
        delay = self.reconnect_min
        try:
            while not self._closed:
                try:
                    handshake_identity = self._self_id or "wechat_bot"
                    headers = {
                        "X-Self-ID": handshake_identity,
                        "X-Client-Role": "Universal",
                    }
                    async with websockets.connect(
                        self.url,
                        additional_headers=headers,
                        ping_interval=None,
                        max_size=None,
                    ) as connection:
                        self._connection = connection
                        self._connected_identity = handshake_identity
                        self._connected_at = time.time()
                        delay = self.reconnect_min
                        if self._self_id and self._self_id != handshake_identity:
                            await connection.close(code=1000, reason="identity updated")
                            continue
                        logger.info("AstrBot WebSocket connected successfully: %s", self.url)
                        await self._run_connection(connection)
                except asyncio.CancelledError:
                    if self._closed:
                        break
                    raise
                except (OSError, ConnectionError, websockets.WebSocketException) as exc:
                    if not self._closed:
                        logger.warning(
                            "AstrBot WebSocket disconnected: %s; retrying in %.1fs "
                            "(retrying indefinitely)",
                            exc,
                            delay,
                        )
                finally:
                    self._connection = None
                    self._connected_identity = ""
                    self._connected_at = 0.0
                if self._closed:
                    break
                try:
                    await asyncio.wait_for(self._closed_event.wait(), timeout=delay)
                except TimeoutError:
                    pass
                delay = min(self.reconnect_max, max(self.reconnect_min, delay * 2))
        finally:
            self._run_task = None

    async def set_identity(self, wxid: str, nickname: str = "") -> None:
        changed = wxid != self._self_id
        self._self_id = wxid
        self._nickname = nickname
        state = getattr(self.router, "state", None)
        if state is not None:
            state.set_identity(wxid, nickname)
        if changed and self._connection is not None and self._connected_identity != wxid:
            await self._connection.close(code=1000, reason="identity updated")

    async def send_event(self, event: Mapping[str, Any]) -> None:
        await self._events.put(dict(event))

    @property
    def connected(self) -> bool:
        return self._connection is not None

    @property
    def connected_identity(self) -> str:
        return self._connected_identity

    @property
    def last_heartbeat_at(self) -> float:
        return self._last_heartbeat_at

    @property
    def heartbeat_fresh(self) -> bool:
        if self._connection is None:
            return False
        now = time.time()
        grace = max(5.0, self.heartbeat_interval * 2.5)
        baseline = self._last_heartbeat_at or self._connected_at
        return bool(baseline and now - baseline <= grace)

    async def close(self) -> None:
        self._closed = True
        self._closed_event.set()
        if self._connection is not None:
            await self._connection.close(code=1000, reason="bridge stopping")
        elif self._run_task is not None and self._run_task is not asyncio.current_task():
            self._run_task.cancel()
