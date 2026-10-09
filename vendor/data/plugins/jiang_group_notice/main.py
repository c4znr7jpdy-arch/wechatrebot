"""
群成员变动通知插件 — 入群欢迎卡片 + 退群告别卡片
通过 CustomFilter 监听 OneBot notice 事件
"""
import logging

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.filter.custom_filter import CustomFilter
from astrbot.core.config import AstrBotConfig

logger = logging.getLogger("jiang_group_notice")

DEFAULT_IMAGE = "https://mmbiz.qpic.cn/mmbiz_png/0k0b2mMEP3ncJpLbGvicjU9TMYLibArPDTzSAQqGn1zBHXhKWicicFvvia2sH3ib8ibVdtJtaRa1NQxoEBVK1rib7QBIGA/640"


def _action_data(result) -> dict:
    if not isinstance(result, dict):
        return {}
    data = result.get("data")
    return data if isinstance(data, dict) else result


def _member_desc(display_name: str, nickname: str, tail_line: str) -> str:
    display_name = str(display_name or "").strip()
    nickname = str(nickname or "").strip()
    tail_line = str(tail_line or "").strip()
    lines = []
    if display_name:
        lines.append(f"群昵称: {display_name}")
    if nickname:
        lines.append(f"名称: {nickname}")
    if not lines:
        lines.append("名称: 未知成员")
    if tail_line:
        lines.append(tail_line)
    return "\n".join(lines)


class _NoticeFilter(CustomFilter):
    """匹配指定 notice_type 的 OneBot 通知事件"""
    def __init__(self, notice_type: str, raise_error: bool = False):
        super().__init__(raise_error=raise_error)
        self._notice_type = notice_type

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        raw = event.message_obj.raw_message
        if not raw:
            return False
        return (
            raw.get("post_type") == "notice"
            and raw.get("notice_type") == self._notice_type
        )


class GroupIncreaseFilter(_NoticeFilter):
    def __init__(self, raise_error: bool = False):
        super().__init__("group_increase", raise_error=raise_error)


class GroupDecreaseFilter(_NoticeFilter):
    def __init__(self, raise_error: bool = False):
        super().__init__("group_decrease", raise_error=raise_error)


class GroupMemberUpdateFilter(_NoticeFilter):
    def __init__(self, raise_error: bool = False):
        super().__init__("group_member_update", raise_error=raise_error)


async def _send_link_card(event: AstrMessageEvent, title: str, desc: str, url: str, image_url: str):
    """通过 bot.call_action 发送 wechat_link_card"""
    msg_obj = event.message_obj
    group_id = msg_obj.group_id if msg_obj else None
    if not group_id:
        return

    # 尝试转 int（QQ 场景），微信保持字符串
    group_id_typed = int(group_id) if isinstance(group_id, str) and group_id.isdigit() else group_id

    card_seg = {
        "type": "wechat_link_card",
        "data": {
            "title": title,
            "desc": desc,
            "url": url,
            "image_url": image_url,
        },
    }
    try:
        await event.bot.call_action(
            action="send_group_msg",
            group_id=group_id_typed,
            message=[card_seg],
        )
        logger.info("群成员通知卡片已发送: group=%s title=%s", group_id, title)
    except Exception as e:
        logger.error(f"发送通知卡片失败: {e}")


async def _query_user_avatar(event: AstrMessageEvent, user_id: str) -> str:
    """Resolve the avatar through the individual contact API.

    Current WeChat group snapshots often omit avatar even though 11029 still
    returns it, so group-card rendering must not rely on member_list alone.
    """
    if not user_id:
        return ""
    try:
        user_info = await event.bot.call_action(
            action="get_user_info",
            user_id=user_id,
        )
        user_info = _action_data(user_info)
        return str(user_info.get("avatar", "") or "")
    except Exception as e:
        logger.warning(f"查询成员头像失败: {e}")
        return ""


