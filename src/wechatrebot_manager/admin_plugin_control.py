"""Administrator-only access to the current group's existing feature policy."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, TYPE_CHECKING

from .customer_config import load_robot_admin_ids
from .plugin_errors import PluginCenterError

if TYPE_CHECKING:
    from .plugin_center import PluginCenterService


CONTROL_PATH = "/internal/plugin-control"
CONTROL_DESCRIPTOR = "data/manager/plugin-control.json"
_ENABLE = re.compile(r"开启|启用|打开|启动|恢复|开起来|打开来")
_DISABLE = re.compile(r"关闭|禁用|停用|关掉|停掉|关了|关起来")
_NON_ACTION = re.compile(
    r"不要|别|勿|禁止|不准|不必|不用|无需|不许|不想|不需要|不再|不.*(?:开|关|启|停)|没让|没叫|未要求|"
    r"怎么|如何|是否|是不是|有没有|已经|了吗|了没|能否|能不能|可不可以|可以.*吗|如果|假如|"
    r"引用|转发|他说|她说|曾经|上次|之前|昨天|当时|什么意思|是什么意思|含义|解释|举例|例如|比如|示例|这句话|"
    r"明天|后天|稍后|以后|过会|待会|等.*再"
)
_FOREIGN_SCOPE = re.compile(r"全局|全部|所有|其它|其他|别的|另一个|隔壁|私聊|@chatroom")
_CURRENT_GROUP = re.compile(r"当前群聊|当前群|这个群聊|这个群|本群聊|本群|该群聊|该群|此群|群聊内|群内|群里")
_PLUGIN_ALIASES = {
    "jiang_commands.setu": ["涩图", "色图"],
    "astrbot_plugin_proactive_chat": ["主动消息", "主动关心", "主动问候", "主动陪聊"],
}
_COMMAND_WORDS = re.compile(r"(?:请|麻烦|帮我|帮忙|劳烦|给我|把|将|在|的|插件|功能|使用权限|权限|开关|一下|吧|呀|啊|谢谢|[，,。.!！:：])*")


def _names(plugin: Mapping[str, Any]) -> list[str]:
    names = [plugin.get(key) for key in ("name", "module", "id")]
    names.extend(plugin.get("aliases") or [])
    return [re.sub(r"\s+", "", str(name)).casefold() for name in names if name]


def _validate_scope_text(message: object, catalog: list[dict[str, object]]) -> str:
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise PluginCenterError("请在当前群聊中明确说明要管理哪个插件。")
    text = re.sub(r"[^\S\n]+", "", message).casefold()
    if _FOREIGN_SCOPE.search(text):
        raise PluginCenterError("这里只能管理当前群聊，请在目标群中说“关闭点歌”或“开启点歌”。")
    scope_text = text
    # Feature names such as 群聊分析 are targets, not a requested group scope.
    for name in sorted({name for item in catalog for name in _names(item)}, key=len, reverse=True):
        scope_text = scope_text.replace(name, "")
    if "群" in _CURRENT_GROUP.sub("", scope_text):
        raise PluginCenterError("无法确认目标是当前群聊，请直接说“在本群关闭点歌”。")
    return text


def _validate_intent(text: str, plugin: Mapping[str, Any], enabled: bool) -> None:
    names = _names(plugin)
    # Quoted feature names are fine; quoted instructions are not an action.
    for name in names:
        for left, right in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("`", "`")):
            text = text.replace(f"{left}{name}{right}", name)
    intended, opposite = (_ENABLE, _DISABLE) if enabled else (_DISABLE, _ENABLE)
    if (_NON_ACTION.search(text) or re.search(r'''[“”‘’"'`「」?？]|[吗么呢]''', text)
            or not intended.search(text) or opposite.search(text)):
        raise PluginCenterError("当前消息未明确要求这次操作，请直接说“在本群开启点歌”或“在本群关闭点歌”。")
    clauses = re.split(r"[，,。；;！!\n]|保留|保持|维持|别动|不要动|不用动|不动|但", text)
    if not any(
        intended.search(clause) and any(
            re.search(r"(?<![a-z0-9_.-])" + re.escape(name) + r"(?![a-z0-9_.-])", clause)
            if name.isascii() else name in clause for name in names
        ) for clause in clauses
    ):
        raise PluginCenterError("当前消息中的插件与操作目标不一致，请使用插件列表里的名称重试。")


def _resolve_intent(message: object, catalog: list[dict[str, object]]) -> tuple[str, bool] | None:
    """Resolve only one explicitly requested target using the same write validation."""
    if isinstance(message, str):
        # An explicit @ address is not part of the requested operation.
        message = re.sub(r"^(?:@[^\s@]+\s+)+", "", message.strip())
    try:
        text = _validate_scope_text(message, catalog)
    except PluginCenterError:
        return None
    matches = []
    for plugin in catalog:
        for enabled in (False, True):
            try:
                _validate_intent(text, plugin, enabled)
            except PluginCenterError:
                continue
            remaining = text
            for name in sorted(_names(plugin), key=len, reverse=True):
                for left, right in (("“", "”"), ("‘", "’"), ('"', '"'), ("'", "'"), ("`", "`")):
                    remaining = remaining.replace(f"{left}{name}{right}", name)
                remaining = remaining.replace(name, "")
            action = _ENABLE if enabled else _DISABLE
            # Automatic execution accepts simple imperative requests; longer
            # discussion and multi-target sentences stay with the normal tools.
            if len(action.findall(remaining)) != 1:
                continue
            remaining = _CURRENT_GROUP.sub("", action.sub("", remaining))
            if _COMMAND_WORDS.fullmatch(remaining):
                matches.append((str(plugin["module"]), enabled))
    return matches[0] if len(matches) == 1 else None


def handle_plugin_control(service: PluginCenterService, payload: Mapping[str, Any]) -> dict[str, object]:
    """Use trusted event scope and configured administrators; never change global plugins."""
    from .group_policy import GroupPolicyError, GroupPolicyService

    sender_id = payload.get("sender_id")
    if not isinstance(sender_id, str) or sender_id not in load_robot_admin_ids(service.install_root):
        raise PluginCenterError("这个管理功能只有配置中的管理员微信号可用。")
    if payload.get("scope") != "current_group":
        raise PluginCenterError("这里只支持当前群聊的插件开关。")
    action = payload.get("action")
    if action not in {"list", "set_enabled", "resolve_message"}:
        raise PluginCenterError("不支持这项插件管理操作。")
    policies = GroupPolicyService(service.install_root)
    account, group = payload.get("account_id"), payload.get("group_id")
    try:
        overview = policies.feature_permissions(account, group)
        policy = overview["policy"]
        catalog = [
            {
                "id": item["id"], "module": item["id"], "name": item["name"],
                "enabled": item["allowed"],
                "aliases": list(_PLUGIN_ALIASES.get(item["id"], ())),
            }
            for item in overview["features"]
        ]
        scope = {"scope": "current_group", "account_id": policy["account_id"], "group_id": policy["group_id"]}
        if action == "resolve_message":
            target = _resolve_intent(payload.get("message"), catalog)
            return {**scope, "resolved": target is not None,
                    **({"module": target[0], "enabled": target[1]} if target else {})}
        text = _validate_scope_text(payload.get("message"), catalog)
        if action == "list":
            return {**scope, "plugins": catalog, "revision": policy["revision"]}
        module, enabled = payload.get("module"), payload.get("enabled")
        if not isinstance(module, str) or not isinstance(enabled, bool):
            raise PluginCenterError("插件标识或启用状态无效。")
        plugin = next((item for item in catalog if item["module"] == module), None)
        if plugin is None:
            raise PluginCenterError("该插件不在当前群聊可管理的功能列表中。")
        _validate_intent(text, plugin, enabled)
        saved = policies.set_feature_enabled(account, group, module, enabled, actor_id=sender_id)
        if saved.get("saved") is not True or (module in saved["allowed_features"]) is not enabled:
            raise PluginCenterError("本群插件状态未确认，请先查询当前群的插件状态。")
    except GroupPolicyError as exc:
        raise PluginCenterError(str(exc)) from exc
    state = "开启" if enabled else "关闭"
    return {
        **scope, "module": module, "enabled": enabled, "applied": True,
        "revision": saved["revision"],
        "message": f"已在当前群{state}“{plugin['name']}”的使用权限，软件中本群的功能勾选已同步。",
    }
