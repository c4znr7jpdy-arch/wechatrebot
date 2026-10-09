"""
定时任务插件 — 新闻、天气、Epic、KFC、油价、B站动态定时推送
使用 AstrBot cron_manager 替代原有 APScheduler
"""
import asyncio
import sys
import os
import re
import json
import logging
import importlib
from pathlib import Path
from typing import Optional

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.web import error_response, json_response, request
import astrbot.api.message_components as Comp
from astrbot.core.message.message_event_result import MessageChain, Plain

logger = logging.getLogger("jiang_schedule")


def _apply_content_service_config() -> None:
    config_path = (
        Path(__file__).resolve().parent.parent.parent
        / "config"
        / "jiang_commands_config.json"
    )
    try:
        config = json.loads(config_path.read_text("utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        config = {}
    mappings = {
        "content_api_id": "JIANG_CONTENT_API_ID",
        "content_api_key": "JIANG_CONTENT_API_KEY",
        "weather_detail_token": "JIANG_WEATHER_DETAIL_TOKEN",
    }
    for key, environment_name in mappings.items():
        os.environ[environment_name] = str(config.get(key, "") or "").strip()

# NoneBot 桩（ai_plugin 依赖 nonebot）
_PLUGINS_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import _nonebot_stubs
_nonebot_stubs.setup()

try:
    from jiang_capability_router.state import record_action as _record_action
except Exception:
    _record_action = None

# 引入 ai_plugin 模块
_AI_PLUGIN_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "ai_plugin" / "ai_plugin")
if _AI_PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _AI_PLUGIN_DIR)

from news import fetch_hot_news, format_news
from weather import fetch_weather, format_weather
from kfc import fetch_kfc_text
import oilprice as _oilprice_module

# 共享模块不在本插件的热更新清理范围内；油价模块无状态，可安全刷新。
_oilprice_module = importlib.reload(_oilprice_module)
fetch_oilprice = _oilprice_module.fetch_oilprice
format_oilprice = _oilprice_module.format_oilprice

_AI_PLUGIN_PARENT = str(Path(__file__).resolve().parent.parent.parent.parent / "ai_plugin")
if _AI_PLUGIN_PARENT not in sys.path:
    sys.path.insert(0, _AI_PLUGIN_PARENT)

import ai_plugin.bilibili_dynamic as _bilibili_dynamic_module
import ai_plugin.checkin as _checkin_module

_bilibili_dynamic_module = importlib.reload(_bilibili_dynamic_module)
_checkin_module = importlib.reload(_checkin_module)
download_bili_image = _bilibili_dynamic_module.download_image
dynamic_image_url = _bilibili_dynamic_module.dynamic_image_url
fetch_dynamics = _bilibili_dynamic_module.fetch_dynamics
format_latest_dynamic = _bilibili_dynamic_module.format_latest_dynamic
checkin_all = _checkin_module.checkin_all
CheckinConfigError = _checkin_module.CheckinConfigError
_CHECKIN_MANAGER = _checkin_module.get_manager()

# 运行时 context 引用（回调发消息用）
_CTX = None

# 持久化文件
_DATA_DIR = Path(__file__).resolve().parent.parent.parent.parent / "data" / "jiang_schedule"
_TASK_FILE = _DATA_DIR / "tasks.json"
_BILI_STATE_FILE = _DATA_DIR / "bili_state.json"
_BILI_SEEN_LIMIT = 30
_BILI_POLL_LOCK = asyncio.Lock()
_CHECKIN_CONFIG_LOCK = asyncio.Lock()


def _record_schedule_action(
    session: str,
    capability_id: str,
    display_name: str,
    details: dict | None = None,
) -> None:
    if not _record_action:
        return
    try:
        _record_action(
            session,
            capability_id,
            display_name,
            "schedule",
            details=details,
        )
    except Exception as exc:
        logger.warning(f"记录定时能力动作失败: {exc}")

# ── 命令解析正则 ────────────────────────────────────────

