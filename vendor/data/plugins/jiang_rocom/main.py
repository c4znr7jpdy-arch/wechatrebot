"""
洛克王国 AstrBot Star 插件
适配层：将 ai_plugin/nonebot_plugin_rocom 的 RocomPlugin 方法桥接到 AstrBot
"""
import sys
import os
import asyncio
import logging
import types
from pathlib import Path

from astrbot.api import star
from astrbot.api.event import AstrMessageEvent, filter

logger = logging.getLogger("jiang_rocom")

# 将 ai_plugin 加入 sys.path
_AI_PLUGIN_DIR = str(Path(__file__).resolve().parent.parent.parent.parent / "ai_plugin")
if _AI_PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _AI_PLUGIN_DIR)

# NoneBot 桩
_PLUGINS_DIR = str(Path(__file__).resolve().parent.parent)
if _PLUGINS_DIR not in sys.path:
    sys.path.insert(0, _PLUGINS_DIR)
import _nonebot_stubs
_nonebot_stubs.setup()

# ── 导入 rocom 子模块 ──

from nonebot_plugin_rocom.client import RocomClient
from nonebot_plugin_rocom.user import UserManager, MerchantSubscriptionManager, HomeSubscriptionManager
from nonebot_plugin_rocom.renderer import Renderer
from nonebot_plugin_rocom.egg_service import EggService
from nonebot_plugin_rocom.config import RocomConfig


# ── 适配层 ──────────────────────────────────────────────

class FakeEvent:
    """模拟 NoneBot MessageEvent，提供 rocom 方法需要的属性"""
    def __init__(self, user_id: str, group_id: str = None, text: str = ""):
        self.user_id = user_id
        self.group_id = group_id
        self._text = text

    def get_plaintext(self):
        return self._text


class AstrBotMsgHelper:
    """适配 MsgHelper 接口，收集消息供 Star handler yield"""
    def __init__(self):
        self.messages = []  # list of (type, data)

    async def send(self, text: str):
        self.messages.append(("text", str(text)))

    async def send_image(self, file_path: str):
        self.messages.append(("image", file_path))

    async def send_image_from_base64(self, b64_data: str, suffix: str = ".png"):
        import base64, tempfile
        img_bytes = base64.b64decode(b64_data)
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(img_bytes)
            tmp_path = tmp.name
        self.messages.append(("image", tmp_path))
        return tmp_path

    async def finish(self, text: str):
        await self.send(text)


def _yield_collected(event: AstrMessageEvent, helper: AstrBotMsgHelper):
    """将收集到的消息转为 AstrBot result"""
    for msg_type, data in helper.messages:
        if msg_type == "text":
            yield event.plain_result(data)
        elif msg_type == "image":
            yield event.image_result(data)


# ── RocomPlugin 实例 ────────────────────────────────────

_rocom = None


def _get_rocom():
    global _rocom
    if _rocom is None:
        config = RocomConfig()
        _rocom = RocomPluginAdapter(config)
    return _rocom


class RocomPluginAdapter:
    """包装 RocomPlugin，管理生命周期"""
    def __init__(self, config: RocomConfig):
        self.config = config
        base_url = config.api_base_url
        wegame_api_key = config.wegame_api_key

        self.client = RocomClient(base_url=base_url, wegame_api_key=wegame_api_key)
        data_dir = str(config.data_dir)
        self.user_mgr = UserManager(data_dir)
        self.merchant_sub_mgr = MerchantSubscriptionManager(data_dir)
        self.home_sub_mgr = HomeSubscriptionManager(data_dir)

        res_path = str(Path(__file__).resolve().parent.parent.parent.parent / "ai_plugin" / "nonebot_plugin_rocom")
        self.renderer = Renderer(res_path=res_path, render_timeout=config.render_timeout)

        searcheggs_dir = os.path.join(res_path, "render", "searcheggs")
        self.egg_searcher = EggService(searcheggs_dir)

        self._merchant_subscription_task = None
        self._home_subscription_task = None

    async def start(self):
        self.renderer.start_cleanup()
        logger.info("[Rocom] 插件启动完成")

    async def stop(self):
        if self._merchant_subscription_task:
            self._merchant_subscription_task.cancel()
        if self._home_subscription_task:
            self._home_subscription_task.cancel()
        await self.renderer.close()
        await self.client.close()
        logger.info("[Rocom] 插件已关闭")


# ── 命令处理器 ──────────────────────────────────────────

async def _run_rocom_method(event: AstrMessageEvent, method_name: str, *args):
    """通用方法调用器"""
    rocom = _get_rocom()
    method = getattr(rocom, method_name, None) or getattr(_get_rocom_plugin(), method_name, None)
    if not method:
        yield event.plain_result(f"方法 {method_name} 未找到")
        return

    user_id = event.get_sender_id()
    group_id = event.get_group_id() if event.get_message_type() == "group" else None
    fake_event = FakeEvent(user_id=user_id, group_id=group_id, text=event.get_message_str())
    helper = AstrBotMsgHelper()

    try:
        await method(helper, fake_event, *args)
        for r in _yield_collected(event, helper):
            yield r
    except Exception as e:
        logger.exception(f"[Rocom] {method_name} 失败: {e}")
        yield event.plain_result(f"命令执行失败: {e}")


