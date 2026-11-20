"""Account-scoped live directory with persistent portrait fallback."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .error_catalog import AccountStatusUnavailableError
from .licensing.protocol import LicenseError
from .port_config import load_runtime_ports
from .runtime_status import probe_bridge_health
from .portrait_cache import PortraitCache


class GroupDirectoryError(ValueError):
    pass


class GroupDirectoryUnavailable(GroupDirectoryError, AccountStatusUnavailableError):
    """Account verification failed; never return an unverified cached directory."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GroupDirectoryService:
    _REFRESH_INTERVAL_SECONDS = 15.0
    _RETRY_INTERVAL_SECONDS = 15.0
    _QRCODE_CACHE_SECONDS = 300.0
    _QRCODE_CACHE_LIMIT = 32

    def __init__(self, install_root: str | Path):
        self.root = Path(install_root)
        self._lock = threading.Lock()
        self._account = ""
        self._account_generation = 0
        self._snapshot = None
        self._next_refresh_at = 0.0
        self._last_error = ""
        self._portraits = PortraitCache(self.root)
        self._qrcodes: dict[str, tuple[float, dict[str, str]]] = {}

    def _is_generation_current(self, account: str, generation: int) -> bool:
        with self._lock:
            return (
                bool(account)
                and self._account == account
                and self._account_generation == generation
            )

    def _connection(self):
        try:
            config = json.loads((self.root / "app/bridge/config.json").read_text("utf-8-sig"))
            port = config.get("callback_port", load_runtime_ports(self.root).bridge_callback)
            token = config.get("manager_query_token", "")
            if config.get("callback_host", "127.0.0.1") not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError()
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError()
            if not isinstance(token, str) or len(token) < 32:
                raise GroupDirectoryError("当前版本尚未准备好群目录，请安装完整更新后重新打开机器人。")
        except GroupDirectoryError:
            raise
        except (OSError, ValueError, AttributeError):
            raise GroupDirectoryError("微信连接设置无法读取，请检查安装或重新选择微信。") from None
        health = probe_bridge_health(port=port)
        if not getattr(health, "status_available", True) and not health.wechat_identity:
            raise GroupDirectoryUnavailable("微信账号状态暂时不可用，请稍后重试。")
        account = health.wechat_identity if health.wechat_logged_in else ""
        if not account:
            # Unknown identity must not expose a previous account's directory.
            if self._account or self._snapshot is not None:
                self._account_generation += 1
            self._account, self._snapshot = "", None
            self._next_refresh_at = 0.0
            self._last_error = ""
            self._qrcodes.clear()
            raise GroupDirectoryError("尚未确认微信登录账号，请先连接微信，再刷新群列表。")
        if account != self._account:
            self._account_generation += 1
            self._account, self._snapshot = account, None
            self._next_refresh_at = 0.0
            self._last_error = ""
            self._qrcodes.clear()
        return port, token, account

    def _clear_current_account(self, account: str, *, state_locked: bool) -> None:
        def clear() -> None:
            if self._account == account:
                self._account_generation += 1
                self._account, self._snapshot = "", None
                self._next_refresh_at = 0.0
                self._last_error = ""
                self._qrcodes.clear()

        if state_locked:
            clear()
            return
        with self._lock:
            clear()

    def _verify_current_account(self, port: int, account: str, *, state_locked: bool) -> None:
        after = probe_bridge_health(port=port)
        if not getattr(after, "status_available", True) and not after.wechat_identity:
            raise GroupDirectoryUnavailable("微信账号状态暂时不可用，请稍后重试。")
        if not after.wechat_logged_in or after.wechat_identity != account:
            self._clear_current_account(account, state_locked=state_locked)
            raise GroupDirectoryError("微信账号已变化，请重新刷新群列表。")

    def _request(
        self,
        port,
        token,
        account,
        path,
        *,
        state_locked: bool = False,
        max_response_bytes: int = 4 * 1024 * 1024,
        verify_account: bool = True,
    ):
        if isinstance(max_response_bytes, bool) or not 1 <= max_response_bytes <= 32 * 1024 * 1024:
            raise ValueError("响应大小上限无效")
        request = Request(f"http://127.0.0.1:{port}{path}", headers={
            "X-WeChatReBot-Token": token, "X-WeChatReBot-Account": account,
            "Accept": "application/json", "Cache-Control": "no-cache",
        })
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(max_response_bytes + 1)
                if len(raw) > max_response_bytes:
                    raise ValueError()
                body = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if (exc.code == 403 and path.startswith("/api/manager/groups/")
                    and path.endswith("/qrcode")):
                try:
                    with exc:
                        denied = exc.read(4097)
                    failure = json.loads(denied.decode("utf-8")) if len(denied) <= 4096 else None
                except (OSError, ValueError, UnicodeError, RecursionError):
                    failure = None
                error = failure.get("error") if isinstance(failure, dict) else None
                if (isinstance(error, dict) and failure.get("ok") is False
                        and error.get("code") == "LIC-automation_paused"
                        and error.get("retryable") is False):
                    raise LicenseError(
                        "automation_paused",
                        "自动化已暂停，群二维码暂不可用。请在运行中心点击启动机器人后重试。",
                    ) from None
            if exc.code == 409:
                # The bridge also returns 409 when its account probe is unavailable.
                # Only a fresh, confirmed account change may discard this cache.
                self._verify_current_account(port, account, state_locked=state_locked)
                raise GroupDirectoryUnavailable("微信账号状态暂时不可用，请稍后重试。") from None
            if exc.code in {401, 403, 404, 503}:
                raise GroupDirectoryError("群目录接口尚不可用，请确认已安装完整更新并连接微信。") from None
            raise GroupDirectoryError("群信息读取失败，旧列表未更新，请稍后重试。") from None
        except (OSError, URLError, ValueError, UnicodeError):
            raise GroupDirectoryError("群信息读取超时或格式不正确，旧列表未更新，请稍后重试。") from None
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict) or body.get("ok") is not True or data.get("account_id") != account:
            raise GroupDirectoryError("未收到当前账号的群信息，请重新连接后刷新。")
        if not isinstance(data.get("items"), list):
            raise GroupDirectoryError("群信息格式不正确，请稍后重试。")
        if verify_account:
            self._verify_current_account(port, account, state_locked=state_locked)
        return data

    def _enrich_portraits(
        self,
        account: str,
        generation: int,
        items: list[dict],
        *,
        port: int | None = None,
        token: str = "",
        force: bool = False,
    ) -> list[dict]:
        if not items:
            return items
        guard = (
            (lambda a=account, g=generation: self._is_generation_current(a, g))
            if port is not None
            else None
        )
        with self._portraits.batch(account):
            return [
                self._portraits.enrich(
                    account,
                    item,
                    str(item.get("group_id") or ""),
                    (
                        lambda group_id=item.get("group_id"): self._request(
                            port,
                            token,
                            account,
                            f"/api/manager/contacts/{quote(str(group_id), safe='')}",
                        ).get("item", {})
                    )
                    if port is not None
                    else None,
                    force=force,
                    **(
                        {"still_current": guard}
                        if guard is not None
                        else {}
                    ),
                )
                for item in items
            ]

    @staticmethod
    def _empty_snapshot(account: str) -> dict[str, object]:
        return {"items": [], "total": 0, "account_id": account, "updated_at": None}

    def list(self, refresh: bool = False, *, include_portraits: bool = True):
        fetch_account = ""
        fetch_generation = 0
        try:
            with self._lock:
                port, token, account = self._connection()
                generation = self._account_generation
                needs_fetch = refresh or time.monotonic() >= self._next_refresh_at
                if not needs_fetch:
                    snapshot = copy.deepcopy(self._snapshot or self._empty_snapshot(account))
                    stale = bool(self._last_error)
                    message = self._last_error or "读取的是当前微信可提供的群信息，不会向群里发送消息。"
            if needs_fetch:
                fetch_account, fetch_generation = account, generation
                data = self._request(
                    port,
                    token,
                    account,
                    "/api/manager/groups",
                    state_locked=False,
                )
                fetch_account = ""
                with self._lock:
                    if self._account != account or self._account_generation != generation:
                        raise GroupDirectoryError("微信账号已变化，请重新刷新群列表。")
                    self._snapshot = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
                    self._next_refresh_at = time.monotonic() + self._REFRESH_INTERVAL_SECONDS
                    self._last_error = ""
                    snapshot = copy.deepcopy(self._snapshot)
                    stale = False
                    message = "读取的是当前微信可提供的群信息，不会向群里发送消息。"
        except GroupDirectoryUnavailable:
            raise
        except GroupDirectoryError as exc:
            with self._lock:
                if fetch_account and self._account == fetch_account and self._account_generation == fetch_generation:
                    self._next_refresh_at = time.monotonic() + self._RETRY_INTERVAL_SECONDS
                    self._last_error = str(exc)
                fallback_account = self._account
                fallback_generation = self._account_generation
                fallback = copy.deepcopy(self._snapshot or self._empty_snapshot(fallback_account))
            if include_portraits:
                fallback["items"] = self._enrich_portraits(
                    fallback_account, fallback_generation, fallback["items"]
                )
            return {**fallback, "stale": True, "message": str(exc)}
        if include_portraits:
            snapshot["items"] = self._enrich_portraits(
                account, generation, snapshot["items"], port=port, token=token, force=refresh
            )
        return {**snapshot, "stale": stale, "message": message}

    def members(self, group_id: str):
        if not isinstance(group_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+@chatroom", group_id):
            raise GroupDirectoryError("群标识格式不正确，请从群列表重新选择。")
        with self._lock:
            port, token, account = self._connection()
            if not self._snapshot or not any(item.get("group_id") == group_id for item in self._snapshot["items"]):
                raise GroupDirectoryError("请先刷新群列表，再选择当前账号所在的群。")
        return self._request(port, token, account, f"/api/manager/groups/{quote(group_id, safe='')}/members")

    def qrcode(self, account_id: str, group_id: str) -> dict[str, str]:
        if not isinstance(group_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+@chatroom", group_id):
            raise GroupDirectoryError("群标识格式不正确，请从群列表重新选择。")
        with self._lock:
            port, token, account = self._connection()
            if not account_id or account_id != account:
                raise GroupDirectoryError("微信账号已变化，请重新刷新群列表。")
            if not self._snapshot or not any(item.get("group_id") == group_id for item in self._snapshot["items"]):
                raise GroupDirectoryError("请先刷新群列表，再选择当前账号所在的群。")
            now = time.monotonic()
            self._qrcodes = {key: cached for key, cached in self._qrcodes.items() if cached[0] > now}
            if group_id in self._qrcodes:
                return dict(self._qrcodes[group_id][1])
        data = self._request(port, token, account, f"/api/manager/groups/{quote(group_id, safe='')}/qrcode")
        item = data["items"][0] if len(data["items"]) == 1 else None
        if not isinstance(item, dict) or item.get("group_id") != group_id:
            raise GroupDirectoryError("未收到当前群聊的二维码，请稍后重试。")
        image = item.get("image_data_url")
        if (not isinstance(image, str) or len(image) > 2 * 1024 * 1024 + 23
                or not re.fullmatch(r"data:image/(?:jpeg|png);base64,[A-Za-z0-9+/=]+", image)):
            raise GroupDirectoryError("群二维码格式不正确，请稍后重试。")
        result = {"account_id": account, "group_id": group_id, "image_data_url": image}
        with self._lock:
            if self._account != account:
                raise GroupDirectoryError("微信账号已变化，请重新刷新群列表。")
            if len(self._qrcodes) >= self._QRCODE_CACHE_LIMIT:
                self._qrcodes.pop(next(iter(self._qrcodes)))
            self._qrcodes[group_id] = (time.monotonic() + self._QRCODE_CACHE_SECONDS, result)
        return dict(result)

    def message_image(
        self,
        account_id: str,
        conversation_id: str,
        message_id: int | str,
        image_index: int,
    ) -> dict[str, object]:
        """Read one original image through the account-authenticated bridge route."""
        if not isinstance(conversation_id, str) or not re.fullmatch(
            r"[A-Za-z0-9_.:@-]{1,256}", conversation_id
        ):
            raise GroupDirectoryError("消息会话标识无效，请刷新消息中心后重试。")
        if (isinstance(message_id, bool) or not str(message_id).isascii()
                or not str(message_id).isdigit() or len(str(message_id)) > 18
                or isinstance(image_index, bool) or not isinstance(image_index, int)):
            raise GroupDirectoryError("消息图片标识无效，请刷新消息中心后重试。")
        numeric_message_id = int(message_id)
        numeric_image_index = image_index
        if numeric_message_id <= 0 or not 0 <= numeric_image_index < 4:
            raise GroupDirectoryError("消息图片标识无效，请刷新消息中心后重试。")
        with self._lock:
            port, token, account = self._connection()
            if not account_id or account_id != account:
                raise GroupDirectoryError("微信账号已变化，请刷新消息中心后重试。")
        path = (
            f"/api/manager/history/{numeric_message_id}/images/{numeric_image_index}"
            f"?conversation_id={quote(conversation_id, safe='')}"
        )
        try:
            data = self._request(
                port, token, account, path, max_response_bytes=28 * 1024 * 1024,
            )
        except GroupDirectoryError:
            raise GroupDirectoryError(
                "原图未能获取，可能已过期或连接暂不可用；已显示缩略图。"
            ) from None
        item = data["items"][0] if len(data["items"]) == 1 else None
        if (not isinstance(item, dict) or type(item.get("message_id")) is not int
                or item["message_id"] != numeric_message_id):
            raise GroupDirectoryError("未收到当前消息的原图，请稍后重试。")
        if type(item.get("image_index")) is not int or item["image_index"] != numeric_image_index:
            raise GroupDirectoryError("未收到请求的消息图片，请稍后重试。")
        source = item.get("image_data_url")
        if (
            not isinstance(source, str)
            or len(source) > 28 * 1024 * 1024
            or not re.fullmatch(
                r"data:image/(?:jpeg|png|webp|gif);base64,[A-Za-z0-9+/=]+",
                source,
            )
        ):
            raise GroupDirectoryError("原图格式不正确，请稍后重试。")
        quality = item.get("quality")
        return {
            "account_id": account,
            "message_id": numeric_message_id,
            "image_index": numeric_image_index,
            "image_data_url": source,
            "quality": quality if isinstance(quality, str) and quality in {"original", "cached"} else "cached",
        }