_COMMAND_PREFIX = r"[\\/]"
_COMMAND_ENTRY_PATTERN = (
    rf"^{_COMMAND_PREFIX}(?:定时|订阅动态|取消订阅动态|订阅列表)"
)
_RE_ADD = re.compile(
    rf"{_COMMAND_PREFIX}定时(新闻|热点|天气|epic|喜加一|免费游戏|油价|kfc|KFC|疯狂星期四|签到)\s*(?:(周[一二三四五六日天])\s*)?(?:([一-鿿]{{2,6}})\s*)?(\d{{1,2}})[:\：]?(\d{{2}})?\s*点?"
)
_RE_DEL = re.compile(
    rf"{_COMMAND_PREFIX}定时删除(新闻|热点|天气|epic|喜加一|免费游戏|油价|kfc|KFC|疯狂星期四|B站动态|b站动态|签到)"
)
_RE_LIST = re.compile(rf"{_COMMAND_PREFIX}定时列表")
_RE_BILI_SUB = re.compile(
    rf"{_COMMAND_PREFIX}订阅动态\s*(\d+)\s*(?:(\d+)\s*分钟?)?"
)
_RE_BILI_UNSUB = re.compile(rf"{_COMMAND_PREFIX}取消订阅动态\s*(\d+)?")
_RE_BILI_LIST = re.compile(rf"{_COMMAND_PREFIX}订阅列表")

_DOW_MAP = {
    "周一": "mon", "周二": "tue", "周三": "wed", "周四": "thu",
    "周五": "fri", "周六": "sat", "周日": "sun", "周天": "sun",
}
_DOW_LABEL = {
    "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
    "fri": "周五", "sat": "周六", "sun": "周日",
}


def _normalize_type(raw: str) -> str:
    if raw in ("新闻", "热点"):
        return "news"
    if raw == "天气":
        return "weather"
    if raw in ("epic", "喜加一", "免费游戏"):
        return "epic"
    if raw == "油价":
        return "oilprice"
    if raw in ("kfc", "KFC", "疯狂星期四"):
        return "kfc"
    if raw in ("B站动态", "b站动态"):
        return "bili_dynamic"
    if raw == "签到":
        return "checkin"
    return raw


def _type_label(t: str) -> str:
    return {"news": "热点新闻", "weather": "天气预报", "epic": "Epic喜加一",
            "oilprice": "每日油价", "kfc": "KFC疯狂星期四", "bili_dynamic": "B站动态订阅",
            "checkin": "每日签到"}.get(t, t)


def _parse_time(hour_str: str, minute_str: Optional[str]) -> tuple[int, int]:
    h = int(hour_str)
    m = int(minute_str) if minute_str else 0
    return max(0, min(h, 23)), max(0, min(m, 59))


# ── 持久化 ──────────────────────────────────────────────

def _load_tasks() -> list[dict]:
    if not _TASK_FILE.exists():
        return []
    try:
        return json.loads(_TASK_FILE.read_text("utf-8")).get("tasks", [])
    except Exception:
        return []


def _save_tasks(tasks: list[dict]):
    _TASK_FILE.parent.mkdir(parents=True, exist_ok=True)
    _TASK_FILE.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=2), "utf-8")


def _apply_checkin_task_config() -> bool:
    """让 tasks.json 中的签到任务与 WebUI 配置保持一致。"""
    tasks = _load_tasks()
    # 首次启用 WebUI 时优先迁移用户原来通过命令配置的时间和群，
    # 只有找不到旧任务时才使用签到模块的默认值。
    if not _CHECKIN_MANAGER.config_file.exists():
        legacy = next((task for task in tasks if task.get("type") == "checkin"), None)
        if legacy:
            _CHECKIN_MANAGER.update_schedule({
                "enabled": True,
                "time": f"{int(legacy.get('hour', 8)):02d}:{int(legacy.get('minute', 30)):02d}",
                "session": legacy.get("session", ""),
            })
    updated = [task for task in tasks if task.get("type") != "checkin"]
    desired = _CHECKIN_MANAGER.schedule_task()
    if desired:
        updated.append(desired)
    if updated == tasks:
        return False
    _save_tasks(updated)
    return True