# 从原始 rocom 模块获取 RocomPlugin 类和 _send/_send_image
_rocom_plugin_instance = None

def _get_rocom_plugin():
    """获取原始 RocomPlugin 实例（单例）"""
    global _rocom_plugin_instance
    if _rocom_plugin_instance is None:
        import nonebot_plugin_rocom as rocom_mod
        _rocom_plugin_instance = rocom_mod._rocom
    return _rocom_plugin_instance


# ── AstrBot Star 插件 ───────────────────────────────────

class Main(star.Star):
    def __init__(self, context: star.Context) -> None:
        self.context = context

    async def initialize(self) -> None:
        try:
            plugin = _get_rocom_plugin()
            plugin.renderer.start_cleanup()
            logger.info("[Rocom] 洛克王国插件已加载")
        except Exception as e:
            logger.exception(f"[Rocom] 初始化失败: {e}")

    async def terminate(self) -> None:
        try:
            plugin = _get_rocom_plugin()
            await plugin.renderer.close()
            await plugin.client.close()
        except Exception:
            pass

    # ── 帮助 ──
    @filter.command("洛克")
    async def rocom_help(self, event: AstrMessageEvent):
        """洛克王国帮助"""
        async for r in _run_rocom_method(event, "rocom_help"):
            yield r

    # ── 登录 / 绑定 ──
    @filter.command("洛克QQ登录")
    async def rocom_qq_login(self, event: AstrMessageEvent):
        """QQ 扫码登录"""
        async for r in _run_rocom_method(event, "rocom_qq_login"):
            yield r

    @filter.command("洛克微信登录")
    async def rocom_wechat_login(self, event: AstrMessageEvent):
        """微信扫码登录"""
        async for r in _run_rocom_method(event, "rocom_wechat_login"):
            yield r

    @filter.command("洛克导入")
    async def rocom_import(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """通过凭证导入"""
        if not arg1 or not arg2:
            yield event.plain_result("用法: /洛克导入 <ID> <Ticket>")
            return
        async for r in _run_rocom_method(event, "rocom_import", arg1, arg2):
            yield r

    @filter.command("洛克绑定列表")
    async def rocom_bind_list(self, event: AstrMessageEvent):
        """查看绑定列表"""
        async for r in _run_rocom_method(event, "rocom_bind_list"):
            yield r

    @filter.command("洛克切换")
    async def rocom_switch(self, event: AstrMessageEvent, index: str = ""):
        """切换主账号"""
        if not index:
            yield event.plain_result("用法: /洛克切换 <序号>")
            return
        async for r in _run_rocom_method(event, "rocom_switch", index):
            yield r

    @filter.command("洛克解绑")
    async def rocom_unbind(self, event: AstrMessageEvent, index: str = ""):
        """解绑账号"""
        if not index:
            yield event.plain_result("用法: /洛克解绑 <序号>")
            return
        async for r in _run_rocom_method(event, "rocom_unbind", index):
            yield r

    @filter.command("洛克刷新")
    async def rocom_refresh(self, event: AstrMessageEvent):
        """刷新凭证"""
        async for r in _run_rocom_method(event, "rocom_refresh"):
            yield r

    @filter.command("洛克刷新所有凭证")
    async def rocom_refresh_all(self, event: AstrMessageEvent):
        """刷新所有凭证（管理员）"""
        async for r in _run_rocom_method(event, "rocom_refresh_all"):
            yield r

    @filter.command("洛克删除无效绑定")
    async def rocom_cleanup(self, event: AstrMessageEvent):
        """清理无效绑定（管理员）"""
        async for r in _run_rocom_method(event, "rocom_cleanup_bindings"):
            yield r

    # ── 数据查询 ──
    @filter.command("洛克档案")
    async def rocom_profile(self, event: AstrMessageEvent):
        """角色档案"""
        async for r in _run_rocom_method(event, "rocom_profile"):
            yield r

    @filter.command("洛克战绩")
    async def rocom_battle_record(self, event: AstrMessageEvent, page: str = ""):
        """战绩查询"""
        args = [page] if page else []
        async for r in _run_rocom_method(event, "rocom_battle_record", *args):
            yield r

    @filter.command("洛克背包")
    async def rocom_package(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """精灵收集"""
        args = [a for a in [arg1, arg2] if a]
        async for r in _run_rocom_method(event, "rocom_package", *args):
            yield r

    @filter.command("洛克阵容")
    async def rocom_lineup(self, event: AstrMessageEvent, arg1: str = "", arg2: str = ""):
        """阵容助手"""
        args = [a for a in [arg1, arg2] if a]
        async for r in _run_rocom_method(event, "rocom_lineup", *args):
            yield r

    @filter.command("查看阵容")
    async def rocom_lineup_detail(self, event: AstrMessageEvent, index: str = ""):
        """阵容详情"""
        if not index:
            yield event.plain_result("用法: /查看阵容 <序号>")
            return
        async for r in _run_rocom_method(event, "rocom_lineup_detail", index):
            yield r

    @filter.command("洛克交换大厅")
    async def rocom_exchange_hall(self, event: AstrMessageEvent, page: str = ""):
        """交换大厅"""
        args = [page] if page else []
        async for r in _run_rocom_method(event, "rocom_exchange_hall", *args):
            yield r

    @filter.command("远行商人")
    async def rocom_merchant(self, event: AstrMessageEvent):
        """远行商人"""
        async for r in _run_rocom_method(event, "rocom_merchant"):
            yield r

    @filter.command("洛克玩家")
    async def rocom_player_search(self, event: AstrMessageEvent, uid: str = ""):
        """玩家查询"""
        if not uid:
            yield event.plain_result("用法: /洛克玩家 <UID>")
            return
        async for r in _run_rocom_method(event, "rocom_player_search", uid):
            yield r

    @filter.command("洛克家园")
    async def rocom_home(self, event: AstrMessageEvent, uid: str = ""):
        """家园查询"""
        args = [uid] if uid else []
        async for r in _run_rocom_method(event, "rocom_home", *args):
            yield r

    @filter.command("洛克商店")
    async def rocom_shop(self, event: AstrMessageEvent, shop_id: str = ""):
        """商店查询"""
        args = [shop_id] if shop_id else []
        async for r in _run_rocom_method(event, "rocom_ingame_shop", *args):
            yield r

    @filter.command("洛克好友关系")
    async def rocom_friendship(self, event: AstrMessageEvent, ids: str = ""):
        """好友关系"""
        if not ids:
            yield event.plain_result("用法: /洛克好友关系 <id1,id2>")
            return
        async for r in _run_rocom_method(event, "rocom_friendship", ids):
            yield r

    @filter.command("洛克学生")
    async def rocom_student(self, event: AstrMessageEvent):
        """学生查询"""
        async for r in _run_rocom_method(event, "rocom_student"):
            yield r

    # ── 查蛋 / 配种 ──
    @filter.command("洛克查蛋")
    async def rocom_search_eggs(self, event: AstrMessageEvent, keyword: str = ""):
        """查蛋"""
        args = [keyword] if keyword else []
        async for r in _run_rocom_method(event, "rocom_search_eggs", *args):
            yield r

    @filter.command("洛克配种")
    async def rocom_breeding(self, event: AstrMessageEvent, a: str = "", b: str = ""):
        """配种查询"""
        if not a:
            yield event.plain_result("用法: /洛克配种 <精灵A> <精灵B>")
            return
        args = [a] + ([b] if b else [])
        async for r in _run_rocom_method(event, "rocom_breeding_check", *args):
            yield r

    # ── Wiki ──
    @filter.command("洛克wiki")
    async def rocom_wiki(self, event: AstrMessageEvent, name: str = ""):
        """精灵 Wiki"""
        args = [name] if name else []
        async for r in _run_rocom_method(event, "rocom_wiki", *args):
            yield r

    @filter.command("洛克技能")
    async def rocom_skill(self, event: AstrMessageEvent, name: str = ""):
        """技能 Wiki"""
        args = [name] if name else []
        async for r in _run_rocom_method(event, "rocom_skill", *args):
            yield r

    # ── 订阅 ──
    @filter.command("订阅远行商人")
    async def subscribe_merchant(self, event: AstrMessageEvent, arg: str = ""):
        """订阅远行商人"""
        args = [a for a in arg.split() if a] if arg else []
        async for r in _run_rocom_method(event, "subscribe_merchant", *args):
            yield r

    @filter.command("取消订阅远行商人")
    async def unsubscribe_merchant(self, event: AstrMessageEvent):
        """取消订阅远行商人"""
        async for r in _run_rocom_method(event, "unsubscribe_merchant"):
            yield r

    @filter.command("订阅家园菜园")
    async def subscribe_home_garden(self, event: AstrMessageEvent, uid: str = ""):
        """订阅家园菜园"""
        args = [uid] if uid else []
        async for r in _run_rocom_method(event, "subscribe_home_garden", *args):
            yield r

    @filter.command("订阅家园灵感")
    async def subscribe_home_inspiration(self, event: AstrMessageEvent, uid: str = ""):
        """订阅家园灵感"""
        args = [uid] if uid else []
        async for r in _run_rocom_method(event, "subscribe_home_inspiration", *args):
            yield r

    @filter.command("取消订阅家园")
    async def unsubscribe_home(self, event: AstrMessageEvent, arg: str = ""):
        """取消订阅家园"""
        args = [a for a in arg.split() if a] if arg else []
        async for r in _run_rocom_method(event, "unsubscribe_home", *args):
            yield r
