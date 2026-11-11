"""Small authenticated bridge to the manager's current-group plugin controls."""
from __future__ import annotations

import asyncio
from http.client import HTTPException
import json
from pathlib import Path
import re
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


_MODULE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_GROUP_ID = re.compile(r"^[A-Za-z0-9_.-]{1,119}@chatroom$")
# Only a cheap read-only lookup hint; the manager owns intent authorization.
_CONTROL_MESSAGE_HINT = re.compile(r"开启|启用|打开|启动|恢复|开起来|打开来|关闭|禁用|停用|关掉|停掉|关了|关起来")
_MAX_DESCRIPTOR_BYTES = 16 * 1024
_MAX_RESPONSE_BYTES = 1024 * 1024
_TIMEOUT_SECONDS = 10.0
_CONTROL_FILE = Path("data/manager/plugin-control.json")


class PluginControlError(RuntimeError):
    """A safe, user-facing failure from the local manager bridge."""


def _install_root() -> Path:
    path = Path(__file__).resolve()
    # Installed payload: <root>/app/astrbot/data/plugins/<plugin>/file.py.
    if [parent.name.lower() for parent in path.parents[1:5]] == [
        "plugins", "data", "astrbot", "app",
    ]:
        return path.parents[5]
    # Source checkout tests and development runs.
    return path.parents[2]


def _read_descriptor() -> tuple[str, str]:
    path = _install_root() / _CONTROL_FILE
    try:
        if path.stat().st_size > _MAX_DESCRIPTOR_BYTES:
            raise PluginControlError("插件管理桥接配置过大。")
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError as exc:
        raise PluginControlError("软件管理接口未就绪，请先打开管理器。") from exc
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as exc:
        raise PluginControlError("软件管理接口配置无效，请重启管理器后重试。") from exc
    if not isinstance(raw, dict):
        raise PluginControlError("软件管理接口配置无效，请重启管理器后重试。")
    url, token = raw.get("url"), raw.get("token")
    try:
        parsed = urlsplit(url) if isinstance(url, str) else None
        port = parsed.port if parsed else None
        valid = (
            port is not None and 1 <= port <= 65535
            and url == f"http://127.0.0.1:{port}/internal/plugin-control"
            and isinstance(token, str) and 16 <= len(token) <= 512
            and all(33 <= ord(char) <= 126 for char in token)
        )
    except ValueError:
        valid = False
    if not valid:
        raise PluginControlError("软件管理接口配置无效，请重启管理器后重试。")
    return url, token


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request(payload: dict[str, object]) -> dict[str, object]:
    url, token = _read_descriptor()
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        try:
            response = opener.open(request, timeout=_TIMEOUT_SECONDS)
        except HTTPError as exc:
            response = exc
        with response:
            raw = response.read(_MAX_RESPONSE_BYTES + 1)
            status = response.status
    except (URLError, TimeoutError, OSError, HTTPException) as exc:
        raise PluginControlError(
            "未能确认软件管理接口的处理结果，请确认管理器正在运行，并重新查询插件状态。"
        ) from exc
    if len(raw) > _MAX_RESPONSE_BYTES:
        raise PluginControlError("软件管理接口返回内容过大。")
    try:
        result = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PluginControlError("软件管理接口返回了无法识别的数据。") from exc
    if not isinstance(result, dict):
        raise PluginControlError("软件管理接口返回格式无效。")
    if result.get("ok") is not True or not 200 <= status < 300:
        message = str(result.get("message") or "操作未完成，请稍后重试。")
        raise PluginControlError(message.replace(token, "[已隐藏]")[:300])
    data = result.get("data")
    if not isinstance(data, dict):
        raise PluginControlError("软件管理接口返回格式无效。")
    return data


async def request_control(payload: dict[str, object]) -> dict[str, object]:
    """Call the loopback manager without blocking AstrBot's event loop."""
    return await asyncio.to_thread(_request, payload)


def is_configured_admin(context, event) -> bool:
    """Require the exact configured WeChat administrator identity."""
    try:
        if str(event.get_platform_name() or "") != "aiocqhttp":
            return False
        sender_id = event.get_sender_id()
        config = context.get_config()
        admins = config.get("admins_id") if isinstance(config, dict) else None
        return bool(isinstance(sender_id, str) and sender_id and isinstance(admins, list) and sender_id in {
            item.strip() for item in admins if isinstance(item, str) and item.strip()
        })
    except (AttributeError, TypeError, ValueError):
        return False


def current_group_scope(context, event) -> dict[str, str] | None:
    """Bind management to the actual group event and a configured administrator."""
    if not is_configured_admin(context, event):
        return None
    try:
        if event.is_private_chat():
            return None
        account_id, group_id = event.get_self_id(), event.get_group_id()
        if not (isinstance(account_id, str) and _ACCOUNT_ID.fullmatch(account_id)
                and isinstance(group_id, str) and _GROUP_ID.fullmatch(group_id)):
            return None
        return {
            "scope": "current_group", "account_id": account_id,
            "group_id": group_id, "sender_id": event.get_sender_id(),
        }
    except (AttributeError, TypeError, ValueError):
        return None


def matches_group_scope(data: dict[str, object], scope: dict[str, str]) -> bool:
    return all(data.get(key) == scope[key] for key in ("scope", "account_id", "group_id"))


def control_message_candidate(message: str) -> bool:
    return bool(isinstance(message, str) and len(message) <= 4000
                and not message.lstrip().startswith(("/", "\\"))
                and _CONTROL_MESSAGE_HINT.search(message))


def valid_module_name(module: str) -> bool:
    return bool(_MODULE_NAME.fullmatch(str(module or "").strip()))
