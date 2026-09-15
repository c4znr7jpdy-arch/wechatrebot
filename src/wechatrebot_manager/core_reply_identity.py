"""Apply the conversation core's per-chat identity without losing system context."""
from __future__ import annotations

import re
from typing import Any


_START = "[robot_core_profile]"
_END = "[/robot_core_profile]"
_PROFILE = re.compile(re.escape(_START) + r".*?" + re.escape(_END), re.DOTALL)


def apply_reply_identity(request: Any, config: Any) -> None:
    identity = str(config.ai_self_identity or "").strip()
    guide = str(config.reply_strategy_guide or "").strip()
    aliases = str(config.alias or "").strip()
    original = str(getattr(request, "system_prompt", "") or "")
    # Retry/rewrite hooks may see the same request again. Replace only our own
    # section, retaining unrelated safety rules, memory and tool instructions.
    original = _PROFILE.sub("", original).rstrip()
    fields = []
    if aliases:
        name = next((part.strip() for part in aliases.split("|") if part.strip()), "")
        fields.append(f"聊天称呼：{name}。可识别昵称：{aliases}。")
    if identity:
        fields.append(f"身份与表达：\n{identity}")
    if guide:
        fields.append(f"接话与表达规则：\n{guide}")
    if not fields:
        request.system_prompt = original
        return
    profile = "\n".join([
        _START,
        "以下是本次聊天的当前机器人核心配置。身份、称呼与表达以此为准，不以管理界面名称或此前的旧角色称呼为准。其他安全、工具和能力边界仍按原有要求执行。",
        *fields,
        _END,
    ])
    request.system_prompt = f"{original}\n\n{profile}" if original else profile
