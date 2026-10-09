"""Local execution guards shared by the independent core and bridge processes."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import wraps
import inspect
import logging
import os
from pathlib import Path
import threading
from typing import Callable
from urllib.request import ProxyHandler, Request, build_opener

from .client import LicenseClient, _NoRedirect
from .config import load_channel
from .protocol import ACCOUNT_PATTERN, LicenseError, parse_json

_LOG = logging.getLogger(__name__)
_READ_HOOK_PATHS = frozenset({
    "/api/health", "/api/get_profile_cache", "/api/get_contact", "/api/get_cdn_info",
    "/api/get_cgi_offset", "/api/get_frien_lists", "/api/update_all_friend",
    "/api/get_chatroom_list", "/api/get_room_members", "/api/get_chatroom_detail_cache",
    "/api/download_img", "/api/cdn_download", "/api/download_file",
    "/api/download_voice", "/api/download_video",
})
_READ_ONEBOT_ACTIONS = frozenset({
    "get_login_info", "get_self_info", "get_status", "get_version_info", "get_version",
    "can_send_image", "can_send_record", "get_msg", "get_group_msg_history",
    "get_friend_list", "get_group_list", "get_group_info", "get_stranger_info", "get_user_info",
    "get_group_member_info", "get_group_member_list", "get_group_member_cache",
})
_EVENT_RUN_ID_FIELD = "_wechatrebot_license_run_id"
_action_session: ContextVar[str | None] = ContextVar("wechatrebot_license_action", default=None)
_PROVIDER_CALLS = frozenset({
    "text_chat", "text_chat_stream", "get_text", "get_audio", "get_audio_stream",
    "get_embedding", "get_embeddings", "get_embeddings_batch", "rerank",
})


def resolve_install_root(module_file: str | Path = __file__, environ=None) -> Path | None:
    """Installed package placement is authoritative; environment is dev-only fallback."""
    path = Path(module_file).resolve()
    for parent in path.parents:
        if parent.name.casefold() != "site-packages":
            continue
        if [item.name.casefold() for item in list(parent.parents)[:3]] == ["lib", "python", "runtime"]:
            return parent.parents[3]
    configured = (os.environ if environ is None else environ).get("WECHATREBOT_INSTALL_ROOT", "")
    return Path(configured).expanduser().resolve() if configured else None


def probe_account() -> str:
    """Read the bridge's actual identity, independently of the manager window."""
    request = Request("http://127.0.0.1:5000/api/health", method="GET")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=0.5) as response:
            raw = response.read(16_385)
        if len(raw) > 16_384:
            return ""
        value = parse_json(raw)
        if any(value.get(field) is not True for field in (
            "service_ready", "status_available", "hook_ready", "wechat_logged_in",
        )):
            return ""
        account = value.get("wechat_identity")
        return account if isinstance(account, str) and ACCOUNT_PATTERN.fullmatch(account) else ""
    except (OSError, ValueError, TypeError):
        return ""