class Main(star.Star):
    def __init__(self, context: star.Context, config: AstrBotConfig = None) -> None:
        self.context = context
        self.config = config or {}

    def _get_config_value(self, key: str, default: str) -> str:
        try:
            value = self.config.get(key, default)
        except Exception:
            value = default
        return str(value if value is not None else default)

    @staticmethod
    def _format_card_text(template: str, *, group_name: str, display_name: str, nickname: str, user_id: str) -> str:
        values = {
            "group_name": group_name,
            "display_name": display_name,
            "nickname": nickname,
            "user_id": user_id,
        }
        try:
            return str(template or "").format(**values)
        except Exception:
            logger.warning("群通知卡片配置模板格式错误，将按原文本发送: %s", template)
            return str(template or "")

    @filter.custom_filter(GroupIncreaseFilter, priority=1000)
    async def on_member_joined(self, event: AstrMessageEvent):
        """入群欢迎卡片"""
        raw = event.message_obj.raw_message
        user_id = str(raw.get("user_id", ""))
        group_id = str(raw.get("group_id", ""))
        wx_nickname = raw.get("wx_nickname", "新成员")
        wx_display_name = raw.get("wx_display_name", "")
        wx_group_name = raw.get("wx_group_name", "本群")
        wx_avatar = raw.get("wx_avatar", "")
        logger.info("收到入群通知: group=%s user=%s", group_id, user_id)

        title_template = self._get_config_value("welcome_title", "🎉 欢迎新成员加入！")
        tail_template = self._get_config_value("welcome_tail", "欢迎来到 {group_name}~")
        title = self._format_card_text(
            title_template,
            group_name=wx_group_name,
            display_name=wx_display_name,
            nickname=wx_nickname,
            user_id=user_id,
        )
        tail_line = self._format_card_text(
            tail_template,
            group_name=wx_group_name,
            display_name=wx_display_name,
            nickname=wx_nickname,
            user_id=user_id,
        )
        desc = _member_desc(wx_display_name, wx_nickname, tail_line)
        url = "https://weixin.qq.com"

        image_url = wx_avatar
        if not image_url and user_id and group_id:
            try:
                user_info = await event.bot.call_action(
                    action="get_group_member_info",
                    group_id=group_id,
                    user_id=user_id,
                )
                user_info = _action_data(user_info)
                if user_info.get("avatar"):
                    image_url = user_info["avatar"]
            except Exception as e:
                logger.warning(f"查询新成员头像失败: {e}")

        if not image_url:
            image_url = await _query_user_avatar(event, user_id)

        if not image_url:
            image_url = DEFAULT_IMAGE

        await _send_link_card(event, title, desc, url, image_url)
        # Notice events have an empty message chain.  Finish them here before
        # broad message handlers can consume/stop the event ahead of this
        # notification plugin.
        event.stop_event()

    @filter.custom_filter(GroupMemberUpdateFilter, priority=1000)
    async def on_member_updated(self, event: AstrMessageEvent):
        """群成员昵称/资料修改卡片"""
        raw = event.message_obj.raw_message
        old_display_name = str(raw.get("wx_old_display_name", "") or "")
        old_nickname = str(raw.get("wx_old_nickname", "") or "")
        wx_display_name = str(raw.get("wx_display_name", "") or "")
        wx_nickname = str(raw.get("wx_nickname", "") or "")
        wx_group_name = str(raw.get("wx_group_name", "本群") or "本群")
        wx_avatar = raw.get("wx_avatar", "")
        user_id = str(raw.get("user_id", "") or "")

        old_label = old_display_name or old_nickname or "未设置"
        new_label = wx_display_name or wx_nickname or "未设置"
        title = "✏️ 有成员修改了群昵称"
        desc = f"原群昵称: {old_label}\n新群昵称: {new_label}"
        url = "https://weixin.qq.com"
        image_url = wx_avatar or await _query_user_avatar(event, user_id) or DEFAULT_IMAGE

        await _send_link_card(event, title, desc, url, image_url)
        event.stop_event()

    @filter.custom_filter(GroupDecreaseFilter, priority=1000)
    async def on_member_left(self, event: AstrMessageEvent):
        """退群告别卡片"""
        raw = event.message_obj.raw_message
        user_id = str(raw.get("user_id", ""))
        wx_nickname = raw.get("wx_nickname", "某成员")
        wx_display_name = raw.get("wx_display_name", "")
        wx_group_name = raw.get("wx_group_name", "本群")
        wx_avatar = raw.get("wx_avatar", "")
        logger.info("收到退群通知: group=%s user=%s", raw.get("group_id", ""), user_id)

        title = "👋 有成员离开了群聊"
        desc = _member_desc(wx_display_name, wx_nickname or user_id, f"离开了 {wx_group_name}")
        url = "https://weixin.qq.com"

        image_url = wx_avatar
        if not image_url and user_id:
            image_url = await _query_user_avatar(event, user_id)

        if not image_url:
            image_url = DEFAULT_IMAGE

        await _send_link_card(event, title, desc, url, image_url)
        event.stop_event()
