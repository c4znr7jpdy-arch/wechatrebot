"""
定时任务热管理模块 — 通过群聊命令动态增删定时任务
"""
import json
import re
from pathlib import Path
from typing import Optional

import nonebot
from nonebot import logger, require, Bot

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from .news import fetch_hot_news, format_news
from .weather import fetch_weather, format_weather
from .bilibili_dynamic import fetch_dynamics, send_dynamic
from .kfc import fetch_kfc_text

_TASK_FILE = Path(__file__).parent.parent / "data" / "schedule_tasks.json"
_BILI_STATE_FILE = Path(__file__).parent.parent / "data" / "bili_dynamic_state.json"

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


# ── Job 逻辑 ────────────────────────────────────────────


async def _run_news(target_id: str, detail_type: str):
    bots = nonebot.get_bots()
    if not bots:
        logger.warning("[SCHEDULE] 无可用 bot")
        return
    bot = next(iter(bots.values()))
    try:
        items = await fetch_hot_news(20)
        msg = format_news(items)
        await bot.send_message(
            detail_type=detail_type,
            user_id=target_id if detail_type == "private" else None,
            group_id=target_id if detail_type == "group" else None,
            message=msg,
        )
        logger.info(f"[SCHEDULE] 新闻已推送 → {target_id}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 新闻推送失败: {e}")


async def _run_weather(target_id: str, detail_type: str, city: str):
    bots = nonebot.get_bots()
    if not bots:
        logger.warning("[SCHEDULE] 无可用 bot")
        return
    bot = next(iter(bots.values()))
    try:
        data = await fetch_weather(city)
        msg = format_weather(data)
        await bot.send_message(
            detail_type=detail_type,
            user_id=target_id if detail_type == "private" else None,
            group_id=target_id if detail_type == "group" else None,
            message=msg,
        )
        logger.info(f"[SCHEDULE] 天气已推送 → {target_id} ({city})")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 天气推送失败: {e}")


async def _run_epic(target_id: str, detail_type: str):
    from .epic import fetch_epic_free, format_epic_free, check_and_update_history
    bots = nonebot.get_bots()
    if not bots:
        logger.warning("[SCHEDULE] 无可用 bot")
        return
    bot = next(iter(bots.values()))
    try:
        games = await fetch_epic_free()
        job_id = f"epic_{target_id}"
        if not check_and_update_history(job_id, games):
            logger.info(f"[SCHEDULE] Epic 内容无变化，跳过推送 → {target_id}")
            return
        msg = format_epic_free(games)
        await bot.send_message(
            detail_type=detail_type,
            user_id=target_id if detail_type == "private" else None,
            group_id=target_id if detail_type == "group" else None,
            message=msg,
        )
        logger.info(f"[SCHEDULE] Epic 已推送 → {target_id}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] Epic 推送失败: {e}")


async def _run_oilprice(target_id: str, detail_type: str, province: str):
    from .oilprice import fetch_oilprice, format_oilprice
    bots = nonebot.get_bots()
    if not bots:
        logger.warning("[SCHEDULE] 无可用 bot")
        return
    bot = next(iter(bots.values()))
    try:
        data = await fetch_oilprice(province)
        msg = format_oilprice(data)
        await bot.send_message(
            detail_type=detail_type,
            user_id=target_id if detail_type == "private" else None,
            group_id=target_id if detail_type == "group" else None,
            message=msg,
        )
        logger.info(f"[SCHEDULE] 油价已推送 → {target_id} ({province})")
    except Exception as e:
        logger.exception(f"[SCHEDULE] 油价推送失败: {e}")


async def _run_kfc(target_id: str, detail_type: str):
    bots = nonebot.get_bots()
    if not bots:
        logger.warning("[SCHEDULE] 无可用 bot")
        return
    bot = next(iter(bots.values()))
    try:
        text = await fetch_kfc_text()
        await bot.send_message(
            detail_type=detail_type,
            user_id=target_id if detail_type == "private" else None,
            group_id=target_id if detail_type == "group" else None,
            message=text,
        )
        logger.info(f"[SCHEDULE] KFC已推送 → {target_id}")
    except Exception as e:
        logger.exception(f"[SCHEDULE] KFC推送失败: {e}")


# ── B站动态轮询 ────────────────────────────────────────


def _load_bili_state() -> dict:
    if not _BILI_STATE_FILE.exists():
        return {}
    try:
        return json.loads(_BILI_STATE_FILE.read_text("utf-8"))
    except Exception:
        return {}


def _save_bili_state(state: dict):
    _BILI_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _BILI_STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), "utf-8")