class RuntimeGuard:
    def __init__(self, root: str | Path | None = None, *, client=None, account_source=None):
        self.root = Path(root).resolve() if root is not None else resolve_install_root()
        # Source-only legacy tests have no installation. This fallback is never
        # accepted once the bundled commercial channel is enabled.
        self._channel_root = self.root or Path(__file__).resolve().parents[1]
        self.client = client or LicenseClient(self._channel_root)
        self.account_source: Callable[[], str] = account_source or probe_account
        self._refresh_stop = threading.Event()
        self._refresh_thread: threading.Thread | None = None
        self._refresh_lock = threading.Lock()
        self._core_session: str | None = None
        self._core_blocked = False

    @staticmethod
    def _denied(reason: str, message: str) -> dict:
        return {"enabled": True, "can_run": False, "state": reason, "message": message, "run_id": ""}

    def status(self, expected_account: str = "") -> dict:
        try:
            channel = load_channel(self._channel_root)
            if not channel.enabled:
                return {"enabled": False, "can_run": True, "state": "legacy", "run_id": ""}
            if self.root is None:
                return self._denied("runtime_root_unavailable", "无法确认安装目录，自动化已暂停。")
            actual = self.account_source()
            actual = actual if isinstance(actual, str) and ACCOUNT_PATTERN.fullmatch(actual) else ""
            state = self.client.snapshot(actual)
            if not actual or (expected_account and expected_account != actual):
                return {**state, "can_run": False, "state": "account_mismatch",
                        "message": "无法确认当前实际微信账号，自动化已暂停。"}
            return state
        except (OSError, ValueError, TypeError) as exc:
            # The client's safe status is authoritative. Never reveal a path,
            # local secret or raw transport error in a runtime failure.
            _LOG.debug("Runtime authorization unavailable (%s)", type(exc).__name__)
            return self._denied("authorization_unavailable", "授权暂时无法验证，自动化已暂停。")

    def require(self, expected_account: str = "", *, session: str | None = None) -> dict:
        state = self.status(expected_account)
        if not state.get("enabled"):
            return state
        if not state.get("can_run") or (session is not None and session != state.get("run_id")):
            raise LicenseError("automation_paused", state.get("message") or "自动化已暂停，请在管理器中确认恢复。")
        return state

    def claim_core_session(self) -> None:
        state = self.require()
        self._core_session = state.get("run_id", "")
        self._core_blocked = False
        self.start_refresh()

    def require_core(self, expected_account: str = "") -> dict:
        if self._core_blocked:
            raise LicenseError("automation_paused", "本次自动化运行已停止，请在管理器中确认恢复。")
        try:
            return self.require(expected_account, session=self._core_session)
        except LicenseError:
            # Recovery is a fresh core process/session. A still-unwinding old
            # task must not resume merely because the subscription was renewed.
            self._core_blocked = True
            raise

    def allow_event(self, event: dict, *, queued: bool = False) -> bool:
        if event.get("post_type") in {None, "", "meta_event"}:
            return True
        state = self.status(str(event.get("self_id") or ""))
        if not state.get("enabled"):
            return True
        if not state.get("can_run"):
            return False
        if queued and event.get(_EVENT_RUN_ID_FIELD) != state.get("run_id"):
            return False
        timestamp = event.get("time")
        granted = state.get("run_granted_at", 0)
        return not (type(timestamp) in {int, float} and granted and timestamp < granted)

    def queue_event(self, event: dict) -> dict | None:
        if not self.allow_event(event):
            return None
        value = dict(event)
        if value.get("post_type") not in {None, "", "meta_event"}:
            state = self.status(str(value.get("self_id") or ""))
            if state.get("enabled"):
                if not state.get("can_run"):
                    return None
                value[_EVENT_RUN_ID_FIELD] = state.get("run_id", "")
        return value

    def delivery_event(self, event: dict) -> dict | None:
        if not self.allow_event(event, queued=True):
            return None
        return {key: value for key, value in event.items() if key != _EVENT_RUN_ID_FIELD}

    def require_hook(self, path: str) -> None:
        if path not in _READ_HOOK_PATHS:
            self.require(session=_action_session.get())

    def refresh_once(self) -> dict:
        # A network request can cross expiry or a login change. Always re-read
        # the actual account and local state after it finishes.
        self.client.refresh_if_due()
        return self.status()

    def start_refresh(self) -> None:
        try:
            if not load_channel(self._channel_root).enabled:
                return
        except LicenseError:
            return
        with self._refresh_lock:
            if self._refresh_thread is not None and self._refresh_thread.is_alive():
                return
            self._refresh_stop.clear()

            def worker():
                while not self._refresh_stop.is_set():
                    try:
                        self.refresh_once()
                    except (OSError, ValueError, TypeError) as exc:
                        _LOG.debug("Runtime refresh deferred (%s)", type(exc).__name__)
                    self._refresh_stop.wait(2)

            self._refresh_thread = threading.Thread(target=worker, name="license-refresh", daemon=True)
            self._refresh_thread.start()

    def stop_refresh(self) -> None:
        self._refresh_stop.set()


