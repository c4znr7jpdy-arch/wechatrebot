from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import re
from typing import Any, Mapping, Sequence
from urllib.parse import parse_qsl, urlsplit
from xml.sax.saxutils import escape as xml_escape

from aiohttp import ClientConnectionError, ClientResponse, ClientSession, ClientTimeout

from .cgi_offsets import cgi_offset_summary
from .models import SendResult, VideoUpload


class HookApiError(RuntimeError):
    def __init__(
        self,
        path: str,
        status: int | None,
        detail: Any,
        *,
        diagnostics: Mapping[str, Any] | None = None,
    ) -> None:
        self.path = path
        self.status = status
        self.detail = detail
        self.diagnostics = dict(diagnostics or {})
        label = f"HTTP {status}" if status is not None else "invalid response"
        detail_text = _safe_error_detail(detail)
        suffix = f": {detail_text}" if detail_text else ""
        super().__init__(f"HOOK API {path} failed: {label}{suffix}")


def _safe_error_detail(value: Any) -> str:
    if isinstance(value, (str, int, float, bool)):
        text = str(value)
    else:
        return ""
    return re.sub(r"\s+", " ", text).strip()[:200]


def _scalar(value: Any) -> str:
    if isinstance(value, Mapping):
        return str(value.get("String", ""))
    return "" if value is None else str(value)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _first_present(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _response_keys(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple(sorted(str(key)[:80] for key in value)[:64])


def _safe_field_shapes(value: Any) -> tuple[str, ...]:
    if not isinstance(value, Mapping):
        return ()
    shapes: list[str] = []
    for raw_key in sorted(value, key=lambda item: str(item))[:64]:
        key = str(raw_key)[:80]
        field_value = value[raw_key]
        type_name = type(field_value).__name__
        if not isinstance(field_value, str):
            shapes.append(f"{key}(type={type_name})")
            continue
        try:
            parsed = urlsplit(field_value)
            is_url = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
            query_keys = (
                sorted(
                    {
                        str(name)[:80]
                        for name, _ in parse_qsl(
                            parsed.query, keep_blank_values=True
                        )
                    }
                )[:32]
                if is_url
                else []
            )
        except ValueError:
            is_url = False
            query_keys = []
        is_hex = bool(re.fullmatch(r"[0-9a-fA-F]+", field_value))
        shapes.append(
            f"{key}(type=str,length={len(field_value)},is_hex={is_hex},"
            f"is_url={is_url},query_keys={query_keys})"
        )
    return tuple(shapes)


def _upload_ret_code(raw: Mapping[str, Any]) -> int | None:
    data = _mapping(raw.get("data"))
    return _integer(
        _first_present(
            data.get("retCode"),
            data.get("retcode"),
            raw.get("retCode"),
            raw.get("retcode"),
        )
    )


def _local_media_diagnostics(video_path: Path, thumbnail_path: Path) -> dict[str, Any]:
    diagnostics: dict[str, Any] = {}
    for prefix, path in (("video", video_path), ("thumb", thumbnail_path)):
        try:
            resolved = path.expanduser().resolve()
            exists = resolved.is_file()
            diagnostics[f"{prefix}_exists"] = exists
            diagnostics[f"{prefix}_length"] = (
                resolved.stat().st_size if exists else None
            )
        except OSError:
            diagnostics[f"{prefix}_exists"] = False
            diagnostics[f"{prefix}_length"] = None
    return diagnostics


async def _read_base64(path: Path) -> str:
    content = await asyncio.to_thread(path.read_bytes)
    return base64.b64encode(content).decode("ascii")


def _query_value(url: str, name: str) -> str:
    try:
        query = parse_qsl(urlsplit(url).query, keep_blank_values=True)
    except ValueError:
        return ""
    expected = name.casefold()
    for key, value in query:
        if key.casefold() == expected:
            return value
    return ""


def _is_http_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    return parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)


def _safe_key_shape(name: str, value: str) -> str:
    return (
        f"{name}(present={bool(value)},length={len(value)},"
        f"is_hex={bool(re.fullmatch(r'[0-9a-fA-F]+', value))})"
    )


def parse_send_result(raw: Mapping[str, Any]) -> SendResult:
    data = _mapping(raw.get("data"))
    base_response = _mapping(data.get("baseResponse") or raw.get("baseResponse"))
    raw_code = raw.get("errCode", raw.get("code"))
    code = _integer(raw_code)
    base_ret = _integer(base_response.get("ret"))
    status = str(raw.get("status", "")).lower()
    explicit_failure = (
        status in {"failed", "error"}
        or code not in {None, 0, 1, 200}
        or bool(raw.get("error"))
        or raw.get("success") is False
    )
    if base_ret not in {None, 0}:
        explicit_failure = True
    message = _scalar(
        raw.get("errMsg")
        or raw.get("msg")
        or raw.get("message")
        or raw.get("error")
    )
    message_lower = message.lower()
    if any(
        marker in message_lower
        for marker in ("失败", "error", "failed", "not success")
    ):
        explicit_failure = True

    message_id = _scalar(
        data.get("newMsgId")
        or data.get("NewMsgId")
        or data.get("message_id")
        or data.get("msgId")
        or raw.get("newMsgId")
        or raw.get("NewMsgId")
        or raw.get("message_id")
        or raw.get("msgId")
    ) or None
    create_time = _integer(
        data.get("createTime")
        or data.get("create_time")
        or raw.get("createTime")
        or raw.get("create_time")
    )
    to_user_name = _scalar(
        data.get("toUserName")
        or data.get("to_user_name")
        or data.get("wxid")
        or raw.get("toUserName")
        or raw.get("to_user_name")
        or raw.get("wxid")
    ) or None
    completion_evidence = (
        base_ret == 0
        or status in {"ok", "success", "completed"}
        or raw.get("success") is True
        or code in {0, 200}
        or message_id is not None
        or any(
            marker in message_lower
            for marker in ("success", "completed", "成功")
        )
    )
    if not completion_evidence:
        explicit_failure = True
    error = None
    if explicit_failure:
        error = message or "HOOK response did not confirm completion"
    return SendResult(
        ok=not explicit_failure,
        message_id=message_id,
        create_time=create_time,
        to_user_name=to_user_name,
        error=error,
        raw=dict(raw),
    )


def _is_large_emotion_file_fallback(result: SendResult) -> bool:
    """Recognize HOOK's successful large-emotion downgrade response.

    WeChat sends an oversized emotion as a file, but the HOOK endpoint still
    returns ``errCode=10002`` with a message containing this explanation.  It
    is an operationally successful send, so it must not be surfaced to
    OneBot/AstrBot as an ``ActionFailed``.
    """
    details = [result.error or ""]
    for key in ("errMsg", "msg", "message", "error"):
        value = result.raw.get(key)
        if value not in (None, ""):
            details.append(str(value))
    text = " ".join(details).casefold()
    return "表情包图片" in text and "当作文件发" in text


def _is_unsupported_route(result: SendResult) -> bool:
    """Return whether a write response means that the HTTP route is absent.

    HOOK normally returns HTTP 200 even for an unknown route, with a JSON
    ``code=404`` body.  Do not fall back for an ordinary revoke failure (for
    example an expired message), otherwise the second request would obscure
    the useful error and produce a misleading unsupported-route diagnosis.
    """
    raw = result.raw
    code = _integer(_first_present(raw.get("code"), raw.get("errCode")))
    if code == 404:
        return True
    detail = " ".join(
        str(raw.get(key, ""))
        for key in ("errMsg", "msg", "message", "error")
    ).casefold()
    return "url is not supported" in detail or "route not found" in detail


def _chatroom_members(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return list(data)
    if not isinstance(data, Mapping):
        return []
    chatroom_data = data.get("newChatroomData")
    if isinstance(chatroom_data, Mapping):
        members = chatroom_data.get("chatRoomMember")
        if isinstance(members, list):
            return list(members)
    members = data.get("chatRoomMember")
    return list(members) if isinstance(members, list) else []


class HookClient:
    def __init__(
        self,
        base_url: str,
        session: ClientSession,
        timeout: float = 10.0,
        retry_delay: float = 0.1,
        image_path_field: str = "filepath",
        video_download_timeout_seconds: float = 180.0,
        video_upload_timeout_seconds: float = 180.0,
    ) -> None:
        if image_path_field not in {"image_path", "filepath"}:
            raise ValueError("image_path_field must be image_path or filepath")
        self.base_url = base_url.rstrip("/")
        self.session = session
        self.timeout = ClientTimeout(total=timeout)
        if video_download_timeout_seconds <= 0:
            raise ValueError("video_download_timeout_seconds must be greater than zero")
        self.video_download_timeout_seconds = float(video_download_timeout_seconds)
        self.video_download_timeout = ClientTimeout(
            total=max(float(timeout), self.video_download_timeout_seconds)
        )
        if video_upload_timeout_seconds <= 0:
            raise ValueError("video_upload_timeout_seconds must be greater than zero")
        self.video_upload_timeout_seconds = float(video_upload_timeout_seconds)
        self.video_upload_timeout = ClientTimeout(
            total=max(float(timeout), self.video_upload_timeout_seconds)
        )
        self.retry_delay = max(0.0, retry_delay)
        self.image_path_field = image_path_field
        self._friend_refresh_attempted = False

    async def _response_json(
        self,
        response: ClientResponse,
        path: str,
        *,
        allow_json_array: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        raw_body = await response.read()
        diagnostics: dict[str, Any] = {
            "http_status": response.status,
            "content_type": response.headers.get("Content-Type", ""),
            "body_length": len(raw_body),
        }
        try:
            text = raw_body.decode(response.get_encoding())
            body = json.loads(text)
        except (json.JSONDecodeError, UnicodeDecodeError, LookupError) as exc:
            diagnostics["parse_error"] = type(exc).__name__
            raise HookApiError(
                path,
                response.status,
                "response body is not valid JSON",
                diagnostics=diagnostics,
            ) from exc
        diagnostics.update(
            {
                "json_type": type(body).__name__,
                "root_keys": _response_keys(body),
                "data_keys": _response_keys(
                    body.get("data") if isinstance(body, Mapping) else None
                ),
            }
        )
        if isinstance(body, Mapping):
            api_code = _integer(_first_present(body.get("code"), body.get("errCode")))
            if api_code is not None:
                diagnostics["api_code"] = api_code
            ret_code = _upload_ret_code(body)
            if ret_code is not None:
                diagnostics["data_ret_code"] = ret_code
        valid_root = isinstance(body, dict) or (
            allow_json_array and isinstance(body, list)
        )
        if response.status >= 400 or not valid_root:
            raise HookApiError(
                path,
                response.status,
                body,
                diagnostics=diagnostics,
            )
        return body, diagnostics

    async def _post_response(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        retry_read: bool = False,
        timeout: ClientTimeout | None = None,
        allow_json_array: bool = False,
    ) -> tuple[Any, dict[str, Any]]:
        attempts = 3 if retry_read else 1
        for attempt in range(attempts):
            try:
                async with self.session.post(
                    f"{self.base_url}{path}",
                    json=dict(payload),
                    timeout=timeout or self.timeout,
                ) as response:
                    return await self._response_json(
                        response,
                        path,
                        allow_json_array=allow_json_array,
                    )
            except (ClientConnectionError, asyncio.TimeoutError):
                if attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(self.retry_delay * (attempt + 1))
            except HookApiError as exc:
                # Read-only 4.1.8.27 endpoints can briefly reply with an
                # incomplete HTTP-200 body while their local cache is warming.
                # Writes must never be retried here because they are not
                # idempotent.
                if exc.status != 200 or attempt + 1 >= attempts:
                    raise
                await asyncio.sleep(self.retry_delay * (attempt + 1))
        raise AssertionError("request attempt loop exhausted")

    async def _post(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        retry_read: bool = False,
        timeout: ClientTimeout | None = None,
        allow_json_array: bool = False,
    ) -> Any:
        body, _ = await self._post_response(
            path,
            payload,
            retry_read=retry_read,
            timeout=timeout,
            allow_json_array=allow_json_array,
        )
        return body

    async def _warm_up_cdn_info(self) -> None:
        try:
            await self._post_response("/api/get_cdn_info", {}, retry_read=True)
        except (HookApiError, ClientConnectionError, asyncio.TimeoutError):
            return

    async def _attach_cgi_offset_diagnostics(
        self, diagnostics: dict[str, Any]
    ) -> None:
        try:
            raw, _ = await self._post_response(
                "/api/get_cgi_offset",
                {},
                retry_read=True,
            )
        except (HookApiError, ClientConnectionError, asyncio.TimeoutError):
            return
        diagnostics.update(cgi_offset_summary(raw))

    async def _write(self, path: str, payload: Mapping[str, Any]) -> SendResult:
        return parse_send_result(await self._post(path, payload, retry_read=False))

    async def _read_data(self, path: str, payload: Mapping[str, Any]) -> Any:
        raw = await self._post(
            path,
            payload,
            retry_read=True,
            allow_json_array=True,
        )
        return raw.get("data", raw) if isinstance(raw, Mapping) else raw

    async def send_text(self, wxid: str, text: str) -> SendResult:
        return await self._write("/api/send_text_msg", {"wxid": wxid, "msg": text})

    async def send_reply_text(
        self, wxid: str, text: str, target: "ReplyTarget"
    ) -> SendResult:
        """Send a native WeChat type=57 reply to the wake-up message."""
        def field(value: Any) -> str:
            return xml_escape(str(value or ""), {"'": "&apos;", '"': "&quot;"})

        content = field(text)
        quoted = field(target.content)
        xml = (
            '<appmsg appid="" sdkver="0">'
            f"<title>{content}</title>"
            f"<des>{content}</des>"
            "<action>view</action><type>57</type><showtype>0</showtype>"
            f"<content>{content}</content><url></url><lowurl></lowurl>"
            "<appattach><totallen>0</totallen><attachid></attachid>"
            "<fileext></fileext></appattach><extinfo></extinfo>"
            "<sourceusername></sourceusername><sourcedisplayname></sourcedisplayname>"
            "<commenturl></commenturl><thumburl></thumburl>"
            "<messageaction></messageaction><laninfo></laninfo>"
            "<refermsg>"
            "<type>1</type>"
            f"<svrid>{field(target.message_id)}</svrid>"
            f"<fromusr>{field(target.sender_id)}</fromusr>"
            f"<chatusr>{field(target.sender_id)}</chatusr>"
            f"<displayname>{field(target.sender_name)}</displayname>"
            f"<content>{quoted}</content>"
            "<msgsource></msgsource>"
            "</refermsg></appmsg>"
        )
        return await self._write(
            "/api/send_app_msg", {"wxid": wxid, "content": xml, "type": 57}
        )

    async def send_at_text(
        self, room_id: str, text: str, wxids: Sequence[str]
    ) -> SendResult:
        return await self._write(
            "/api/send_at_text",
            {"roomId": room_id, "msg": text, "wxids": ",".join(wxids)},
        )

    async def send_image(self, wxid: str, path: Path) -> SendResult:
        return await self._write(
            "/api/send_image_msg",
            {"wxid": wxid, self.image_path_field: str(path)},
        )

    async def send_file(self, wxid: str, path: Path) -> SendResult:
        return await self._write(
            "/api/send_file_msg", {"wxid": wxid, "filepath": str(path)}
        )

    async def send_mp3_voice(self, wxid: str, path: Path) -> SendResult:
        return await self._write(
            "/api/send_mp3_voice", {"wxid": wxid, "mp3Path": str(path)}
        )

    async def send_silk_voice(self, wxid: str, path: Path) -> SendResult:
        return await self._write(
            "/api/send_voice", {"toWxid": wxid, "silkPath": str(path)}
        )

    async def send_emotion(self, wxid: str, path: Path) -> SendResult:
        result = await self._write(
            "/api/send_emotion_msg", {"wxid": wxid, "filepath": str(path)}
        )
        if not result.ok and _is_large_emotion_file_fallback(result):
            return SendResult(
                ok=True,
                message_id=result.message_id,
                create_time=result.create_time,
                to_user_name=result.to_user_name,
                raw=result.raw,
            )
        return result

    async def upload_video(
        self, video_path: Path, thumbnail_path: Path
    ) -> VideoUpload:
        api_path = "/api/cdn_c2c_video_upload"
        path_payload = {
            "videoPath": str(video_path),
            "thumbPath": str(thumbnail_path),
            "videoBase64": "",
            "thumbBase64": "",
        }
        local_diagnostics = _local_media_diagnostics(video_path, thumbnail_path)
        raw: dict[str, Any] = {}
        diagnostics: dict[str, Any] = {}
        base64_payload: dict[str, Any] | None = None
        for attempt in range(2):
            payload = path_payload if attempt == 0 else base64_payload
            if payload is None:
                payload = {
                    "videoPath": "",
                    "thumbPath": "",
                    "videoBase64": await _read_base64(video_path),
                    "thumbBase64": await _read_base64(thumbnail_path),
                }
                base64_payload = payload
            try:
                raw, diagnostics = await self._post_response(
                    api_path,
                    payload,
                    retry_read=False,
                    timeout=self.video_upload_timeout,
                )
            except HookApiError as exc:
                exc.diagnostics.update(local_diagnostics)
                raise
            diagnostics.update(local_diagnostics)
            data = _mapping(raw.get("data"))
            file_url = _scalar(data.get("fileUrl") or raw.get("fileUrl")).strip()
            file_id = _scalar(data.get("fileId") or raw.get("fileId")).strip()
            if file_url or file_id or _upload_ret_code(raw) is None or attempt:
                break
            await self._warm_up_cdn_info()

        data = _mapping(raw.get("data"))
        diagnostics["data_field_shapes"] = _safe_field_shapes(data)
        file_url = _scalar(data.get("fileUrl") or raw.get("fileUrl")).strip()
        file_id = _scalar(data.get("fileId") or raw.get("fileId")).strip()
        explicit_key = _scalar(data.get("encKey") or raw.get("encKey")).strip()
        url_file_key = _query_value(file_url, "filekey")
        file_id_is_hex = bool(re.fullmatch(r"[0-9a-fA-F]+", file_id))
        file_url_is_http = _is_http_url(file_url)
        patched_key = ""
        if file_url and file_id:
            if explicit_key:
                patched_key = explicit_key
            elif file_url_is_http:
                patched_key = file_id
            elif not (file_id_is_hex and len(file_id) >= 64):
                patched_key = file_id
        encryption_key = explicit_key or patched_key
        cdn_video_url = file_url or (file_id if encryption_key else "")
        if not cdn_video_url:
            ret_code = _upload_ret_code(raw)
            detail = (
                f"upload returned retCode={ret_code} without CDN credentials"
                if ret_code is not None
                else _scalar(
                    raw.get("errMsg")
                    or raw.get("msg")
                    or raw.get("message")
                    or "response is missing fileUrl or legacy fileId/encKey"
                )
            )
            await self._attach_cgi_offset_diagnostics(diagnostics)
            raise HookApiError(
                api_path,
                None,
                detail,
                diagnostics=diagnostics,
            )
        if not encryption_key:
            detail = "response is missing a valid video AES key"
            raise HookApiError(
                api_path,
                None,
                detail,
                diagnostics=diagnostics,
            )
        return VideoUpload(
            cdn_video_url=cdn_video_url,
            encryption_key=encryption_key,
            url_file_key=url_file_key,
            credential_shapes=(
                _safe_key_shape("fileId-key", patched_key),
                _safe_key_shape("filekey", url_file_key),
            ),
            response_keys=tuple(sorted(str(key) for key in raw)),
            data_keys=tuple(sorted(str(key) for key in data)),
        )

    async def forward_video(
        self,
        wxid: str,
        upload: VideoUpload,
        *,
        video_length: int,
        thumbnail_length: int,
        play_length: int,
    ) -> SendResult:
        return await self._write(
            "/api/cdn_video_forward",
            {
                "wxid": wxid,
                "cdnVideoUrl": upload.cdn_video_url,
                "aesKey": upload.encryption_key,
                "videoLength": int(video_length),
                "thumbLength": int(thumbnail_length),
                "playLength": int(play_length),
            },
        )

    async def send_link_card(
        self,
        wxid: str,
        title: str,
        description: str,
        thumb_url: str,
        url: str,
    ) -> SendResult:
        """Send a native link card through HOOK's dedicated link API.

        ``send_app_msg`` accepts an appmsg XML document but does not turn a
        remote ``thumburl`` into the CDN thumbnail metadata needed by WeChat.
        The documented ``send_xml`` endpoint owns that fetch/upload step.
        """
        return await self._write(
            "/api/send_xml",
            {
                "wxid": wxid,
                "title": title,
                "description": description,
                "thumbUrl": thumb_url,
                "url": url,
            },
        )

    async def send_app_xml(
        self, wxid: str, content: str, app_type: int = 3
    ) -> SendResult:
        return await self._write(
            "/api/send_app_msg",
            {"wxid": wxid, "content": content, "type": app_type},
        )

    async def recall(
        self, message_id: str, create_time: int, to_user_name: str
    ) -> SendResult:
        # The DLL bundled with the current 4.1.8.27 bridge exposes
        # ``/api/revoke_msg``.  Newer HOOK builds document ``/api/revoke_any``
        # instead, so keep the latter as a compatibility fallback.  Both
        # routes ultimately need the real WeChat ``newMsgId`` and timestamp;
        # the image send endpoint does not manufacture those values for us.
        try:
            numeric_message_id: int | str = int(message_id)
        except (TypeError, ValueError):
            numeric_message_id = message_id

        current_result = await self._write(
            "/api/revoke_msg",
            {
                "newMsgId": numeric_message_id,
                "createTime": int(create_time),
                "toUserName": to_user_name,
            },
        )
        if current_result.ok or not _is_unsupported_route(current_result):
            return current_result

        return await self._write(
            "/api/revoke_any",
            {
                "newMsgId": numeric_message_id,
                "createTime": int(create_time),
                "toUserName": to_user_name,
            },
        )

    async def get_friends(self) -> list[dict[str, Any]]:
        data = await self._read_data("/api/get_frien_lists", {})
        if isinstance(data, Mapping):
            data = data.get("contact_list", [])
        friends = list(data) if isinstance(data, list) else []
        if friends or self._friend_refresh_attempted:
            return friends

        # A new HOOK process can start with an empty local friend cache.  The
        # documented refresh endpoint populates it from WeChat; only attempt
        # this once per bridge process so UI polling cannot repeatedly refresh.
        self._friend_refresh_attempted = True
        try:
            refreshed = await self._read_data("/api/update_all_friend", {})
        except (HookApiError, ClientConnectionError, asyncio.TimeoutError):
            return []
        if isinstance(refreshed, Mapping):
            refreshed = refreshed.get("contact_list", [])
        return list(refreshed) if isinstance(refreshed, list) else []

    async def get_groups(self) -> list[dict[str, Any]]:
        data = await self._read_data("/api/get_chatroom_list", {})
        return list(data) if isinstance(data, list) else []

    async def get_group_members(self, room_id: str) -> list[dict[str, Any]]:
        data = await self._read_data("/api/get_room_members", {"room_id": room_id})
        return _chatroom_members(data)

    async def get_group_detail_members(self, room_id: str) -> list[dict[str, Any]]:
        data = await self._read_data(
            "/api/get_chatroom_detail_cache", {"room_id": room_id}
        )
        return _chatroom_members(data)

    async def get_profile(self) -> dict[str, Any]:
        data = await self._read_data("/api/get_profile_cache", {})
        return dict(data) if isinstance(data, Mapping) else {}

    async def get_contact(self, wxid: str) -> dict[str, Any]:
        data = await self._read_data("/api/get_contact", {"wxid": wxid})
        return dict(data) if isinstance(data, Mapping) else {}

    async def download_image(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._post("/api/download_img", payload, retry_read=True)

    async def download_cdn(
        self,
        file_id: str,
        aes_key: str,
        image_type: int,
        destination: Path,
    ) -> dict[str, Any]:
        return await self._post(
            "/api/cdn_download",
            {
                "fileid": file_id,
                "asekey": aes_key,
                "imgType": int(image_type),
                "out": str(destination),
            },
            retry_read=True,
        )

    async def download_file(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._post("/api/download_file", payload, retry_read=True)

    async def download_voice(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        return await self._post("/api/download_voice", payload, retry_read=True)

    async def download_video(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        # This endpoint starts a native WeChat download and can remain busy for
        # minutes.  Retrying it after a client timeout can start a second native
        # download for the same message, which is unsafe for the 4.1.8.27 DLL.
        return await self._post(
            "/api/download_video",
            payload,
            retry_read=False,
            timeout=self.video_download_timeout,
        )
