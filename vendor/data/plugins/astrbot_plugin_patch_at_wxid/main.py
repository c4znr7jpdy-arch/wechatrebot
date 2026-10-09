"""
兼容微信 wxid 形态的 at 段补丁。

问题：Py 端（WeChat 注入层）把 @机器人 转成 OneBot V11 at 段时，
qq 字段填的是 wxid（如 wxid_trq1c905eckq22），不是数字 QQ 号。
AstrBot 的 aiocqhttp 适配器强制 int(qq) 调 get_group_member_info，
wxid 非数字直接抛异常，被 except BaseException 兜住但打 ERROR 日志。

更关键的：waking_check 阶段靠 At 段的 qq == self_id 判断群聊唤醒，
如果简单过滤掉 wxid at 段，机器人就不响应 @ 了。

本插件 monkeypatch 适配器的 _convert_handle_message_event：
保留 wxid at 段（确保唤醒判断有效），但在调用原方法前临时给 bot.call_action
打补丁——当 user_id 是 wxid（非数字）时直接返回空 dict，让适配器走兜底分支
(At(qq=..., name=""))，不打 ERROR。
"""
import logging
from astrbot.api import star
from astrbot.api.star import register
from astrbot.api import logger as astrbot_logger

logger = logging.getLogger("patch_at_wxid")

_PATCHED_FLAG = "_wxid_at_compat_patched"


def _apply_patch() -> bool:
    try:
        from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_platform_adapter import (
            AiocqhttpAdapter,
        )
    except ImportError as e:
        logger.warning(f"[patch_at_wxid] 无法导入 AiocqhttpAdapter: {e}")
        return False

    if getattr(AiocqhttpAdapter, _PATCHED_FLAG, False):
        return True

    original = AiocqhttpAdapter._convert_handle_message_event

    async def patched(self, event, get_reply=True):
        bot = getattr(self, "bot", None)
        if bot is None:
            return await original(self, event, get_reply=get_reply)

        original_call_action = getattr(bot, "call_action", None)
        if not callable(original_call_action):
            return await original(self, event, get_reply=get_reply)

        async def safe_call_action(action, **params):
            user_id = params.get("user_id")
            if user_id is not None and not str(user_id).isdigit():
                if action in ("get_group_member_info", "get_stranger_info"):
                    return {}
            return await original_call_action(action, **params)

        bot.call_action = safe_call_action
        try:
            return await original(self, event, get_reply=get_reply)
        finally:
            bot.call_action = original_call_action

    AiocqhttpAdapter._convert_handle_message_event = patched
    setattr(AiocqhttpAdapter, _PATCHED_FLAG, True)
    logger.info("[patch_at_wxid] 已为 AiocqhttpAdapter._convert_handle_message_event 应用 wxid at 兼容补丁")
    return True


@register(
    "astrbot_plugin_patch_at_wxid",
    "fch",
    "兼容微信 wxid 形态的 at 段，避免 aiocqhttp 适配器 int() 报错日志",
    "0.1.0",
    "",
)
class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def initialize(self) -> None:
        ok = _apply_patch()
        if not ok:
            astrbot_logger.warning("[patch_at_wxid] 补丁未生效，wxid 形态 at 段可能仍报 ERROR")

    async def terminate(self) -> None:
        pass