def _load_bili_state() -> dict:
    if not _BILI_STATE_FILE.exists():
        return {}
    try:
        return json.loads(_BILI_STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save_bili_state(state: dict):
    _BILI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_file = _BILI_STATE_FILE.with_suffix(".json.tmp")
    temp_file.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")
    temp_file.replace(_BILI_STATE_FILE)


def _bili_item_id(item: dict) -> str:
    return str(item.get("dynamic_id", "") or "").strip()


def _bili_item_timestamp(item: dict) -> int:
    try:
        return int(item.get("timestamp", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _bili_id_order(dynamic_id: str) -> int:
    try:
        return int(dynamic_id)
    except (TypeError, ValueError):
        return 0


def _bili_item_cursor(item: dict) -> tuple[int, int]:
    dynamic_id = _bili_item_id(item)
    return _bili_item_timestamp(item), _bili_id_order(dynamic_id)


def _bili_recent_ids(*groups) -> list[str]:
    result = []
    seen = set()
    for group in groups:
        for value in group or []:
            dynamic_id = str(value or "").strip()
            if not dynamic_id or dynamic_id in seen:
                continue
            seen.add(dynamic_id)
            result.append(dynamic_id)
            if len(result) >= _BILI_SEEN_LIMIT:
                return result
    return result


def _bili_checkpoint(raw_state, items: list[dict]) -> tuple[dict, bool]:
    """规范化单个订阅状态；第二个返回值表示旧基线能否安全定位。"""
    if isinstance(raw_state, dict):
        last_id = str(raw_state.get("last_id", "") or "").strip()
        try:
            last_timestamp = int(raw_state.get("last_timestamp", 0) or 0)
        except (TypeError, ValueError):
            last_timestamp = 0
        seen_ids = _bili_recent_ids(
            raw_state.get("seen_ids", []),
            [last_id],
        )
    else:
        last_id = str(raw_state or "").strip()
        last_timestamp = 0
        seen_ids = _bili_recent_ids([last_id])

    if last_timestamp <= 0 and last_id:
        matched = next(
            (item for item in items if _bili_item_id(item) == last_id),
            None,
        )
        if matched is None:
            return {
                "last_id": last_id,
                "last_timestamp": 0,
                "seen_ids": seen_ids,
            }, False
        last_timestamp = _bili_item_timestamp(matched)

    return {
        "last_id": last_id,
        "last_timestamp": last_timestamp,
        "seen_ids": seen_ids,
    }, bool(last_id and last_timestamp > 0)


def _bili_state_for_item(
    item: dict,
    previous: dict | None = None,
    observed_items: list[dict] | None = None,
) -> dict:
    dynamic_id = _bili_item_id(item)
    previous_seen = (previous or {}).get("seen_ids", [])
    observed_ids = [_bili_item_id(value) for value in observed_items or []]
    return {
        "last_id": dynamic_id,
        "last_timestamp": _bili_item_timestamp(item),
        "seen_ids": _bili_recent_ids(
            [dynamic_id],
            observed_ids,
            previous_seen,
        ),
    }


# ── 定时任务回调 ────────────────────────────────────────

async def _cb_news(session: str, **kwargs):
    """定时推送热点新闻"""
    ctx = _CTX
    if not ctx:
        return
    try:
        items = await fetch_hot_news(20)
        msg = format_news(items)
        sent = await ctx.send_message(session, MessageChain([Plain(msg)]))
        if not sent:
            raise RuntimeError("未找到可发送的平台会话")
        _record_schedule_action(session, "info.news", "热点新闻", {"count": 20})
        logger.info(f"[SCHEDULE] 新闻已推送 → {session}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 新闻推送失败: {e}")


async def _cb_weather(session: str, city: str = "", **kwargs):
    """定时推送天气"""
    ctx = _CTX
    if not ctx or not city:
        return
    try:
        data = await fetch_weather(city)
        msg = format_weather(data)
        sent = await ctx.send_message(session, MessageChain([Plain(msg)]))
        if not sent:
            raise RuntimeError("未找到可发送的平台会话")
        _record_schedule_action(
            session,
            "info.weather",
            "天气预报",
            {"city": city},
        )
        logger.info(f"[SCHEDULE] 天气已推送 → {session} ({city})")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 天气推送失败: {e}")


async def _cb_kfc(session: str, **kwargs):
    """定时推送 KFC 文案"""
    ctx = _CTX
    if not ctx:
        return
    try:
        text = await fetch_kfc_text()
        sent = await ctx.send_message(session, MessageChain([Plain(text)]))
        if not sent:
            raise RuntimeError("未找到可发送的平台会话")
        _record_schedule_action(session, "content.kfc", "疯狂星期四文案")
        logger.info(f"[SCHEDULE] KFC已推送 → {session}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] KFC推送失败: {e}")


async def _cb_oilprice(session: str, province: str = "", **kwargs):
    """定时推送油价"""
    ctx = _CTX
    if not ctx or not province:
        return
    try:
        data = await fetch_oilprice(province)
        msg = format_oilprice(data)
        sent = await ctx.send_message(session, MessageChain([Plain(msg)]))
        if not sent:
            raise RuntimeError("未找到可发送的平台会话")
        _record_schedule_action(
            session,
            "info.oilprice",
            "今日油价",
            {"province": province},
        )
        logger.info(f"[SCHEDULE] 油价已推送 → {session} ({province})")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 油价推送失败: {e}")


async def _cb_bili_dynamic(session: str, uid: str = "", **kwargs):
    """轮询 B站动态并推送新内容"""
    ctx = _CTX
    if not ctx or not uid:
        return
    state_key = f"{uid}_{session}"

    # 同一进程内串行处理所有 B站轮询，避免两个任务同时读写状态文件。
    async with _BILI_POLL_LOCK:
        state = _load_bili_state()
        raw_checkpoint = state.get(state_key)

        try:
            items = await fetch_dynamics(uid, count=10)
        except Exception as e:
            logger.warning(f"[SCHEDULE] B站动态拉取失败 uid={uid}: {e}")
            return

        items = [
            item for item in items
            if _bili_item_id(item) and _bili_item_timestamp(item) > 0
        ]
        if not items:
            return

        # 空间流可能包含置顶旧动态，动态 ID 仅作为同秒排序的辅助键。
        items = sorted(items, key=_bili_item_cursor, reverse=True)
        latest = items[0]

        # 首次运行只建立基线，同时记住当前窗口，绝不补发历史内容。
        if raw_checkpoint is None:
            state[state_key] = _bili_state_for_item(
                latest,
                observed_items=items,
            )
            _save_bili_state(state)
            logger.info(
                f"[SCHEDULE] B站动态首次记录 uid={uid} "
                f"id={_bili_item_id(latest)} ts={_bili_item_timestamp(latest)}"
            )
            return

        checkpoint, checkpoint_resolved = _bili_checkpoint(raw_checkpoint, items)
        if not checkpoint_resolved:
            # 旧版状态只有 last_id。若本轮接口没返回该 ID，无法判断是接口
            # 短暂回退还是积累了大量新内容；为避免误发旧内容，保留原基线。
            logger.warning(
                f"[SCHEDULE] B站动态基线未出现在当前窗口，忽略本轮 "
                f"uid={uid} last_id={checkpoint['last_id']} "
                f"latest_id={_bili_item_id(latest)}"
            )
            return

        checkpoint_cursor = (
            checkpoint["last_timestamp"],
            _bili_id_order(checkpoint["last_id"]),
        )
        latest_cursor = _bili_item_cursor(latest)

        # B站接口偶尔返回旧缓存。游标只能前进，禁止状态倒退。
        if latest_cursor < checkpoint_cursor:
            logger.warning(
                f"[SCHEDULE] B站动态接口结果发生时间倒退，忽略本轮 "
                f"uid={uid} checkpoint={checkpoint_cursor} latest={latest_cursor}"
            )
            return

        seen_ids = set(checkpoint.get("seen_ids", []))
        new_items = [
            item for item in items
            if _bili_item_cursor(item) > checkpoint_cursor
            and _bili_item_id(item) not in seen_ids
        ]
        if not new_items:
            # 将旧字符串状态静默升级成带时间高水位的结构化状态。
            normalized = {
                "last_id": checkpoint["last_id"],
                "last_timestamp": checkpoint["last_timestamp"],
                "seen_ids": _bili_recent_ids(
                    [checkpoint["last_id"]],
                    [_bili_item_id(item) for item in items],
                    checkpoint.get("seen_ids", []),
                ),
            }
            if raw_checkpoint != normalized:
                state[state_key] = normalized
                _save_bili_state(state)
            return

        # 即使轮询期间积累了多条，也只推送真正最新的一条和一张相关图片。
        newest = max(new_items, key=_bili_item_cursor)
        image_path = None
        try:
            author = str(newest.get("author", "") or "").strip()
            msg = format_latest_dynamic(
                newest,
                title=author or f"B站用户 {uid}",
            )
            image_path = await download_bili_image(dynamic_image_url(newest))
            chain = []
            if image_path:
                chain.append(Comp.Image.fromFileSystem(image_path))
            chain.append(Comp.Plain(msg))
            sent = await ctx.send_message(session, MessageChain(chain))
            if not sent:
                raise RuntimeError("未找到可发送的平台会话")
        except Exception as e:
            logger.warning(f"[SCHEDULE] B站动态推送失败: {e}")
            return
        finally:
            if image_path:
                Path(image_path).unlink(missing_ok=True)

        # 发送成功后才提交高水位；发送失败时下次轮询会继续重试。
        state[state_key] = _bili_state_for_item(
            newest,
            previous=checkpoint,
            observed_items=items,
        )
        _save_bili_state(state)
        _record_schedule_action(
            session,
            "info.bili_dynamic",
            "B站动态",
            {"uid": uid, "count": 1},
        )
        logger.info(
            f"[SCHEDULE] B站动态已推送最新 1 条 → {session} "
            f"id={_bili_item_id(newest)} ts={_bili_item_timestamp(newest)}"
        )


async def _cb_checkin(session: str, **kwargs):
    """定时签到推送"""
    ctx = _CTX
    if not ctx:
        return
    try:
        msg = await checkin_all()
        await ctx.send_message(session, MessageChain([Plain(msg)]))
        logger.info(f"[SCHEDULE] 签到已推送 → {session}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 签到推送失败: {e}")


# 回调映射
_CALLBACKS = {
    "news": _cb_news,
    "weather": _cb_weather,
    "kfc": _cb_kfc,
    "oilprice": _cb_oilprice,
    "bili_dynamic": _cb_bili_dynamic,
    "checkin": _cb_checkin,
}


# ── Job 管理 ────────────────────────────────────────────

def _task_to_cron(task: dict) -> str:
    """将任务配置转为 cron 表达式"""
    t = task["type"]
    h = task.get("hour", 0)
    m = task.get("minute", 0)
    dow = task.get("extra", {}).get("day_of_week", "")

    if t == "bili_dynamic":
        interval = task.get("extra", {}).get("interval", 5)
        return f"*/{interval} * * * *"

    if dow:
        return f"{m} {h} * * {dow}"
    return f"{m} {h} * * *"


def _make_job_name(task: dict) -> str:
    t = task["type"]
    tid = task.get("session", "default")
    if t == "bili_dynamic":
        uid = task.get("extra", {}).get("uid", "")
        return f"bili_{tid}_{uid}"
    return f"{t}_{tid}"


async def _sync_jobs(ctx):
    """同步定时任务：清除旧 job，重新注册"""
    cron = ctx.cron_manager
    # 列出已有的 basic jobs
    existing = await cron.list_jobs(job_type="basic")
    logger.info(f"[SCHEDULE] 数据库中已有 {len(existing)} 个 basic jobs")
    for job in existing:
        if job.name.startswith(("news_", "weather_", "kfc_", "oilprice_", "bili_", "checkin_")):
            await cron.delete_job(job.job_id)
            logger.info(f"[SCHEDULE] 删除旧 job: {job.name} ({job.job_id})")

    tasks = _load_tasks()
    logger.info(f"[SCHEDULE] tasks.json 中有 {len(tasks)} 个任务")
    for task in tasks:
        if task.get("enabled", True) is False:
            logger.info(f"[SCHEDULE] 跳过已暂停任务: {_make_job_name(task)}")
            continue
        await _register_task(ctx, task)

    # 验证注册结果
    all_jobs = await cron.list_jobs()
    logger.info(f"[SCHEDULE] 注册后数据库总 jobs: {len(all_jobs)}")
    for j in all_jobs:
        logger.info(f"  - {j.job_id} | {j.name} | {j.job_type} | payload={j.payload}")


async def _register_task(ctx, task: dict):
    """注册单个定时任务"""
    cron = ctx.cron_manager
    name = _make_job_name(task)
    cron_expr = _task_to_cron(task)
    session = task.get("session", "")
    t = task["type"]
    logger.info(f"[SCHEDULE] 准备注册: type={t} name={name} cron={cron_expr} session={session}")

    if t not in _CALLBACKS:
        return

    handler = _CALLBACKS[t]
    payload = {"session": session}

    if t == "weather":
        payload["city"] = task.get("extra", {}).get("city", "")
    elif t == "oilprice":
        payload["province"] = task.get("extra", {}).get("province", "")
    elif t == "bili_dynamic":
        payload["uid"] = task.get("extra", {}).get("uid", "")

    try:
        await cron.add_basic_job(
            name=name,
            cron_expression=cron_expr,
            handler=handler,
            payload=payload,
            description=f"{_type_label(t)} 定时推送",
            timezone="Asia/Shanghai",
            persistent=True,
        )
        logger.info(f"[SCHEDULE] 注册: {name} → {cron_expr}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 注册失败: {name} → {e}")


# ── AstrBot Star 插件 ───────────────────────────────────

class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        global _CTX
        self.context = context
        _apply_content_service_config()
        # AstrBot 热重载插件时不一定再次调用 initialize()，构造阶段先绑定，
        # 保证新注册的定时回调仍能取得发送上下文。
        _CTX = context
        self._register_page_apis()

    def _register_page_apis(self) -> None:
        """注册签到管理 Plugin Page API。"""
        if not hasattr(self.context, "register_web_api"):
            logger.warning("[SCHEDULE] 当前 AstrBot 不支持 Plugin Pages")
            return
        routes = (
            ("checkin/config", self.page_checkin_config, ["GET"], "Check-in dashboard data"),
            ("checkin/accounts/save", self.page_checkin_save_account, ["POST"], "Save check-in account"),
            ("checkin/schedule/save", self.page_checkin_save_schedule, ["POST"], "Save check-in schedule"),
            ("checkin/run", self.page_checkin_run, ["POST"], "Run check-in now"),
        )
        for path, handler, methods, description in routes:
            self.context.register_web_api(
                f"/jiang_schedule/page/{path}", handler, methods, description
            )

    async def page_checkin_config(self):
        try:
            return json_response({"status": "ok", "data": _CHECKIN_MANAGER.public_config()})
        except CheckinConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[SCHEDULE] WebUI 读取签到配置失败")
            return error_response(str(exc), status_code=500)

    async def page_checkin_save_account(self):
        try:
            payload = await request.json(default={})
            account = (payload or {}).get("account", payload)
            async with _CHECKIN_CONFIG_LOCK:
                config = _CHECKIN_MANAGER.upsert_account(account)
            return json_response({"status": "ok", "data": config})
        except CheckinConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[SCHEDULE] WebUI 保存签到站点失败")
            return error_response(str(exc), status_code=500)

    async def page_checkin_save_schedule(self):
        try:
            payload = await request.json(default={})
            schedule = (payload or {}).get("schedule", payload)
            async with _CHECKIN_CONFIG_LOCK:
                config = _CHECKIN_MANAGER.update_schedule(schedule)
                _apply_checkin_task_config()
                await _sync_jobs(self.context)
            return json_response({"status": "ok", "data": config})
        except CheckinConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[SCHEDULE] WebUI 保存签到定时任务失败")
            return error_response(str(exc), status_code=500)

    async def page_checkin_run(self):
        try:
            payload = await request.json(default={})
            account_id = str((payload or {}).get("id") or "").strip() or None
            result = await _CHECKIN_MANAGER.run(account_id)
            return json_response({"status": "ok", "data": result})
        except CheckinConfigError as exc:
            return error_response(str(exc), status_code=400)
        except Exception as exc:
            logger.exception("[SCHEDULE] WebUI 手动签到失败")
            return error_response(str(exc), status_code=502)

    async def initialize(self) -> None:
        """启动时加载所有定时任务"""
        global _CTX
        _CTX = self.context
        logger.info("[SCHEDULE] initialize 开始")
        try:
            _apply_checkin_task_config()
            await _sync_jobs(self.context)
            tasks = _load_tasks()
            if tasks:
                logger.info(f"[SCHEDULE] 已加载 {len(tasks)} 个定时任务")

            logger.info("[SCHEDULE] initialize 完成")
        except Exception as e:
            logger.exception(f"[SCHEDULE] 初始化失败: {e}")

    @filter.regex(_COMMAND_ENTRY_PATTERN)
    async def schedule(self, event: AstrMessageEvent):
        """定时任务管理"""
        text = event.get_message_str().strip()
        session = event.unified_msg_origin

        # ── 列表 ──
        if _RE_LIST.search(text):
            tasks = _load_tasks()
            mine = [t for t in tasks if t.get("session") == session]
            if not mine:
                yield event.plain_result("当前聊天暂无定时任务")
                return
            lines = ["定时任务列表:"]
            for t in mine:
                label = _type_label(t["type"])
                extra = t.get("extra", {})
                if t["type"] == "bili_dynamic":
                    uid = extra.get("uid", "?")
                    interval = extra.get("interval", 5)
                    lines.append(f"  • {label} UID:{uid}  每{interval}分钟")
                else:
                    location = extra.get("city", "") or extra.get("province", "")
                    time_str = f"{t['hour']:02d}:{t['minute']:02d}"
                    dow = extra.get("day_of_week", "")
                    freq = _DOW_LABEL.get(dow, "") if dow else "每天"
                    suffix = f" ({location})" if location else ""
                    lines.append(f"  • {label}{suffix}  {freq} {time_str}")
            lines.append("\n发送「/定时删除新闻」可删除")
            yield event.plain_result("\n".join(lines))
            return

        # ── 删除 ──
        m = _RE_DEL.search(text)
        if m:
            t = _normalize_type(m.group(1))
            if t == "checkin":
                schedule = _CHECKIN_MANAGER.public_config()["schedule"]
                if schedule.get("enabled") and schedule.get("session") == session:
                    _CHECKIN_MANAGER.update_schedule({**schedule, "enabled": False})
                    _apply_checkin_task_config()
                    await _sync_jobs(self.context)
                    yield event.plain_result("已删除「每日签到」定时任务")
                else:
                    yield event.plain_result("当前聊天没有每日签到定时任务")
                return
            tasks = _load_tasks()
            new_tasks = []
            removed = False
            for task in tasks:
                if task.get("session") == session and task["type"] == t:
                    removed = True
                else:
                    new_tasks.append(task)
            if removed:
                _save_tasks(new_tasks)
                await _sync_jobs(self.context)
                yield event.plain_result(f"已删除「{_type_label(t)}」定时任务")
            else:
                yield event.plain_result(f"未找到「{_type_label(t)}」定时任务")
            return

        # ── 添加 ──
        m = _RE_ADD.search(text)
        if m:
            t = _normalize_type(m.group(1))
            dow_raw = m.group(2) or ""
            city = m.group(3) or ""
            hour, minute = _parse_time(m.group(4), m.group(5))

            if t == "checkin":
                _CHECKIN_MANAGER.update_schedule({
                    "enabled": True,
                    "time": f"{hour:02d}:{minute:02d}",
                    "session": session,
                })
                _apply_checkin_task_config()
                await _sync_jobs(self.context)
                yield event.plain_result(f"已更新「每日签到」→ 每天 {hour:02d}:{minute:02d}")
                return

            if t == "weather" and not city:
                yield event.plain_result("天气任务需要指定城市，如: /定时天气 绵阳 8点")
                return
            if t == "oilprice" and not city:
                yield event.plain_result("油价任务需要指定省份，如: /定时油价 四川 8点")
                return

            day_of_week = ""
            if t == "kfc":
                day_of_week = _DOW_MAP.get(dow_raw, "thu")
            elif dow_raw:
                day_of_week = _DOW_MAP.get(dow_raw, "")

            tasks = _load_tasks()
            # 检查重复
            for task in tasks:
                if task.get("session") == session and task["type"] == t:
                    task["hour"] = hour
                    task["minute"] = minute
                    if city:
                        if t == "oilprice":
                            task.setdefault("extra", {})["province"] = city
                        else:
                            task.setdefault("extra", {})["city"] = city
                    if day_of_week:
                        task.setdefault("extra", {})["day_of_week"] = day_of_week
                    _save_tasks(tasks)
                    await _sync_jobs(self.context)
                    label = _type_label(t)
                    suffix = f" ({city})" if city else ""
                    dow_s = f" {_DOW_LABEL.get(day_of_week, dow_raw)}" if day_of_week else " 每天"
                    yield event.plain_result(f"已更新「{label}{suffix}」→{dow_s} {hour:02d}:{minute:02d}")
                    return

            # 新增
            extra = {}
            if city:
                if t == "oilprice":
                    extra["province"] = city
                else:
                    extra["city"] = city
            if day_of_week:
                extra["day_of_week"] = day_of_week
            task = {
                "type": t,
                "session": session,
                "hour": hour,
                "minute": minute,
                "extra": extra,
            }
            tasks.append(task)
            _save_tasks(tasks)
            await _register_task(self.context, task)
            label = _type_label(t)
            suffix = f" ({city})" if city else ""
            dow_s = f" {_DOW_LABEL.get(day_of_week, dow_raw)}" if day_of_week else " 每天"
            yield event.plain_result(f"已添加「{label}{suffix}」→{dow_s} {hour:02d}:{minute:02d}")
            return

        # ── B站动态订阅 ──
        m = _RE_BILI_SUB.search(text)
        if m:
            uid = m.group(1)
            interval = int(m.group(2)) if m.group(2) else 5
            interval = max(2, min(interval, 60))

            tasks = _load_tasks()
            for task in tasks:
                if (task.get("session") == session and task["type"] == "bili_dynamic"
                        and task.get("extra", {}).get("uid") == uid):
                    task["extra"]["interval"] = interval
                    _save_tasks(tasks)
                    await _sync_jobs(self.context)
                    yield event.plain_result(f"已更新B站动态订阅 UID:{uid} → 每{interval}分钟检查")
                    return

            task = {
                "type": "bili_dynamic",
                "session": session,
                "hour": 0,
                "minute": 0,
                "extra": {"uid": uid, "interval": interval},
            }
            tasks.append(task)
            _save_tasks(tasks)
            await _register_task(self.context, task)
            yield event.plain_result(f"已订阅B站动态 UID:{uid} → 每{interval}分钟检查\n有新动态会自动推送")
            return

        # ── 取消B站动态订阅 ──
        m = _RE_BILI_UNSUB.search(text)
        if m:
            uid = m.group(1) or ""
            tasks = _load_tasks()
            new_tasks = []
            removed = False
            for task in tasks:
                if (task.get("session") == session and task["type"] == "bili_dynamic"
                        and (not uid or task.get("extra", {}).get("uid") == uid)):
                    removed = True
                else:
                    new_tasks.append(task)
            if removed:
                _save_tasks(new_tasks)
                await _sync_jobs(self.context)
                suffix = f" UID:{uid}" if uid else "（全部）"
                yield event.plain_result(f"已取消B站动态订阅{suffix}")
            else:
                yield event.plain_result("未找到相关B站动态订阅")
            return

        # ── 订阅列表 ──
        if _RE_BILI_LIST.search(text):
            tasks = _load_tasks()
            mine = [t for t in tasks if t.get("session") == session and t["type"] == "bili_dynamic"]
            if not mine:
                yield event.plain_result("当前聊天暂无B站动态订阅\n发送「/订阅动态 UID」可添加")
                return
            lines = ["B站动态订阅列表:"]
            for t in mine:
                uid = t.get("extra", {}).get("uid", "?")
                interval = t.get("extra", {}).get("interval", 5)
                lines.append(f"  • UID:{uid}  每{interval}分钟")
            lines.append("\n发送「/取消订阅动态 UID」可取消")
            yield event.plain_result("\n".join(lines))
            return

        # 未匹配任何子命令
        yield event.plain_result(
            "定时任务用法:\n"
            "  /定时新闻 8点 — 每天推送热点\n"
            "  /定时天气 绵阳 8点 — 每天天气\n"
            "  /定时kfc 周四 12点 — 每周KFC\n"
            "  /定时列表 — 查看定时任务\n"
            "  /定时删除新闻 — 删除任务\n"
            "  /订阅动态 UID — B站动态订阅\n"
            "  /取消订阅动态 UID — 取消订阅\n"
            "  /订阅列表 — 查看订阅"
        )