_guard: RuntimeGuard | None = None
_guard_lock = threading.Lock()


def runtime_guard() -> RuntimeGuard:
    global _guard
    with _guard_lock:
        if _guard is None:
            _guard = RuntimeGuard()
        return _guard


def bind_account_source(source: Callable[[], str]) -> None:
    runtime_guard().account_source = source


def guard_bridge_action(function):
    """Keep a request's original permission across downloads, locks and child tasks."""
    @wraps(function)
    async def action(self, request, *args, **kwargs):
        guard = runtime_guard()
        inherited = _action_session.get()
        if inherited is None:
            state = guard.status()
            session = state.get("run_id", "") if state.get("can_run") else ""
        else:
            session = inherited
        if request.get("action") not in _READ_ONEBOT_ACTIONS:
            guard.require(session=session)
        token = _action_session.set(session)
        try:
            return await function(self, request, *args, **kwargs)
        finally:
            _action_session.reset(token)
    return action


def guard_provider_class(cls: type) -> None:
    """Wrap real provider overrides, including streaming and plugin subclasses."""
    for name in _PROVIDER_CALLS:
        function = cls.__dict__.get(name)
        if function is None or getattr(function, "_wechatrebot_guarded", False):
            continue
        if inspect.isasyncgenfunction(function):
            @wraps(function)
            async def stream(*args, __call=function, **kwargs):
                guard = runtime_guard()
                session = guard.require_core().get("run_id", "")
                generator = __call(*args, **kwargs)
                try:
                    while True:
                        guard.require(session=session)
                        try:
                            chunk = await generator.__anext__()
                        except StopAsyncIteration:
                            break
                        guard.require(session=session)
                        yield chunk
                finally:
                    await generator.aclose()
            wrapped = stream
        elif inspect.iscoroutinefunction(function):
            @wraps(function)
            async def call(*args, __call=function, **kwargs):
                guard = runtime_guard()
                session = guard.require_core().get("run_id", "")
                result = await __call(*args, **kwargs)
                guard.require(session=session)
                return result
            wrapped = call
        else:
            continue
        wrapped._wechatrebot_guarded = True
        setattr(cls, name, wrapped)


async def watch_core(lifecycle, *, interval: float = 1.0) -> None:
    """Expiry checks never wait behind the independent network refresh thread."""
    guard = runtime_guard()
    while True:
        try:
            state = await asyncio.to_thread(guard.require_core)
            if not state.get("enabled"):
                return
        except LicenseError:
            lifecycle._license_stopping = True
            _LOG.warning("Subscription unavailable; stopping automation and retaining WeChat/data.")
            pending = list(getattr(getattr(lifecycle, "event_bus", None), "_pending_tasks", ()))
            for task in pending:
                task.cancel()
            if pending:
                try:
                    await asyncio.wait_for(asyncio.gather(*pending, return_exceptions=True), timeout=5)
                except TimeoutError:
                    _LOG.warning("Some pipeline tasks did not finish cancellation promptly.")
            try:
                await asyncio.wait_for(lifecycle.stop(), timeout=20)
            except TimeoutError:
                _LOG.error("Core shutdown timed out; authorization guards remain closed.")
                shutdown = getattr(lifecycle, "dashboard_shutdown_event", None)
                if shutdown is not None:
                    shutdown.set()
            finally:
                guard.stop_refresh()
            return
        await asyncio.sleep(interval)


async def finish_core_watch(lifecycle, watcher: asyncio.Task) -> None:
    if not getattr(lifecycle, "_license_stopping", False):
        watcher.cancel()
    await asyncio.gather(watcher, return_exceptions=True)
    runtime_guard().stop_refresh()