async def _run_bili_dynamic(target_id: str, detail_type: str, uid: str):
    bots = nonebot.get_bots()
    if not bots:
        logger.warning("[SCHEDULE] 无可用 bot")
        return
    bot = next(iter(bots.values()))
    is_group = detail_type == "group"
    state = _load_bili_state()
    state_key = f"{uid}_{target_id}"
    last_id = state.get(state_key, "")

    try:
        items = await fetch_dynamics(uid, count=5)
    except Exception as e:
        logger.warning(f"[SCHEDULE] B站动态拉取失败 uid={uid}: {e}")
        return

    if not items:
        return

    # 首次运行，只记录最新 id，不推送
    if not last_id:
        state[state_key] = items[0]["dynamic_id"]
        _save_bili_state(state)
        logger.info(f"[SCHEDULE] B站动态首次记录 uid={uid} last_id={items[0]['dynamic_id']}")
        return

    # 找出新动态（id 比 last_id 更新的）
    new_items = []
    for item in items:
        if item["dynamic_id"] == last_id:
            break
        new_items.append(item)

    if not new_items:
        return

    # 更新 state
    state[state_key] = new_items[0]["dynamic_id"]
    _save_bili_state(state)

    # 推送新动态（从旧到新）
    for item in reversed(new_items):
        try:
            await send_dynamic(bot, target_id, is_group, item)
        except Exception as e:
            logger.warning(f"[SCHEDULE] B站动态推送失败: {e}")

    logger.info(f"[SCHEDULE] B站动态已推送 {len(new_items)} 条 → {target_id}")


# ── Job 注册 ────────────────────────────────────────────


def _make_job_id(task: dict) -> str:
    if task["type"] == "bili_dynamic":
        uid = task.get("extra", {}).get("uid", "")
        return f"dyn_bili_{task['target_id']}_{uid}"
    return f"dyn_{task['type']}_{task['target_id']}_{task['hour']}_{task['minute']}"


def _register_job(task: dict):
    job_id = _make_job_id(task)
    if scheduler.get_job(job_id):
        return

    t = task["type"]
    tid = task["target_id"]
    dt = task["detail_type"]

    if t == "news":
        dow = task.get("extra", {}).get("day_of_week", "")
        cron_kwargs = {"hour": task["hour"], "minute": task["minute"],
                       "id": job_id, "misfire_grace_time": 300}
        if dow:
            cron_kwargs["day_of_week"] = dow
        @scheduler.scheduled_job("cron", **cron_kwargs)
        async def _job():
            await _run_news(tid, dt)
    elif t == "weather":
        city = task.get("extra", {}).get("city", "")
        dow = task.get("extra", {}).get("day_of_week", "")
        cron_kwargs = {"hour": task["hour"], "minute": task["minute"],
                       "id": job_id, "misfire_grace_time": 300}
        if dow:
            cron_kwargs["day_of_week"] = dow
        @scheduler.scheduled_job("cron", **cron_kwargs)
        async def _job():
            await _run_weather(tid, dt, city)
    elif t == "epic":
        dow = task.get("extra", {}).get("day_of_week", "")
        cron_kwargs = {"hour": task["hour"], "minute": task["minute"],
                       "id": job_id, "misfire_grace_time": 300}
        if dow:
            cron_kwargs["day_of_week"] = dow
        @scheduler.scheduled_job("cron", **cron_kwargs)
        async def _job():
            await _run_epic(tid, dt)
    elif t == "oilprice":
        province = task.get("extra", {}).get("province", "")
        dow = task.get("extra", {}).get("day_of_week", "")
        cron_kwargs = {"hour": task["hour"], "minute": task["minute"],
                       "id": job_id, "misfire_grace_time": 300}
        if dow:
            cron_kwargs["day_of_week"] = dow
        @scheduler.scheduled_job("cron", **cron_kwargs)
        async def _job():
            await _run_oilprice(tid, dt, province)
    elif t == "kfc":
        dow = task.get("extra", {}).get("day_of_week", "thu")
        @scheduler.scheduled_job("cron", day_of_week=dow, hour=task["hour"],
                                 minute=task["minute"], id=job_id, misfire_grace_time=300)
        async def _job():
            await _run_kfc(tid, dt)
    elif t == "bili_dynamic":
        uid = task.get("extra", {}).get("uid", "")
        interval = task.get("extra", {}).get("interval", 5)
        # B站动态用 interval 轮询而非 cron
        @scheduler.scheduled_job("interval", minutes=interval,
                                 id=job_id, misfire_grace_time=120)
        async def _job():
            await _run_bili_dynamic(tid, dt, uid)

    logger.info(f"[SCHEDULE] 注册: {job_id}")


def _unregister_job(task: dict):
    job_id = _make_job_id(task)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"[SCHEDULE] 移除: {job_id}")


