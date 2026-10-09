"""
复读机插件 — 群内连续两条相同的普通聊天自动复读
规则：
1. 两位不同用户在 60 秒内连续发送相同文本时触发
2. @消息、命令、引用/卡片、媒体消息和机器人自身消息不参与复读
3. 不参与复读的消息会打断连续判定
4. 复读后冷却 2 分钟
"""
import json
import logging
import re
from collections import defaultdict
from time import time

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.event.filter import EventMessageType

logger = logging.getLogger("jiang_repeater")

# 每个群的最近消息: [(user_id, text, timestamp), ...]
_group_history: dict[str, list[tuple[str, str, float]]] = defaultdict(list)

# 每个群的冷却时间戳
_cooldown: dict[str, float] = {}

COOLDOWN_SECONDS = 120
REPEAT_WINDOW_SECONDS = 60

_COMMAND_PREFIXES = ("/", "\\", "#")
_PLAIN_COMMAND_RE = re.compile(
    r"^(?:点歌|网易点歌|nj点歌|星之阁点歌|波点点歌|汽水点歌)(?:\s|$)",
    re.IGNORECASE,
)
_INTERNAL_TEXT_MARKERS = (
    "[引用消息]",
    "[引用 ",
    "[转发聊天记录",
    "[FORWARD ",
)
_SYSTEM_MEDIA_PLACEHOLDERS = frozenset(
    {
        "[表情]",
        "[语音]",
        "[视频]",
        "[文件或应用消息]",
        "[图片下载失败，无法识图，请勿猜测图片内容]",
        "[拍一拍]",
    }
)
_QUOTED_IMAGE_FAILURE = "[引用图片下载失败，无法识图，请勿猜测图片内容]"
_UNSUPPORTED_MESSAGE_RE = re.compile(r"^\[不支持的消息类型:\d+\]$")


def _strip_system_identity_prompt(text: str) -> str:
    """移除消息管线注入的身份提示，只保留用户实际发送的内容。"""
    cleaned = str(text or "").strip()
    marker = "[系统身份提示："
    start = cleaned.find(marker)
    if start < 0:
        return cleaned

    # 当前身份提示以 ] 后换行结束；兼容 Windows 和 Unix 换行。
    end_match = None
    for terminator in ("]\r\n", "]\n"):
        end = cleaned.find(terminator, start)
        if end >= 0 and (end_match is None or end < end_match[0]):
            end_match = (end, len(terminator))
    if end_match is None:
        return cleaned

    end, terminator_length = end_match
    return (cleaned[:start] + cleaned[end + terminator_length :]).strip()


def _component_type(component) -> str:
    """返回统一的小写消息段类型，兼容枚举和字符串实现。"""
    value = getattr(component, "type", "")
    value = getattr(value, "value", value)
    return str(value or "").strip().lower()


def _get_message_components(event: AstrMessageEvent) -> list:
    getter = getattr(event, "get_messages", None)
    if not callable(getter):
        return []
    try:
        components = getter()
    except Exception:
        return []
    return list(components or [])


def _contains_quote_metadata(text: str) -> bool:
    """识别桥接层嵌入正文首行的引用元数据，避免内部字段被复读。"""
    first_line = next(
        (line.strip() for line in (text or "").splitlines() if line.strip()),
        "",
    )
    if first_line.startswith("[引用消息]"):
        first_line = first_line.removeprefix("[引用消息]").strip()
    if not first_line.startswith("{"):
        return False
    try:
        payload = json.loads(first_line)
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and bool(
        {"qt", "qs", "qsw", "qtxt"} & payload.keys()
    )


def _is_system_media_placeholder(text: str) -> bool:
    """识别桥接层将非文字消息降级后生成的 Plain 占位文本。"""
    stripped = str(text or "").strip()
    first_line = stripped.splitlines()[0].strip() if stripped else ""
    return (
        stripped in _SYSTEM_MEDIA_PLACEHOLDERS
        or first_line == _QUOTED_IMAGE_FAILURE
        or bool(_UNSUPPORTED_MESSAGE_RE.fullmatch(stripped))
    )


def _repeat_block_reason(event: AstrMessageEvent, text: str) -> str | None:
    """返回消息不可参与复读的原因；None 表示可参与。"""
    if not text:
        return "empty"
    if len(text) > 500:
        return "too_long"
    if _is_system_media_placeholder(text):
        return "system_media_placeholder"

    sender_id = str(event.get_sender_id() or "")
    self_id = str(event.get_self_id() or "")
    if self_id and sender_id == self_id:
        return "self_message"

    components = _get_message_components(event)
    if components and any(_component_type(item) != "plain" for item in components):
        return "non_plain_component"

    if bool(getattr(event, "is_at_or_wake_command", False)):
        return "directed_or_wake_command"

    stripped = text.lstrip()
    if stripped.startswith(_COMMAND_PREFIXES) or _PLAIN_COMMAND_RE.match(stripped):
        return "command"

    if _contains_quote_metadata(stripped):
        return "quote_metadata"
    if any(marker in stripped for marker in _INTERNAL_TEXT_MARKERS):
        return "quote_or_forward"

    lowered = stripped.lower()
    if "<appmsg" in lowered or "&lt;appmsg" in lowered:
        return "app_card_xml"

    return None


class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    @filter.event_message_type(EventMessageType.GROUP_MESSAGE)
    async def on_group_message(self, event: AstrMessageEvent):
        """监听群消息，只让普通聊天参与连续复读。"""
        text = _strip_system_identity_prompt(event.get_message_str())
        gid = event.get_group_id()
        uid = event.get_sender_id()
        now = time()

        block_reason = _repeat_block_reason(event, text)
        if block_reason:
            # “连续两条”必须真的是连续普通聊天；命令、@、卡片等都应打断历史。
            _group_history.pop(gid, None)
            logger.debug(
                "群 %s 忽略复读候选: reason=%s, sender=%s, text=%r",
                gid,
                block_reason,
                uid,
                text[:80],
            )
            return

        # 检查冷却
        if now - _cooldown.get(gid, 0) < COOLDOWN_SECONDS:
            return

        history = _group_history[gid]

        # 保持最近 2 条
        history.append((uid, text, now))
        if len(history) > 2:
            history.pop(0)

        if len(history) < 2:
            return

        (uid1, text1, time1), (uid2, text2, time2) = history

        # 即使期间没有其他普通聊天，过久的两条消息也不算连续复读。
        if time2 - time1 > REPEAT_WINDOW_SECONDS:
            history[:] = [history[-1]]
            return

        # 两条消息内容相同
        if text1 != text2:
            return

        # 同一用户连续发相同内容，不触发
        if uid1 == uid2:
            return

        # 触发复读
        _cooldown[gid] = time()
        _group_history[gid].clear()
        logger.info("群 %s 触发复读，发送者 %s -> %s，内容=%r", gid, uid1, uid2, text)
        yield event.plain_result(text)
