from nonebot import on_notice, logger, Bot
from nonebot.adapters.onebot.v12 import NoticeEvent, MessageSegment

DEFAULT_IMAGE = "https://mmbiz.qpic.cn/mmbiz_png/0k0b2mMEP3ncJpLbGvicjU9TMYLibArPDTzSAQqGn1zBHXhKWicicFvvia2sH3ib8ibVdtJtaRa1NQxoEBVK1rib7QBIGA/640"

# 匹配入群事件
notice_join = on_notice()
# 匹配退群事件 
notice_leave = on_notice()

@notice_join.handle()
async def handle_group_increase(bot: Bot, event: NoticeEvent):
    if event.detail_type != "group_member_increase":
        return

    user_id = getattr(event, "user_id", "")
    group_id = getattr(event, "group_id", "")
    wx_nickname = getattr(event, "wx_nickname", "新成员")
    wx_group_name = getattr(event, "wx_group_name", "本群")
    wx_avatar = getattr(event, "wx_avatar", "")

    title = "🎉 欢迎新成员加入！"
    # 入群时不显示 wxid
    desc = f"昵称: {wx_nickname}\n欢迎来到 {wx_group_name}~"
    url = "https://weixin.qq.com"

    # 优先使用事件中携带的头像，否则尝试通过 API 查询
    image_url = wx_avatar
    if not image_url and user_id and group_id:
        try:
            user_info = await bot.call_api("get_group_member_info", group_id=group_id, user_id=user_id)
            if user_info and user_info.get("avatar"):
                image_url = user_info["avatar"]
        except Exception as e:
            logger.warning(f"查询新成员头像失败（将使用默认图）: {e}")

    if not image_url:
        image_url = DEFAULT_IMAGE

    card_seg = MessageSegment(
        type="wechat_link_card", 
        data={
            "title": title,
            "desc": desc,
            "url": url,
            "image_url": image_url
        }
    )
    try:
        await bot.send_message(
            detail_type="group",
            group_id=group_id,
            message=card_seg
        )
    except Exception as e:
        logger.error(f"发送通知失败: {e}")

@notice_leave.handle()
async def handle_group_decrease(bot: Bot, event: NoticeEvent):
    if event.detail_type != "group_member_decrease":
        return

    user_id = getattr(event, "user_id", "")
    group_id = getattr(event, "group_id", "")
    wx_nickname = getattr(event, "wx_nickname", "某成员")
    wx_group_name = getattr(event, "wx_group_name", "本群")
    wx_avatar = getattr(event, "wx_avatar", "")

    title = "👋 有成员离开了群聊"
    # 退群时显示 wxid
    desc = f"昵称: {wx_nickname}\nWxID: {user_id}\n离开了 {wx_group_name}"
    url = "https://weixin.qq.com"

    # 退群人已经不在群里了，所以优先用桥接层缓存的头像
    image_url = wx_avatar 

    if not image_url and user_id:
        try:
            # 假如是老成员直接退群（没在缓存里），用 11029 (get_user_info) API 再捞一下
            user_info = await bot.call_api("get_user_info", user_id=user_id)
            if user_info and user_info.get("avatar"):
                image_url = user_info["avatar"]
        except Exception as e:
            logger.warning(f"尝试按离群者wxid强行获取头像失败（将使用默认图）: {e}")

    if not image_url:
        image_url = DEFAULT_IMAGE

    card_seg = MessageSegment(
        type="wechat_link_card", 
        data={
            "title": title,
            "desc": desc,
            "url": url,
            "image_url": image_url
        }
    )
    try:
        await bot.send_message(
            detail_type="group",
            group_id=group_id,
            message=card_seg
        )
    except Exception as e:
        logger.error(f"发送通知失败: {e}")