def load_all_tasks():
    """启动时加载所有已保存的定时任务"""
    tasks = _load_tasks()
    for t in tasks:
        _register_job(t)
    if tasks:
        logger.info(f"[SCHEDULE] 已加载 {len(tasks)} 个定时任务")


# ── 公开 API（供 tools.py 调用）────────────────────────


def list_tasks(target_id: str) -> list[dict]:
    """列出指定目标的所有定时任务"""
    tasks = _load_tasks()
    return [t for t in tasks if t["target_id"] == target_id]


def delete_task(target_id: str, task_type: str) -> bool:
    """删除指定目标的指定类型定时任务，返回是否成功"""
    tasks = _load_tasks()
    new_tasks = []
    removed = False
    for task in tasks:
        if task["target_id"] == target_id and task["type"] == task_type:
            _unregister_job(task)
            removed = True
        else:
            new_tasks.append(task)
    if removed:
        _save_tasks(new_tasks)
    return removed


def add_task(target_id: str, detail_type: str, task_type: str,
             hour: int, minute: int, city: str = "") -> str:
    """添加定时任务，返回确认消息"""
    tasks = _load_tasks()
    # 检查重复，更新已有任务
    for task in tasks:
        if task["target_id"] == target_id and task["type"] == task_type:
            task["hour"] = hour
            task["minute"] = minute
            if city:
                task.setdefault("extra", {})["city"] = city
            _save_tasks(tasks)
            _unregister_job(task)
            _register_job(task)
            return f"已更新定时任务 → 每天 {hour:02d}:{minute:02d}"

    # 新增任务
    task = {
        "type": task_type,
        "target_id": target_id,
        "detail_type": detail_type,
        "hour": hour,
        "minute": minute,
        "extra": {"city": city} if city else {},
    }
    tasks.append(task)
    _save_tasks(tasks)
    _register_job(task)
    return f"已添加定时任务 → 每天 {hour:02d}:{minute:02d}"


# ── 命令解析 ────────────────────────────────────────────

_RE_ADD = re.compile(
    r"\\定时(新闻|热点|天气|epic|喜加一|免费游戏|油价|kfc|KFC|疯狂星期四)\s*(?:(周[一二三四五六日天])\s*)?(?:([一-鿿]{2,6})\s*)?(\d{1,2})[:\：]?(\d{2})?\s*点?"
)
_RE_DEL = re.compile(r"\\定时删除(新闻|热点|天气|epic|喜加一|免费游戏|油价|kfc|KFC|疯狂星期四|B站动态|b站动态)")
_RE_LIST = re.compile(r"\\定时列表")
_RE_BILI_SUB = re.compile(r"\\订阅动态\s*(\d+)\s*(?:(\d+)\s*分钟?)?")
_RE_BILI_UNSUB = re.compile(r"\\取消订阅动态\s*(\d+)?")
_RE_BILI_LIST = re.compile(r"\\订阅列表")


def _parse_time(hour_str: str, minute_str: Optional[str]) -> tuple[int, int]:
    h = int(hour_str)
    m = int(minute_str) if minute_str else 0
    return max(0, min(h, 23)), max(0, min(m, 59))


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
    return raw


def _type_label(t: str) -> str:
    return {"news": "热点新闻", "weather": "天气预报", "epic": "Epic喜加一",
            "oilprice": "每日油价", "kfc": "KFC疯狂星期四", "bili_dynamic": "B站动态订阅"}.get(t, t)


_DOW_MAP = {
    "周一": "mon", "周二": "tue", "周三": "wed", "周四": "thu",
    "周五": "fri", "周六": "sat", "周日": "sun", "周天": "sun",
}

_DOW_LABEL = {
    "mon": "周一", "tue": "周二", "wed": "周三", "thu": "周四",
    "fri": "周五", "sat": "周六", "sun": "周日",
}


