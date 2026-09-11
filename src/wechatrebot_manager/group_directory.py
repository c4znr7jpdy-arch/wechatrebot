"""Account-scoped, memory-only directory from the active connection service."""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import threading
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

from .runtime_status import probe_bridge_health


class GroupDirectoryError(ValueError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class GroupDirectoryService:
    def __init__(self, install_root: str | Path):
        self.root = Path(install_root)
        self._lock = threading.Lock()
        self._account = ""
        self._snapshot = None

    def _connection(self):
        try:
            config = json.loads((self.root / "app/bridge/config.json").read_text("utf-8-sig"))
            port = config.get("callback_port", 5000)
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
        account = health.wechat_identity if health.wechat_logged_in else ""
        if not account:
            # Unknown identity must not expose a previous account's directory.
            self._account, self._snapshot = "", None
            raise GroupDirectoryError("尚未确认微信登录账号，请先连接微信，再刷新群列表。")
        if account != self._account:
            self._account, self._snapshot = account, None
        return port, token, account

    def _clear_current_account(self, account: str, *, state_locked: bool) -> None:
        def clear() -> None:
            if self._account == account:
                self._account, self._snapshot = "", None

        if state_locked:
            clear()
            return
        with self._lock:
            clear()

    def _request(self, port, token, account, path, *, state_locked: bool = False):
        request = Request(f"http://127.0.0.1:{port}{path}", headers={
            "X-WeChatReBot-Token": token, "X-WeChatReBot-Account": account,
            "Accept": "application/json", "Cache-Control": "no-cache",
        })
        try:
            with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=20) as response:
                raw = response.read(4 * 1024 * 1024 + 1)
                if len(raw) > 4 * 1024 * 1024:
                    raise ValueError()
                body = json.loads(raw.decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 409:
                self._clear_current_account(account, state_locked=state_locked)
                raise GroupDirectoryError("微信账号已变化，请重新刷新群列表。") from None
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
        after = probe_bridge_health(port=port)
        if not after.wechat_logged_in or after.wechat_identity != account:
            self._clear_current_account(account, state_locked=state_locked)
            raise GroupDirectoryError("微信账号已变化，请重新刷新群列表。")
        return data

    def list(self, refresh: bool = False):
        with self._lock:
            try:
                port, token, account = self._connection()
                if refresh or self._snapshot is None:
                    data = self._request(
                        port,
                        token,
                        account,
                        "/api/manager/groups",
                        state_locked=True,
                    )
                    self._snapshot = {**data, "updated_at": datetime.now(timezone.utc).isoformat()}
                return {**copy.deepcopy(self._snapshot), "stale": False,
                        "message": "读取的是当前微信可提供的群信息，不会向群里发送消息。"}
            except GroupDirectoryError as exc:
                return {**copy.deepcopy(self._snapshot or {"items": [], "total": 0, "account_id": self._account, "updated_at": None}),
                        "stale": True, "message": str(exc)}

    def members(self, group_id: str):
        if not isinstance(group_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+@chatroom", group_id):
            raise GroupDirectoryError("群标识格式不正确，请从群列表重新选择。")
        with self._lock:
            port, token, account = self._connection()
            if not self._snapshot or not any(item.get("group_id") == group_id for item in self._snapshot["items"]):
                raise GroupDirectoryError("请先刷新群列表，再选择当前账号所在的群。")
        return self._request(port, token, account, f"/api/manager/groups/{quote(group_id, safe='')}/members")
