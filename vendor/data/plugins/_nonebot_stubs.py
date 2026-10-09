"""
NoneBot 桩模块 — 使 NoneBot 插件可在 AstrBot 环境下导入
所有需要 NoneBot 桩的插件在顶部 import 此模块即可
"""
import sys
import types
import logging


class _Stub:
    """通用桩，支持任意属性访问、调用链和布尔判断"""
    def __getattr__(self, name):
        return _Stub()
    def __call__(self, *args, **kwargs):
        return _Stub()
    def __bool__(self):
        return False


def setup():
    """注册所有 NoneBot 桩模块到 sys.modules"""
    logger = logging.getLogger("nonebot_stub")

    # nonebot 主模块
    if "nonebot" not in sys.modules:
        nb = types.ModuleType("nonebot")
        nb.on_command = lambda *a, **kw: _Stub()
        nb.on_regex = lambda *a, **kw: _Stub()
        nb.on_message = lambda *a, **kw: _Stub()
        nb.on_notice = lambda *a, **kw: _Stub()
        nb.on_request = lambda *a, **kw: _Stub()
        nb.get_driver = lambda: _Stub()
        nb.require = lambda *a, **kw: None
        nb.Adapter = type("Adapter", (), {})
        nb.Bot = type("Bot", (), {})
        nb.logger = logger
        sys.modules["nonebot"] = nb
    else:
        nb = sys.modules["nonebot"]
        for attr in ["on_command", "on_regex", "on_message", "on_notice", "on_request", "get_driver", "require"]:
            if not hasattr(nb, attr):
                setattr(nb, attr, lambda *a, **kw: _Stub())
        for attr in ["Adapter", "Bot"]:
            if not hasattr(nb, attr):
                setattr(nb, attr, type(attr, (), {}))
        if not hasattr(nb, "logger"):
            nb.logger = logger

    # nonebot 子模块
    _submodules = {
        "nonebot.log": {"logger": logger, "default_format": "{time} | {level} | {message}"},
        "nonebot.plugin": {},
        "nonebot.rule": {"Rule": lambda *a, **kw: _Stub()},
        "nonebot.params": {"CommandArg": lambda: None, "T_State": dict, "ArgPlainText": str, "Command": str},
        "nonebot.matcher": {"Matcher": type("Matcher", (), {})},
        "nonebot.internal": {},
        "nonebot.internal.matcher": {"Matcher": type("Matcher", (), {})},
        "nonebot.internal.params": {"ArgStr": str},
        "nonebot.adapters": {},
        "nonebot.adapters.onebot": {},
        "nonebot.adapters.onebot.v11": {
            "MessageEvent": type("MessageEvent", (), {}),
            "PrivateMessageEvent": type("PrivateMessageEvent", (), {}),
            "GroupMessageEvent": type("GroupMessageEvent", (), {}),
            "NoticeEvent": type("NoticeEvent", (), {}),
            "RequestEvent": type("RequestEvent", (), {}),
            "Adapter": type("Adapter", (), {}),
            "Bot": type("Bot", (), {}),
            "MessageSegment": type("MessageSegment", (), {}),
        },
        "nonebot.adapters.onebot.v12": {
            "MessageEvent": type("MessageEvent", (), {}),
            "PrivateMessageEvent": type("PrivateMessageEvent", (), {}),
            "GroupMessageEvent": type("GroupMessageEvent", (), {}),
            "NoticeEvent": type("NoticeEvent", (), {}),
            "RequestEvent": type("RequestEvent", (), {}),
            "Adapter": type("Adapter", (), {}),
            "Bot": type("Bot", (), {}),
            "MessageSegment": type("MessageSegment", (), {"__init__": lambda self, *a, **kw: None}),
        },
        "nonebot.adapters.qq": {
            "DirectMessageCreateEvent": type("DirectMessageCreateEvent", (), {}),
            "MessageCreateEvent": type("MessageCreateEvent", (), {}),
            "Adapter": type("Adapter", (), {}),
            "Bot": type("Bot", (), {}),
            "MessageEvent": type("MessageEvent", (), {}),
            "MessageSegment": type("MessageSegment", (), {}),
        },
        "nonebot.adapters.qq.exception": {
            "ActionFailed": type("ActionFailed", (Exception,), {}),
        },
        "nonebot_plugin_saa": {
            "MessageSegmentFactory": type("MessageSegmentFactory", (), {}),
            "Text": type("Text", (), {}),
            "AggregatedMessageFactory": type("AggregatedMessageFactory", (), {}),
            "TargetQQPrivate": type("TargetQQPrivate", (), {}),
            "TargetQQGuildDirect": type("TargetQQGuildDirect", (), {}),
            "enable_auto_select_bot": lambda: None,
        },
        "nonebot_plugin_apscheduler": {
            "scheduler": _Stub(),
        },
    }

    for mod_name, attrs in _submodules.items():
        if mod_name not in sys.modules:
            mod = types.ModuleType(mod_name)
            for k, v in attrs.items():
                setattr(mod, k, v)
            sys.modules[mod_name] = mod
        else:
            mod = sys.modules[mod_name]
            for k, v in attrs.items():
                if not hasattr(mod, k):
                    setattr(mod, k, v)
