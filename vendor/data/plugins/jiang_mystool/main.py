# -*- coding: utf-8 -*-
"""nonebot-plugin-mystool v2.10.2 的 AstrBot 适配层。"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import httpx
import importlib.util
import io
import json
import logging
import os
import random
import string
import sys
import tempfile
import time
import types
from dataclasses import dataclass
from datetime import datetime, time as datetime_time
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, MessageChain, filter


logger = logging.getLogger("jiang_mystool")
if not hasattr(logger, "success"):
    logger.success = logger.info  # type: ignore[attr-defined]

DISPLAY_PREFIX = "/"
_PLUGIN_ROOT = Path(__file__).resolve().parents[3]
_MYSTOOL_SRC = _PLUGIN_ROOT / "nonebot-plugin-mystool" / "src" / "nonebot_plugin_mystool"
_PLUGINS_DIR = Path(__file__).resolve().parent.parent
_LOGIN_STATE_PATH = _PLUGIN_ROOT / "data" / "nonebot-plugin-mystool" / "login_state.json"
_MISSION_CACHE_PATH = _PLUGIN_ROOT / "data" / "nonebot-plugin-mystool" / "mission_cache.json"
_MISSION_SCHEDULE_PATH = (
    _PLUGIN_ROOT / "data" / "nonebot-plugin-mystool" / "mission_schedule.json"
)
_MISSION_DAILY_TIME = datetime_time(hour=0, minute=1)
_MISSION_TIMEZONE = ZoneInfo("Asia/Shanghai")
_MISSION_NETWORK_ATTEMPTS = 3
_MISSION_AUTO_ATTEMPTS = 5
_MISSION_AUTO_RETRY_SECONDS = 120
_TARGET_PLATFORM_ID = "aiocqhttp_hook41827"
_PASSPORT_QR_CREATE = (
    "https://passport-api.miyoushe.com/account/ma-cn-passport/app/createQRLogin"
)
_PASSPORT_QR_QUERY = (
    "https://passport-api.miyoushe.com/account/ma-cn-passport/app/queryQRLoginStatus"
)
_PASSPORT_QR_APP_ID = "bll8iq97cem8"

if str(_PLUGINS_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGINS_DIR))
import _nonebot_stubs

_nonebot_stubs.setup()


def _ensure_packages() -> None:
    for package in (
        "nonebot_plugin_mystool",
        "nonebot_plugin_mystool.model",
        "nonebot_plugin_mystool.utils",
        "nonebot_plugin_mystool.api",
    ):
        if package in sys.modules:
            continue
        module = types.ModuleType(package)
        module.__package__ = package
        module.__path__ = [str(_MYSTOOL_SRC / package.replace(".", os.sep))]
        sys.modules[package] = module


def _load_module(name: str, relative_path: str, *, force: bool = False):
    if name in sys.modules and not force:
        return sys.modules[name]
    path = _MYSTOOL_SRC / relative_path
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"无法加载模块 {name}: {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_utils_bridge() -> None:
    """只提供业务 API 所需工具，避免加载 NoneBot 适配器依赖。"""
    import uuid
    import tenacity

    common = types.ModuleType("nonebot_plugin_mystool.utils.common")
    common.__package__ = "nonebot_plugin_mystool.utils"
    common.__all__ = [
        "generate_device_id",
        "generate_ds",
        "get_async_retry",
        "generate_seed_id",
        "generate_fp_locally",
        "cookie_str_to_dict",
        "cookie_dict_to_str",
        "blur_phone",
        "get_validate",
        "logger",
        "COMMAND_BEGIN",
    ]

    def generate_device_id() -> str:
        return str(uuid.uuid4()).upper()

    def generate_ds(data=None, params=None, platform="ios", salt=None) -> str:
        from nonebot_plugin_mystool.model.config import plugin_env

        if (data is None and params is None) or (
            salt is not None and salt != plugin_env.salt_config.SALT_PROD
        ):
            salt = salt or (
                plugin_env.salt_config.SALT_IOS
                if platform == "ios"
                else plugin_env.salt_config.SALT_ANDROID
            )
            timestamp = str(int(time.time()))
            nonce = "".join(random.sample(string.ascii_lowercase + string.digits, 6))
            digest = hashlib.md5(
                f"salt={salt}&t={timestamp}&r={nonce}".encode()
            ).hexdigest()
            return f"{timestamp},{nonce},{digest}"

        salt = (
            (plugin_env.salt_config.SALT_PARAMS if not salt else salt)
            if params
            else (plugin_env.salt_config.SALT_DATA if not salt else salt)
        )
        if not data:
            data = {} if salt == plugin_env.salt_config.SALT_PROD else ""
        if not params:
            params = ""
        if not isinstance(data, str):
            data = json.dumps(data, separators=(",", ":"))
        if not isinstance(params, str):
            from urllib.parse import urlencode

            params = urlencode(params)
        timestamp = str(int(time.time()))
        nonce = str(random.randint(100000, 200000))
        digest = hashlib.md5(
            f"salt={salt}&t={timestamp}&r={nonce}&b={data}&q={params}".encode()
        ).hexdigest()
        return f"{timestamp},{nonce},{digest}"

    def get_async_retry(retry: bool):
        from nonebot_plugin_mystool.model.config import plugin_config

        return tenacity.AsyncRetrying(
            stop=tenacity.stop_after_attempt(
                plugin_config.preference.max_retry_times if retry else 1
            ),
            retry=tenacity.retry_if_exception_type(Exception),
            wait=tenacity.wait_fixed(plugin_config.preference.retry_interval),
        )

    def generate_seed_id(length: int = 8) -> str:
        return f"{random.randint(0, int('FF' * length, 16)):x}"

    def generate_fp_locally(length: int = 13) -> str:
        return "".join(random.choices(string.digits + "abcdef", k=length))

    def cookie_str_to_dict(cookie_str: str) -> dict[str, str]:
        result: dict[str, str] = {}
        for part in cookie_str.split(";"):
            if "=" in part:
                key, value = part.strip().split("=", 1)
                result[key] = value
        return result

    def cookie_dict_to_str(cookies: dict[str, str]) -> str:
        return "".join(f"{key}={value};" for key, value in cookies.items())

    def blur_phone(phone: str | None):
        if not phone or len(phone) < 7:
            return phone
        return phone[:3] + "****" + phone[-4:]

    async def get_validate(user, gt=None, challenge=None):
        from nonebot_plugin_mystool.model.common import GeetestResult

        return GeetestResult(validate="", seccode="")

    common.generate_device_id = generate_device_id
    common.generate_ds = generate_ds
    common.get_async_retry = get_async_retry
    common.generate_seed_id = generate_seed_id
    common.generate_fp_locally = generate_fp_locally
    common.cookie_str_to_dict = cookie_str_to_dict
    common.cookie_dict_to_str = cookie_dict_to_str
    common.blur_phone = blur_phone
    common.get_validate = get_validate
    common.logger = logger
    common.COMMAND_BEGIN = "/"
    sys.modules[common.__name__] = common

    package = types.ModuleType("nonebot_plugin_mystool.utils")
    package.__package__ = "nonebot_plugin_mystool.utils"
    package.__path__ = [str(_MYSTOOL_SRC / "utils")]
    for name in common.__all__:
        setattr(package, name, getattr(common, name))
    sys.modules[package.__name__] = package


def _load_mystool() -> None:
    _ensure_packages()
    _load_module("nonebot_plugin_mystool._version", "_version.py")
    _load_module("nonebot_plugin_mystool.model.common", "model/common.py")
    _load_module("nonebot_plugin_mystool.model.config", "model/config.py")
    _load_module("nonebot_plugin_mystool.model.data", "model/data.py")
    _load_module("nonebot_plugin_mystool.model", "model/__init__.py", force=True)
    _install_utils_bridge()
    _load_module("nonebot_plugin_mystool.api.common", "api/common.py")
    _load_module("nonebot_plugin_mystool.api.game_sign_api", "api/game_sign_api.py")
    _load_module(
        "nonebot_plugin_mystool.api.myb_missions_api", "api/myb_missions_api.py"
    )
    _load_module("nonebot_plugin_mystool.api", "api/__init__.py", force=True)


_load_mystool()

from nonebot_plugin_mystool.api.common import (  # noqa: E402
    genshin_note,
    get_address,
    get_cookie_token_by_stoken,
    get_device_fp,
    get_game_record,
    get_good_detail,
    get_good_list,
    get_ltoken_by_stoken,
    starrail_note,
)
from nonebot_plugin_mystool.api.game_sign_api import BaseGameSign  # noqa: E402
from nonebot_plugin_mystool.api.myb_missions_api import (  # noqa: E402
    BaseMission,
    get_missions_state,
)
from nonebot_plugin_mystool.model.config import plugin_config  # noqa: E402
from nonebot_plugin_mystool.model.data import (  # noqa: E402
    BBSCookies,
    ExchangePlan,
    PluginDataManager,
    UserAccount,
    UserData,
)
from nonebot_plugin_mystool.utils.common import generate_device_id  # noqa: E402
from qrcode import QRCode  # noqa: E402


_DATA_LOCK = asyncio.Lock()
_MISSION_CACHE_LOCK = asyncio.Lock()
_MISSION_USER_LOCKS: dict[str, asyncio.Lock] = {}


@dataclass
class MissionRunResult:
    messages: list[str]
    complete: bool
    network_failed: bool = False
    requires_user_action: bool = False


def _result_has_network_error(result: Any) -> bool:
    status = result[0] if isinstance(result, tuple) and result else result
    return bool(getattr(status, "network_error", False))


async def _call_with_network_retry(operation, label: str) -> tuple[Any, bool]:
    """执行网络操作，网络失败时最多尝试三次（含首次）。"""
    last_result: Any = None
    last_error: Exception | None = None
    for attempt in range(1, _MISSION_NETWORK_ATTEMPTS + 1):
        try:
            last_result = await operation()
            last_error = None
        except (httpx.TransportError, TimeoutError, OSError) as error:
            last_result = None
            last_error = error
        if last_error is None and not _result_has_network_error(last_result):
            return last_result, False
        logger.warning(
            "[Mystool] %s 网络失败，准备重试: attempt=%s/%s error=%s",
            label,
            attempt,
            _MISSION_NETWORK_ATTEMPTS,
            type(last_error).__name__ if last_error else "network_error",
        )
        if attempt < _MISSION_NETWORK_ATTEMPTS:
            await asyncio.sleep(float(plugin_config.preference.retry_interval or 2))
    return last_result, True


def _read_mission_cache() -> dict[str, Any]:
    today = time.strftime("%Y-%m-%d")
    try:
        payload = json.loads(_MISSION_CACHE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {"date": today, "users": {}}
    if payload.get("date") != today or not isinstance(payload.get("users"), dict):
        return {"date": today, "users": {}}
    return payload


def _write_mission_cache(payload: dict[str, Any]) -> None:
    _MISSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _MISSION_CACHE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(_MISSION_CACHE_PATH)


def _read_mission_schedule() -> dict[str, Any]:
    try:
        payload = json.loads(_MISSION_SCHEDULE_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, ValueError, TypeError):
        return {"users": {}}
    if not isinstance(payload.get("users"), dict):
        return {"users": {}}
    return payload


def _write_mission_schedule(payload: dict[str, Any]) -> None:
    _MISSION_SCHEDULE_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = _MISSION_SCHEDULE_PATH.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    temporary.replace(_MISSION_SCHEDULE_PATH)


def _private_umo(event: AstrMessageEvent, user_id: str) -> str:
    origin = str(getattr(event, "unified_msg_origin", "") or "")
    platform_id = origin.split(":", 1)[0].strip() if ":" in origin else ""
    return f"{platform_id or _TARGET_PLATFORM_ID}:FriendMessage:{user_id}"


def _normalize_mission_umo(umo: Any, user_id: str) -> str:
    """将历史旧桥目标迁移到新版桥，保留已明确配置的其他平台。"""
    value = str(umo or "").strip()
    if not value:
        return f"{_TARGET_PLATFORM_ID}:FriendMessage:{user_id}"
    if value.startswith("aiocqhttp:FriendMessage:"):
        return f"{_TARGET_PLATFORM_ID}:FriendMessage:{value.rsplit(':', 1)[-1]}"
    return value


async def _get_cached_missions(
    user_id: str, account_id: str, mission_class_name: str
) -> set[str]:
    async with _MISSION_CACHE_LOCK:
        payload = _read_mission_cache()
        account_id = str(account_id)
        completed = (
            payload.get("users", {})
            .get(user_id, {})
            .get(account_id, {})
            .get(mission_class_name, [])
        )
        return {str(item) for item in completed}


async def _mark_cached_mission(
    user_id: str, account_id: str, mission_class_name: str, mission_key: str
) -> None:
    async with _MISSION_CACHE_LOCK:
        payload = _read_mission_cache()
        account_id = str(account_id)
        users = payload.setdefault("users", {})
        accounts = users.setdefault(user_id, {})
        partitions = accounts.setdefault(account_id, {})
        completed = partitions.setdefault(mission_class_name, [])
        if mission_key not in completed:
            completed.append(mission_key)
            try:
                _write_mission_cache(payload)
            except OSError:
                logger.exception("[Mystool] 写入米游币任务缓存失败")


def _save_data() -> bool:
    return bool(PluginDataManager.write_plugin_data())


def _get_user(user_id: str):
    return PluginDataManager.plugin_data.users.get(user_id)


def _has_accounts(user) -> bool:
    return bool(user and user.accounts)


def _extract_command(text: str):
    cleaned = str(text or "").strip()
    if cleaned.startswith("[系统身份提示：") and "]\n" in cleaned:
        cleaned = cleaned.split("]\n", 1)[1].lstrip()
    if not cleaned or cleaned[0] not in ("/", "\\"):
        return None
    body = cleaned[1:].strip()
    if not body:
        return None
    return body.split(None, 1)[0], body


def _extract_event_command(event: AstrMessageEvent):
    """从未被 AstrBot 唤醒阶段裁掉 `/` 的原始消息中解析命令。"""
    message_obj = getattr(event, "message_obj", None)
    candidates = [
        getattr(message_obj, "message_str", ""),
        event.get_message_outline(),
        event.get_message_str(),
    ]
    seen: set[str] = set()
    for candidate in candidates:
        text = str(candidate or "")
        if not text or text in seen:
            continue
        seen.add(text)
        if parsed := _extract_command(text):
            return parsed
    return None


def _qr_image(data: str) -> bytes:
    qr = QRCode(border=2)
    qr.add_data(data)
    qr.make()
    image = qr.make_image()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _passport_qr_headers(device_id: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0",
        "x-rpc-app_id": _PASSPORT_QR_APP_ID,
        "x-rpc-client_type": "2",
        "x-rpc-device_id": device_id,
    }


async def _create_passport_qrcode(
    device_id: str,
) -> tuple[str | None, str | None, str]:
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                _PASSPORT_QR_CREATE,
                headers=_passport_qr_headers(device_id),
                json={},
                timeout=plugin_config.preference.timeout,
            )
        payload = response.json()
    except (httpx.HTTPError, ValueError) as error:
        return None, None, f"{type(error).__name__}: {error}"
    if response.status_code != 200 or payload.get("retcode") != 0:
        return None, None, (
            f"HTTP {response.status_code}, retcode={payload.get('retcode')}, "
            f"message={payload.get('message') or '未知错误'}"
        )
    data = payload.get("data") or {}
    qrcode_url = data.get("url")
    ticket = data.get("ticket")
    if not qrcode_url or not ticket:
        return None, None, "服务器响应缺少二维码地址或 ticket"
    return str(qrcode_url), str(ticket), ""


async def _query_passport_qrcode(
    ticket: str, device_id: str
) -> tuple[str, dict[str, Any] | None, str]:
    try:
        async with httpx.AsyncClient(trust_env=False) as client:
            response = await client.post(
                _PASSPORT_QR_QUERY,
                headers=_passport_qr_headers(device_id),
                json={"ticket": ticket},
                timeout=plugin_config.preference.timeout,
            )
        payload = response.json()
    except httpx.HTTPError as error:
        return "NetworkError", None, f"{type(error).__name__}: {error}"
    except ValueError as error:
        return "InvalidResponse", None, f"响应不是有效 JSON：{error}"

    retcode = payload.get("retcode")
    if retcode == -3501:
        return "Expired", None, payload.get("message") or "二维码已过期"
    if retcode == -3505:
        return "Cancelled", None, payload.get("message") or "手机端已取消登录"
    if response.status_code != 200 or retcode != 0:
        return "InvalidResponse", None, (
            f"HTTP {response.status_code}, retcode={retcode}, "
            f"message={payload.get('message') or '未知错误'}"
        )

    data = payload.get("data") or {}
    status = str(data.get("status") or "Unknown")
    if status == "Created":
        status = "Init"
    return status, data, ""


async def _onebot_send(bot, group_id: str | None, user_id: str, message) -> bool:
    try:
        if group_id:
            await bot.call_action("send_group_msg", group_id=group_id, message=message)
        else:
            await bot.call_action("send_private_msg", user_id=user_id, message=message)
        return True
    except Exception:
        logger.exception("[Mystool] OneBot 消息发送失败")
        return False


async def _send_text(bot, group_id: str | None, user_id: str, text: str) -> bool:
    return await _onebot_send(
        bot, group_id, user_id, [{"type": "text", "data": {"text": text}}]
    )


async def _send_qrcode(
    bot, group_id: str | None, user_id: str, image_bytes: bytes, wait_seconds: int
) -> bool:
    image_data = base64.b64encode(image_bytes).decode("ascii")
    return await _onebot_send(
        bot,
        group_id,
        user_id,
        [
            {
                "type": "text",
                "data": {
                    "text": f"请用米游社 App 扫描二维码，并在 {wait_seconds} 秒内确认登录"
                },
            },
            {
                "type": "image",
                "data": {"file": f"base64://{image_data}"},
            },
        ],
    )


def _temporary_qrcode(event: AstrMessageEvent, image_bytes: bytes) -> str:
    descriptor, path = tempfile.mkstemp(prefix="mystool_login_", suffix=".png")
    with os.fdopen(descriptor, "wb") as output:
        output.write(image_bytes)
    tracker = getattr(event, "track_temporary_local_file", None)
    if callable(tracker):
        tracker(path)
    return path


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context
        self._login_tasks: dict[str, asyncio.Task] = {}
        self._login_states: dict[str, dict[str, Any]] = {}
        self._mission_schedule = _read_mission_schedule()
        self._mission_flow_tasks: dict[str, asyncio.Task] = {}
        self._mission_scheduler_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        changed = False
        users = self._mission_schedule.setdefault("users", {})
        for user_id, user in PluginDataManager.plugin_data.users.items():
            if not _has_accounts(user):
                continue
            record = users.get(str(user_id))
            if not isinstance(record, dict):
                record = {}
                users[str(user_id)] = record
                changed = True
            normalized_umo = _normalize_mission_umo(record.get("umo"), user_id)
            if record.get("umo") != normalized_umo:
                record["umo"] = normalized_umo
                changed = True
            if record.get("enabled") is not True:
                record["enabled"] = True
                changed = True
        if changed:
            self._save_mission_schedule()
        self._mission_scheduler_task = asyncio.create_task(
            self._daily_mission_scheduler(), name="mystool-daily-mission-scheduler"
        )

    def _save_mission_schedule(self) -> None:
        try:
            _write_mission_schedule(self._mission_schedule)
        except OSError:
            logger.exception("[Mystool] 写入米游币定时任务配置失败")

    def _register_mission_schedule(self, user_id: str, umo: str) -> None:
        users = self._mission_schedule.setdefault("users", {})
        record = users.get(user_id)
        if not isinstance(record, dict):
            record = {}
            users[user_id] = record
        record.update(
            {
                "umo": umo,
                "enabled": True,
                "registered_at": datetime.now(_MISSION_TIMEZONE).isoformat(
                    timespec="seconds"
                ),
            }
        )
        self._save_mission_schedule()

    async def _send_mission_private(self, umo: str, text: str) -> bool:
        try:
            result = await self.context.send_message(umo, MessageChain().message(text))
            if result is False:
                logger.warning("[Mystool] 定时任务私聊发送失败: umo=%s", umo)
                return False
            return True
        except Exception:
            logger.exception("[Mystool] 定时任务私聊发送异常: umo=%s", umo)
            return False

    def _mark_daily_started(self, user_id: str) -> None:
        users = self._mission_schedule.setdefault("users", {})
        record = users.get(user_id)
        if not isinstance(record, dict):
            record = {}
            users[user_id] = record
        record["last_daily_date"] = datetime.now(_MISSION_TIMEZONE).date().isoformat()
        self._save_mission_schedule()

    def _start_mission_flow(
        self, user_id: str, umo: str, reason: str, *, mark_daily: bool = False
    ) -> None:
        if mark_daily:
            self._mark_daily_started(user_id)
        current = self._mission_flow_tasks.get(user_id)
        if current and not current.done():
            logger.info(
                "[Mystool] 用户已有自动任务在运行，跳过重复启动: user=%s reason=%s",
                user_id,
                reason,
            )
            return
        task = asyncio.create_task(
            self._automatic_mission_flow(user_id, umo, reason),
            name=f"mystool-auto-mission-{user_id}",
        )
        self._mission_flow_tasks[user_id] = task
        task.add_done_callback(
            lambda finished, uid=user_id: self._mission_flow_done(uid, finished)
        )

    def _mission_flow_done(self, user_id: str, task: asyncio.Task) -> None:
        if self._mission_flow_tasks.get(user_id) is task:
            self._mission_flow_tasks.pop(user_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "[Mystool] 自动米游币任务异常: user=%s error=%s",
                user_id,
                error,
                exc_info=error,
            )

    async def _automatic_mission_flow(
        self, user_id: str, umo: str, reason: str
    ) -> None:
        for attempt in range(1, _MISSION_AUTO_ATTEMPTS + 1):
            try:
                result = await _run_missions(user_id)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                logger.exception(
                    "[Mystool] 自动任务单轮执行异常: user=%s attempt=%s",
                    user_id,
                    attempt,
                )
                result = MissionRunResult(
                    messages=[f"任务执行异常：{type(error).__name__}"],
                    complete=False,
                )
            heading = (
                reason
                if result.complete and attempt == 1
                else f"{reason}（第 {attempt}/{_MISSION_AUTO_ATTEMPTS} 次）"
            )
            text = f"{heading}\n\n" + "\n\n".join(result.messages)
            if result.complete:
                await self._send_mission_private(umo, text)
                return
            if result.requires_user_action:
                text += (
                    "\n\n⚠️ 米游社要求人机验证，本轮自动补跑已停止。"
                    "请打开米游社 App 手动完成“打卡”，或稍后私聊发送 /任务 重试。"
                )
                await self._send_mission_private(umo, text)
                return
            if attempt < _MISSION_AUTO_ATTEMPTS:
                text += "\n\n⚠️ 尚未全部完成，2 分钟后自动重试。"
                await self._send_mission_private(umo, text)
                await asyncio.sleep(_MISSION_AUTO_RETRY_SECONDS)
                continue
            text += (
                "\n\n⚠️ 连续 5 次仍未全部完成，本轮自动任务已结束。"
                "请私聊发送 /任务 手动执行。"
            )
            await self._send_mission_private(umo, text)

    async def _daily_mission_scheduler(self) -> None:
        # 等待平台适配器完成连接，随后补跑当天 00:01 之后尚未执行的任务。
        await asyncio.sleep(10)
        while True:
            now = datetime.now(_MISSION_TIMEZONE)
            today = now.date().isoformat()
            if now.time() >= _MISSION_DAILY_TIME:
                for user_id, record in list(
                    self._mission_schedule.get("users", {}).items()
                ):
                    if not isinstance(record, dict):
                        continue
                    if not record.get("enabled") or record.get("last_daily_date") == today:
                        continue
                    umo = _normalize_mission_umo(record.get("umo"), user_id)
                    self._start_mission_flow(
                        str(user_id), umo, "每日 00:01 自动任务", mark_daily=True
                    )
            await asyncio.sleep(30)

    def _set_login_state(self, user_id: str, state: str, detail: str = "") -> None:
        record = {
            "state": state,
            "detail": detail,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._login_states[user_id] = record
        try:
            _LOGIN_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _LOGIN_STATE_PATH.write_text(
                json.dumps({"users": self._login_states}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("[Mystool] 写入登录状态文件失败")

    async def terminate(self):
        tasks = [
            *self._login_tasks.values(),
            *self._mission_flow_tasks.values(),
        ]
        if self._mission_scheduler_task:
            tasks.append(self._mission_scheduler_task)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._login_tasks.clear()
        self._mission_flow_tasks.clear()
        self._mission_scheduler_task = None

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def command_router(self, event: AstrMessageEvent):
        parsed = _extract_event_command(event)
        if not parsed:
            return
        command, command_text = parsed
        handlers = {
            "登录": self.login,
            "登录状态": self.login_status,
            "签到": self.sign,
            "任务": self.mission,
            "地址": self.address,
            "兑换": self.exchange,
            "商品": self.goods,
            "原神便签": self.genshin_note_command,
            "星穹铁道便签": self.starrail_note_command,
            "设置": self.settings,
            "账号设置": self.account_settings,
            "通知设置": self.notice_settings,
            "导出Cookies": self.export_cookies,
            "导出Cookie": self.export_cookies,
            "米游社帮助": self.help,
            "米游社": self.help,
        }
        handler = handlers.get(command)
        if handler is None:
            return
        event.stop_event()
        event.message_str = command_text
        logger.info(
            "[Mystool] command=%s sender=%s session=%s",
            command,
            event.get_sender_id(),
            event.unified_msg_origin,
        )
        try:
            async for result in handler(event):
                yield result
        except Exception as error:
            logger.exception("[Mystool] 命令 %s 执行失败", command)
            yield event.plain_result(f"米游社命令执行失败：{error}")

    async def login(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        active = self._login_tasks.get(user_id)
        if active and not active.done():
            active.cancel()
            self._set_login_state(user_id, "Cancelled", "新的登录命令已替换上一张二维码")

        device_id = generate_device_id()
        qrcode_url, ticket, reason = await _create_passport_qrcode(device_id)
        if not qrcode_url or not ticket:
            self._set_login_state(user_id, "FetchFailed", reason)
            yield event.plain_result(f"获取登录二维码失败：{reason}")
            return

        image_bytes = _qr_image(qrcode_url)
        wait_seconds = int(plugin_config.preference.qrcode_wait_time)
        group_id = event.get_group_id()
        bot = event.bot
        private_umo = _private_umo(event, user_id)

        # 登录二维码和后续状态始终私发给命令发起者，避免群内其他成员抢扫。
        sent = await _send_qrcode(bot, None, user_id, image_bytes, wait_seconds)
        if not sent:
            if group_id:
                self._set_login_state(
                    user_id,
                    "SendFailed",
                    "无法私发登录二维码，请先私聊机器人发送 /登录",
                )
                yield event.plain_result(
                    "无法私发登录二维码，请先私聊机器人发送 /登录。"
                    "为保护账户安全，二维码不会回退发送到群里。"
                )
                return
            path = _temporary_qrcode(event, image_bytes)
            yield event.plain_result(
                f"请用米游社 App 扫描二维码，并在 {wait_seconds} 秒内确认登录"
            )
            yield event.image_result(path)

        self._set_login_state(
            user_id,
            "QRCodeSent",
            "已发送米游社通行证登录二维码",
        )

        task = asyncio.create_task(
            self._finish_login(
                bot=bot,
                group_id=None,
                sender_id=user_id,
                ticket=ticket,
                device_id=device_id,
                private_umo=private_umo,
            ),
            name=f"mystool-login-{user_id}",
        )
        self._login_tasks[user_id] = task
        task.add_done_callback(
            lambda finished, uid=user_id: self._login_done(uid, finished)
        )
        if group_id:
            yield event.plain_result("登录二维码已私发给你，插件正在等待手机确认结果")

    async def login_status(self, event: AstrMessageEvent):
        user_id = event.get_sender_id()
        state = self._login_states.get(user_id)
        if state is None and _LOGIN_STATE_PATH.exists():
            try:
                saved = json.loads(_LOGIN_STATE_PATH.read_text(encoding="utf-8"))
                state = saved.get("users", {}).get(user_id)
            except Exception:
                logger.exception("[Mystool] 读取登录状态文件失败")
        if not state:
            yield event.plain_result("当前没有登录任务，请发送 /登录")
            return
        running = self._login_tasks.get(user_id)
        task_text = "运行中" if running and not running.done() else "已结束"
        yield event.plain_result(
            f"登录任务：{task_text}\n"
            f"状态：{state.get('state')}\n"
            f"说明：{state.get('detail') or '无'}\n"
            f"更新时间：{state.get('updated_at')}"
        )

    def _login_done(self, user_id: str, task: asyncio.Task) -> None:
        if self._login_tasks.get(user_id) is task:
            self._login_tasks.pop(user_id, None)
        if task.cancelled():
            return
        error = task.exception()
        if error:
            logger.error(
                "[Mystool] 登录后台任务异常: user=%s error=%s",
                user_id,
                error,
                exc_info=error,
            )

    async def _finish_login(
        self,
        *,
        bot,
        group_id: str | None,
        sender_id: str,
        ticket: str,
        device_id: str,
        private_umo: str,
    ) -> None:
        timeout = float(plugin_config.preference.qrcode_wait_time)
        interval = max(float(plugin_config.preference.qrcode_query_interval), 0.5)
        deadline = time.monotonic() + timeout
        last_state = ""
        scanned_notice_sent = False
        network_notice_sent = False
        try:
            while time.monotonic() < deadline:
                state, qr_result, query_detail = await _query_passport_qrcode(
                    ticket, device_id
                )

                if state != last_state:
                    logger.info(
                        "[Mystool] 二维码状态: user=%s state=%s",
                        sender_id,
                        state,
                    )
                    last_state = state
                    self._set_login_state(sender_id, state)

                if state == "Scanned" and not scanned_notice_sent:
                    scanned_notice_sent = True
                    self._set_login_state(sender_id, "Scanned", "已扫码，等待手机确认")
                    await _send_text(
                        bot, group_id, sender_id, "已检测到扫码，请在手机端确认登录"
                    )
                elif state in ("NetworkError", "InvalidResponse") and not network_notice_sent:
                    network_notice_sent = True
                    self._set_login_state(
                        sender_id,
                        state,
                        query_detail or "查询登录状态失败，正在继续重试",
                    )
                    await _send_text(
                        bot, group_id, sender_id, "查询登录状态暂时失败，插件正在继续重试"
                    )

                if state == "Confirmed" and qr_result:
                    self._set_login_state(sender_id, "Confirmed", "正在换取账户凭据")
                    await _send_text(
                        bot, group_id, sender_id, "手机确认成功，正在换取账户凭据"
                    )
                    user_info = qr_result.get("user_info") or {}
                    tokens = qr_result.get("tokens") or []
                    token_item = next(
                        (
                            item
                            for item in tokens
                            if isinstance(item, dict)
                            and item.get("token")
                            and str(item.get("token_type")) == "1"
                        ),
                        None,
                    ) or next(
                        (
                            item
                            for item in tokens
                            if isinstance(item, dict) and item.get("token")
                        ),
                        None,
                    )
                    bbs_uid = str(user_info.get("aid") or "")
                    mid = str(user_info.get("mid") or "")
                    stoken = str((token_item or {}).get("token") or "")
                    if not bbs_uid or not mid or not stoken:
                        missing = []
                        if not bbs_uid:
                            missing.append("账号ID")
                        if not mid:
                            missing.append("mid")
                        if not stoken:
                            missing.append("SToken")
                        message = f"手机确认成功，但响应缺少 {'、'.join(missing)}"
                    else:
                        message = await self._exchange_passport_tokens(
                            sender_id, bbs_uid, mid, stoken, device_id
                        )
                    self._set_login_state(sender_id, "Completed", message)
                    await _send_text(bot, group_id, sender_id, message)
                    if message.startswith("米游社账户") and message.endswith(
                        "绑定成功"
                    ):
                        self._register_mission_schedule(sender_id, private_umo)
                        self._start_mission_flow(
                            sender_id,
                            private_umo,
                            "登录后自动任务",
                            mark_daily=True,
                        )
                    return
                if state == "Expired":
                    self._set_login_state(
                        sender_id, "Expired", query_detail or "二维码已过期"
                    )
                    await _send_text(
                        bot, group_id, sender_id, "登录二维码已过期，请重新发送 /登录"
                    )
                    return
                if state == "Cancelled":
                    self._set_login_state(
                        sender_id, "Cancelled", query_detail or "手机端已取消登录"
                    )
                    await _send_text(
                        bot, group_id, sender_id, "手机端已取消登录，请重新发送 /登录"
                    )
                    return
                await asyncio.sleep(interval)

            self._set_login_state(
                sender_id,
                "Timeout",
                f"最后状态：{last_state or '未获取'}",
            )
            await _send_text(
                bot,
                group_id,
                sender_id,
                f"等待登录确认超时，最后状态：{last_state or '未获取'}，请重新发送 /登录",
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            logger.exception("[Mystool] 登录状态处理失败")
            self._set_login_state(
                sender_id,
                "Failed",
                f"{type(error).__name__}: {error}",
            )
            await _send_text(
                bot, group_id, sender_id, f"登录处理失败：{type(error).__name__}: {error}"
            )

    async def _exchange_passport_tokens(
        self,
        sender_id: str,
        bbs_uid: str,
        mid: str,
        stoken: str,
        device_id: str,
    ) -> str:
        cookies = BBSCookies(mid=mid)
        cookies.bbs_uid = bbs_uid
        cookies.stoken = stoken

        ltoken_status, ltoken_cookies = await get_ltoken_by_stoken(cookies, device_id)
        if ltoken_status and ltoken_cookies:
            cookies.update(ltoken_cookies)
        cookie_status, cookie_cookies = await get_cookie_token_by_stoken(
            cookies, device_id
        )
        if cookie_status and cookie_cookies:
            cookies.update(cookie_cookies)

        fp_status, device_fp = await get_device_fp(device_id)
        async with _DATA_LOCK:
            user = PluginDataManager.plugin_data.users.setdefault(
                sender_id, UserData()
            )
            account = user.accounts.get(bbs_uid)
            if account is None:
                account = UserAccount(
                    phone_number=None,
                    cookies=cookies,
                    device_id_ios=device_id,
                    device_id_android=device_id,
                )
                user.accounts[bbs_uid] = account
            else:
                account.cookies.update(cookies)
                account.device_id_ios = device_id
                account.device_id_android = device_id
            if fp_status:
                account.device_fp = device_fp
            _save_data()

        if cookies.stoken and cookies.cookie_token:
            return f"米游社账户 {bbs_uid} 绑定成功"
        missing = []
        if not cookies.stoken:
            missing.append("SToken")
        if not cookies.cookie_token:
            missing.append("CookieToken")
        return f"账户 {bbs_uid} 已确认，但缺少 {'、'.join(missing)}，请稍后重新登录"

    async def sign(self, event: AstrMessageEvent):
        messages = await _perform_game_sign(event.get_sender_id())
        yield event.plain_result("\n".join(messages))

    async def mission(self, event: AstrMessageEvent):
        messages = await _perform_missions(event.get_sender_id())
        yield event.plain_result("\n\n".join(messages))

    async def address(self, event: AstrMessageEvent):
        user = _get_user(event.get_sender_id())
        if not _has_accounts(user):
            yield event.plain_result("尚未绑定米游社账户，请先发送 /登录")
            return
        parts = event.get_message_str().split()
        account = _select_account(user, parts[1] if len(parts) > 1 else "")
        if account is None:
            yield event.plain_result(_account_selection_text(user, "地址"))
            return
        status, addresses = await get_address(account)
        if not status:
            yield event.plain_result("获取收货地址失败，请检查登录状态")
            return
        if not addresses:
            yield event.plain_result("当前账户没有收货地址")
            return
        lines = [f"账户 {account.display_name} 的收货地址："]
        for address in addresses:
            lines.append(
                f"ID {address.id}：{address.province_name}{address.city_name}"
                f"{address.county_name}{address.addr_ext}，{address.connect_name}，{address.phone}"
            )
        yield event.plain_result("\n".join(lines))

    async def goods(self, event: AstrMessageEvent):
        parts = event.get_message_str().split(maxsplit=1)
        game = parts[1] if len(parts) > 1 else ""
        status, goods = await get_good_list(game)
        if not status or not goods:
            yield event.plain_result("获取商品列表失败")
            return
        lines = [f"商品列表（显示前 30 项，共 {len(goods)} 项）："]
        for good in goods[:30]:
            lines.append(
                f"{good.general_name}｜ID {good.goods_id}｜{good.price} 米游币｜{good.time_text}"
            )
        yield event.plain_result("\n".join(lines))

    async def exchange(self, event: AstrMessageEvent):
        user = _get_user(event.get_sender_id())
        if not _has_accounts(user):
            yield event.plain_result("尚未绑定米游社账户，请先发送 /登录")
            return
        parts = event.get_message_str().split()
        if len(parts) == 1:
            if not user.exchange_plans:
                yield event.plain_result(
                    "暂无兑换计划。用法：/兑换 + 商品ID 或 /兑换 - 商品ID"
                )
                return
            lines = ["当前兑换计划："]
            for plan in user.exchange_plans:
                lines.append(
                    f"{plan.good.general_name}｜ID {plan.good.goods_id}｜账户 {plan.account.display_name}"
                )
            yield event.plain_result("\n".join(lines))
            return
        if len(parts) != 3 or parts[1] not in ("+", "-"):
            yield event.plain_result("用法：/兑换 + 商品ID 或 /兑换 - 商品ID")
            return
        operation, goods_id = parts[1], parts[2]
        status, good = await get_good_detail(goods_id)
        if not status or not good:
            yield event.plain_result(f"获取商品 {goods_id} 失败")
            return
        if operation == "+":
            account = next(iter(user.accounts.values()))
            address_status, addresses = await get_address(account)
            address = addresses[0] if address_status and addresses else None
            user.exchange_plans.add(
                ExchangePlan(good=good, address=address, account=account)
            )
            _save_data()
            yield event.plain_result(f"已添加兑换计划：{good.general_name}")
            return
        target = next(
            (
                plan
                for plan in user.exchange_plans
                if str(plan.good.goods_id) == str(goods_id)
            ),
            None,
        )
        if target is None:
            yield event.plain_result(f"没有商品 {goods_id} 的兑换计划")
            return
        user.exchange_plans.discard(target)
        _save_data()
        yield event.plain_result(f"已删除兑换计划：{goods_id}")

    async def genshin_note_command(self, event: AstrMessageEvent):
        user = _get_user(event.get_sender_id())
        if not _has_accounts(user):
            yield event.plain_result("尚未绑定米游社账户，请先发送 /登录")
            return
        lines: list[str] = []
        for account in user.accounts.values():
            status, note = await genshin_note(account)
            if not status or not note:
                lines.append(f"账户 {account.display_name} 获取原神便签失败")
                continue
            lines.extend(
                [
                    f"原神｜{account.display_name}",
                    f"树脂：{note.current_resin}",
                    f"洞天宝钱：{note.current_home_coin}/{note.max_home_coin}",
                    f"每日委托：{note.finished_task_num}/4",
                    f"探索派遣：{note.current_expedition_num}/{note.max_expedition_num}",
                ]
            )
        yield event.plain_result("\n".join(lines))

    async def starrail_note_command(self, event: AstrMessageEvent):
        user = _get_user(event.get_sender_id())
        if not _has_accounts(user):
            yield event.plain_result("尚未绑定米游社账户，请先发送 /登录")
            return
        lines: list[str] = []
        for account in user.accounts.values():
            status, note = await starrail_note(account)
            if not status or not note:
                lines.append(f"账户 {account.display_name} 获取星穹铁道便签失败")
                continue
            lines.extend(
                [
                    f"星穹铁道｜{account.display_name}",
                    f"开拓力：{note.current_stamina}/{note.max_stamina}",
                    f"每日实训：{note.current_train_score}/{note.max_train_score}",
                    f"模拟宇宙：{note.current_rogue_score}/{note.max_rogue_score}",
                    f"委托：{note.accepted_expedition_num}/{note.total_expedition_num}",
                ]
            )
        yield event.plain_result("\n".join(lines))

    async def settings(self, event: AstrMessageEvent):
        yield event.plain_result(
            "米游社设置：\n"
            "/账号设置 [账户ID] 平台 ios/android\n"
            "/账号设置 [账户ID] 签到 开启/关闭\n"
            "/账号设置 [账户ID] 任务 开启/关闭\n"
            "/通知设置 开启/关闭"
        )

    async def account_settings(self, event: AstrMessageEvent):
        user = _get_user(event.get_sender_id())
        if not _has_accounts(user):
            yield event.plain_result("尚未绑定米游社账户，请先发送 /登录")
            return
        parts = event.get_message_str().split()
        index = 1
        account = None
        if len(parts) > 1 and parts[1] in user.accounts:
            account = user.accounts[parts[1]]
            index = 2
        elif len(user.accounts) == 1:
            account = next(iter(user.accounts.values()))
        if account is None:
            yield event.plain_result(_account_selection_text(user, "账号设置"))
            return
        if len(parts) <= index + 1:
            yield event.plain_result(
                f"账户 {account.display_name}\n"
                f"平台：{account.platform}\n"
                f"游戏签到：{'开启' if account.enable_game_sign else '关闭'}\n"
                f"米游币任务：{'开启' if account.enable_mission else '关闭'}"
            )
            return
        item, value = parts[index], parts[index + 1]
        if item == "平台" and value in ("ios", "android"):
            account.platform = value
        elif item == "签到" and value in ("开启", "关闭"):
            account.enable_game_sign = value == "开启"
        elif item == "任务" and value in ("开启", "关闭"):
            account.enable_mission = value == "开启"
        else:
            yield event.plain_result("设置参数无效，请发送 /设置 查看用法")
            return
        _save_data()
        yield event.plain_result(f"账户 {account.display_name} 的{item}已设为 {value}")

    async def notice_settings(self, event: AstrMessageEvent):
        user = _get_user(event.get_sender_id())
        if not user:
            yield event.plain_result("尚未绑定米游社账户，请先发送 /登录")
            return
        parts = event.get_message_str().split()
        if len(parts) == 2 and parts[1] in ("开启", "关闭"):
            user.enable_notice = parts[1] == "开启"
            _save_data()
            yield event.plain_result(f"通知已{parts[1]}")
            return
        yield event.plain_result(
            f"通知当前为{'开启' if user.enable_notice else '关闭'}。用法：/通知设置 开启/关闭"
        )

    async def export_cookies(self, event: AstrMessageEvent):
        if event.get_group_id():
            yield event.plain_result("Cookies 包含敏感凭据，请私聊导出")
            return
        user = _get_user(event.get_sender_id())
        if not _has_accounts(user):
            yield event.plain_result("尚未绑定米游社账户")
            return
        lines: list[str] = []
        for uid, account in user.accounts.items():
            lines.append(f"账户 {uid}")
            lines.append(account.cookies.dict(v2_stoken=True, cookie_type=True))
        yield event.plain_result("\n".join(map(str, lines)))

    async def help(self, event: AstrMessageEvent):
        yield event.plain_result(
            "米游社命令：\n"
            "/登录　扫码绑定账户\n"
            "/登录状态　查看扫码处理进度\n"
            "/签到　执行游戏签到\n"
            "/任务　手动执行米游币任务（登录后及每日 00:01 自动执行）\n"
            "/地址 [账户ID]　查看收货地址\n"
            "/商品 [游戏]　查看商品\n"
            "/兑换 +/- 商品ID　管理兑换计划\n"
            "/原神便签　查看原神便签\n"
            "/星穹铁道便签　查看星铁便签\n"
            "/设置　查看设置用法\n"
            "/导出Cookies　私聊导出凭据\n"
            "/米游社帮助　显示本帮助"
        )


def _select_account(user, requested_uid: str):
    if requested_uid:
        return user.accounts.get(requested_uid)
    if len(user.accounts) == 1:
        return next(iter(user.accounts.values()))
    return None


def _account_selection_text(user, command: str) -> str:
    accounts = "、".join(user.accounts.keys())
    return f"存在多个账户：{accounts}。请发送 /{command} 账户ID"


async def _perform_game_sign(user_id: str) -> list[str]:
    user = _get_user(user_id)
    if not _has_accounts(user):
        return ["尚未绑定米游社账户，请先发送 /登录"]
    messages: list[str] = []
    for account in user.accounts.values():
        status, records = await get_game_record(account)
        if not status or not records:
            messages.append(f"账户 {account.display_name} 获取游戏角色失败")
            continue
        for sign_class in sorted(BaseGameSign.available_game_signs, key=lambda item: item.name):
            if sign_class.en_name not in account.game_sign_games:
                continue
            signer = sign_class(account, records)
            if not signer.has_record:
                continue
            info_status, info = await signer.get_info(account.platform)
            if not info_status or not info:
                messages.append(f"{sign_class.name}：获取签到状态失败")
                continue
            if info.is_sign:
                messages.append(f"{sign_class.name}：今日已签到")
                continue
            sign_status, verification = await signer.sign(account.platform)
            if sign_status:
                messages.append(f"{sign_class.name}：签到成功")
            elif sign_status.need_verify or verification:
                messages.append(f"{sign_class.name}：需要人机验证，未完成签到")
            else:
                messages.append(f"{sign_class.name}：签到失败")
    return messages or ["没有可签到的游戏角色"]


def _mission_status_reason(status: Any) -> str:
    if getattr(status, "login_expired", False):
        return "登录凭据已失效，请重新发送 /登录"
    if getattr(status, "need_verify", False):
        return "触发人机验证"
    if getattr(status, "invalid_ds", False):
        return "请求校验失败"
    if getattr(status, "failed_getting_post", False):
        return "获取帖子失败"
    if getattr(status, "network_error", False):
        return "网络连接失败"
    if getattr(status, "incorrect_return", False):
        return "服务器响应异常"
    return "执行失败"


def _mission_finished_by_state(state: Any, mission_key: str) -> bool:
    entry = state.state_dict.get(mission_key)
    if not entry:
        return False
    mission_data, current = entry
    return current >= mission_data.threshold


async def _perform_missions_locked(user_id: str) -> MissionRunResult:
    user = _get_user(user_id)
    if not _has_accounts(user):
        return MissionRunResult(
            messages=["尚未绑定米游社账户，请先发送 /登录"], complete=False
        )
    messages: list[str] = []
    all_complete = True
    any_network_failure = False
    requires_user_action = False
    for account_id, account in user.accounts.items():
        state_result, state_network_failed = await _call_with_network_retry(
            lambda: get_missions_state(account, retry=False),
            f"账户 {account.display_name} 获取任务状态",
        )
        any_network_failure = any_network_failure or state_network_failed
        if not state_result:
            all_complete = False
            messages.append(
                "\n".join(
                    [
                        "【米游币任务｜⚠️ 未完成】",
                        f"账户：{account.display_name}",
                        "",
                        "❌ 获取任务状态　网络连接失败",
                    ]
                )
            )
            continue
        state_status, before = state_result
        if not state_status or not before:
            all_complete = False
            reason = _mission_status_reason(state_status)
            messages.append(
                "\n".join(
                    [
                        "【米游币任务｜⚠️ 未完成】",
                        f"账户：{account.display_name}",
                        "",
                        f"❌ 获取任务状态　{reason}",
                    ]
                )
            )
            continue

        account_lines: list[str] = []
        account_success = True
        for class_name in account.mission_games:
            mission_class = BaseMission.available_games.get(class_name)
            if mission_class is None:
                account_lines.append(f"〔{class_name}〕")
                account_lines.append("❌ 任务分区不存在")
                account_success = False
                continue
            mission = mission_class(account)
            cached = await _get_cached_missions(user_id, account_id, class_name)
            if account_lines:
                account_lines.append("")
            account_lines.append(f"〔{mission_class.name}〕")
            supported_actions = (
                (BaseMission.SIGN, "签到", None),
                (BaseMission.VIEW, "阅读", 3),
                (BaseMission.LIKE, "点赞", 5),
                (BaseMission.SHARE, "分享", 1),
            )
            # 任务以米游社实时返回为准。阅读、点赞、分享已于 2026-07
            # 从任务清单下线，不能再用固定四项兼容模型强制执行。
            actions = tuple(
                action
                for action in supported_actions
                if action[0] in before.state_dict
            )
            for key, label, target_count in actions:
                if key in cached or _mission_finished_by_state(before, key):
                    account_lines.append(f"✅ {label}　已完成")
                    continue
                try:
                    if key == BaseMission.SIGN:
                        operation = lambda: mission.sign(user, retry=False)
                    elif key == BaseMission.VIEW:
                        operation = lambda: mission.read(
                            read_times=target_count, retry=False
                        )
                    elif key == BaseMission.LIKE:
                        operation = lambda: mission.like(
                            like_times=target_count, retry=False
                        )
                    else:
                        operation = lambda: mission.share(retry=False)

                    action_result, action_network_failed = (
                        await _call_with_network_retry(
                            operation,
                            f"账户 {account.display_name} {label}",
                        )
                    )
                    any_network_failure = (
                        any_network_failure or action_network_failed
                    )
                    if key == BaseMission.SIGN:
                        status, points = action_result or (None, None)
                        success_text = f"+{points or 0} 米游币"
                    else:
                        status = action_result
                        success_text = f"{target_count}/{target_count}"
                    if status:
                        await _mark_cached_mission(
                            user_id, account_id, class_name, key
                        )
                        cached.add(key)
                        account_lines.append(f"✅ {label}　{success_text}")
                    else:
                        account_success = False
                        needs_verification = bool(
                            getattr(status, "need_verify", False)
                        )
                        requires_user_action = (
                            requires_user_action or needs_verification
                        )
                        reason = (
                            "网络连接失败"
                            if action_network_failed
                            else _mission_status_reason(status)
                        )
                        account_lines.append(
                            f"❌ {label}　{reason}"
                        )
                        if needs_verification:
                            account_lines.append(
                                "⚠️ 请打开米游社 App 手动完成打卡"
                            )
                except Exception as error:
                    logger.exception(
                        "[Mystool] 米游币子任务异常: account=%s action=%s",
                        account.display_name,
                        label,
                    )
                    account_success = False
                    account_lines.append(
                        f"❌ {label}　执行异常（{type(error).__name__}）"
                    )

        after_result, after_network_failed = await _call_with_network_retry(
            lambda: get_missions_state(account, retry=False),
            f"账户 {account.display_name} 刷新任务状态",
        )
        any_network_failure = any_network_failure or after_network_failed
        after_status, after = after_result or (None, None)
        if after_status and after:
            gained = max(0, after.current_myb - before.current_myb)
            current_myb = after.current_myb
        else:
            gained = 0
            current_myb = before.current_myb
            account_success = False
            after_reason = (
                "网络连接失败"
                if after_network_failed
                else _mission_status_reason(after_status)
            )
            account_lines.extend(["", f"❌ 余额刷新　{after_reason}"])

        if not account_success:
            all_complete = False
        title = "✅ 全部完成" if account_success else "⚠️ 部分未完成"
        messages.append(
            "\n".join(
                [
                    f"【米游币任务｜{title}】",
                    f"账户：{account.display_name}",
                    "",
                    *account_lines,
                    "",
                    f"本次获得：+{gained} 米游币",
                    f"当前余额：{current_myb} 米游币",
                ]
            )
        )
    if not messages:
        return MissionRunResult(
            messages=["没有可执行的米游币任务"], complete=False
        )
    return MissionRunResult(
        messages=messages,
        complete=all_complete,
        network_failed=any_network_failure,
        requires_user_action=requires_user_action,
    )


async def _run_missions(user_id: str) -> MissionRunResult:
    lock = _MISSION_USER_LOCKS.setdefault(user_id, asyncio.Lock())
    async with lock:
        return await _perform_missions_locked(user_id)


async def _perform_missions(user_id: str) -> list[str]:
    return (await _run_missions(user_id)).messages