async def handle_schedule_command(bot: Bot, event, text: str) -> Optional[str]:
    """处理定时任务命令，返回回复文本；非命令返回 None"""
    from nonebot.adapters.onebot.v12 import MessageEvent
    if not isinstance(event, MessageEvent):
        return None

    is_group = hasattr(event, 'group_id') and event.group_id
    target_id = str(event.group_id) if is_group else event.user_id
    detail_type = "group" if is_group else "private"

    # ── 列表 ──
    if _RE_LIST.search(text):
        tasks = _load_tasks()
        # 只显示当前聊天的定时任务
        mine = [t for t in tasks if t["target_id"] == target_id]
        if not mine:
            return "当前聊天暂无定时任务"
        lines = ["📋 定时任务列表:"]
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
        return "\n".join(lines)

    # ── 删除 ──
    m = _RE_DEL.search(text)
    if m:
        t = _normalize_type(m.group(1))
        tasks = _load_tasks()
        new_tasks = []
        removed = False
        for task in tasks:
            if task["target_id"] == target_id and task["type"] == t:
                _unregister_job(task)
                removed = True
            else:
                new_tasks.append(task)
        if removed:
            _save_tasks(new_tasks)
            return f"已删除「{_type_label(t)}」定时任务"
        return f"未找到「{_type_label(t)}」定时任务"

    # ── 添加 ──
    m = _RE_ADD.search(text)
    if m:
        t = _normalize_type(m.group(1))
        dow_raw = m.group(2) or ""
        city = m.group(3) or ""
        hour, minute = _parse_time(m.group(4), m.group(5))

        # 天气任务必须有城市
        if t == "weather" and not city:
            return "天气任务需要指定城市，如: /定时天气 绵阳 8点"

        # 油价任务必须有省份
        if t == "oilprice" and not city:
            return "油价任务需要指定省份，如: /定时油价 四川 8点"

        # KFC 默认周四
        day_of_week = ""
        if t == "kfc":
            day_of_week = _DOW_MAP.get(dow_raw, "thu")
        elif dow_raw:
            day_of_week = _DOW_MAP.get(dow_raw, "")

        tasks = _load_tasks()
        # 检查重复
        for task in tasks:
            if task["target_id"] == target_id and task["type"] == t:
                # 更新已有任务
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
                _unregister_job(task)
                _register_job(task)
                label = _type_label(t)
                suffix = f" ({city})" if city else ""
                dow_suffix = f" {_DOW_LABEL.get(day_of_week, dow_raw)}" if day_of_week else " 每天"
                return f"已更新「{label}{suffix}」→{dow_suffix} {hour:02d}:{minute:02d}"

        # 新增任务
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
            "target_id": target_id,
            "detail_type": detail_type,
            "hour": hour,
            "minute": minute,
            "extra": extra,
        }
        tasks.append(task)
        _save_tasks(tasks)
        _register_job(task)
        label = _type_label(t)
        suffix = f" ({city})" if city else ""
        dow_suffix = f" {_DOW_LABEL.get(day_of_week, dow_raw)}" if day_of_week else " 每天"
        return f"已添加「{label}{suffix}」→{dow_suffix} {hour:02d}:{minute:02d}"

    # ── B站动态订阅 ──
    m = _RE_BILI_SUB.search(text)
    if m:
        uid = m.group(1)
        interval = int(m.group(2)) if m.group(2) else 5
        interval = max(2, min(interval, 60))

        tasks = _load_tasks()
        # 检查是否已订阅该 uid
        for task in tasks:
            if (task["target_id"] == target_id and task["type"] == "bili_dynamic"
                    and task.get("extra", {}).get("uid") == uid):
                task["extra"]["interval"] = interval
                _save_tasks(tasks)
                _unregister_job(task)
                _register_job(task)
                return f"已更新B站动态订阅 UID:{uid} → 每{interval}分钟检查"

        task = {
            "type": "bili_dynamic",
            "target_id": target_id,
            "detail_type": detail_type,
            "hour": 0,
            "minute": 0,
            "extra": {"uid": uid, "interval": interval},
        }
        tasks.append(task)
        _save_tasks(tasks)
        _register_job(task)
        return f"已订阅B站动态 UID:{uid} → 每{interval}分钟检查\n有新动态会自动推送"

    # ── 取消B站动态订阅 ──
    m = _RE_BILI_UNSUB.search(text)
    if m:
        uid = m.group(1) or ""
        tasks = _load_tasks()
        new_tasks = []
        removed = False
        for task in tasks:
            if (task["target_id"] == target_id and task["type"] == "bili_dynamic"
                    and (not uid or task.get("extra", {}).get("uid") == uid)):
                _unregister_job(task)
                removed = True
            else:
                new_tasks.append(task)
        if removed:
            _save_tasks(new_tasks)
            return f"已取消B站动态订阅" + (f" UID:{uid}" if uid else "（全部）")
        return "未找到相关B站动态订阅"

    # ── 订阅列表 ──
    if _RE_BILI_LIST.search(text):
        tasks = _load_tasks()
        mine = [t for t in tasks if t["target_id"] == target_id and t["type"] == "bili_dynamic"]
        if not mine:
            return "当前聊天暂无B站动态订阅\n发送「/订阅动态 UID」可添加"
        lines = ["B站动态订阅列表:"]
        for t in mine:
            uid = t.get("extra", {}).get("uid", "?")
            interval = t.get("extra", {}).get("interval", 5)
            lines.append(f"  • UID:{uid}  每{interval}分钟")
        lines.append("\n发送「/取消订阅动态 UID」可取消")
        return "\n".join(lines)

    return None
