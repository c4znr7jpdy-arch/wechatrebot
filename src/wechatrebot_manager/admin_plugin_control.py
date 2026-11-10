"""Administrator-only natural-language access to the existing plugin transaction."""
from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any, TYPE_CHECKING

from .customer_config import load_robot_settings
from .plugin_errors import PluginCenterError

if TYPE_CHECKING:
    from .plugin_center import PluginCenterService


CONTROL_PATH = "/internal/plugin-control"
CONTROL_DESCRIPTOR = "data/manager/plugin-control.json"
_ENABLE = re.compile(r"开启|启用|打开|启动|恢复|开起来|打开来")
_DISABLE = re.compile(r"关闭|禁用|停用|关掉|停掉|关了|关起来")
_NON_ACTION = re.compile(
    r"不要|别|不用|无需|不许|不想|不需要|不再|不.*(?:开|关|启|停)|"
    r"怎么|如何|是否|有没有|已经|了吗|了没|能否|能不能|可不可以|可以.*吗|如果|假如"
)


def _validate_intent(message: object, plugin: Mapping[str, Any], enabled: bool) -> None:
    if not isinstance(message, str) or not message.strip() or len(message) > 4000:
        raise PluginCenterError("请在当前消息中明确说明要开启或关闭哪个插件。")
    text = re.sub(r"\s+", "", message).casefold()
    intended, opposite = (_ENABLE, _DISABLE) if enabled else (_DISABLE, _ENABLE)
    if _NON_ACTION.search(text) or not intended.search(text) or opposite.search(text):
        raise PluginCenterError("当前消息未明确要求这次操作，请直接说“全局开启点歌”或“全局关闭点歌”。")
    names = [str(plugin.get(key) or "") for key in ("name", "module", "id")]
    names.extend(plugin.get("aliases") or [])
    names = [re.sub(r"\s+", "", str(name)).casefold() for name in names]
    if not any(name and name in text for name in names):
        raise PluginCenterError("当前消息中的插件与操作目标不一致，请使用插件列表里的名称重试。")


def handle_plugin_control(service: PluginCenterService, payload: Mapping[str, Any]) -> dict[str, object]:
    """Authorize against the saved administrator, then delegate to one transaction."""
    admin_id = load_robot_settings(service.install_root).admin_id.strip()
    sender_id = payload.get("sender_id")
    if not admin_id or not isinstance(sender_id, str) or sender_id.strip() != admin_id:
        raise PluginCenterError("这个管理功能只有配置中的管理员微信号可用。")
    action = payload.get("action")
    if action not in {"list", "set_enabled"}:
        raise PluginCenterError("不支持这项插件管理操作。")
    catalog = service.global_control_catalog()
    if action == "list":
        return {"plugins": catalog}
    module = payload.get("module")
    enabled = payload.get("enabled")
    if not isinstance(module, str) or not isinstance(enabled, bool):
        raise PluginCenterError("插件标识或启用状态无效。")
    plugin = next((item for item in catalog if item["module"] == module), None)
    if plugin is None:
        raise PluginCenterError("该插件不在可全局管理的正式插件列表中。")
    _validate_intent(payload.get("message"), plugin, enabled)
    result = service.set_enabled(str(plugin["id"]), enabled, require_online=True)
    confirmed = next((item for item in service.global_control_catalog() if item["module"] == module), None)
    if confirmed is None or confirmed.get("enabled") is not enabled:
        raise PluginCenterError("插件状态未确认，请先查询当前全局状态。")
    if result.get("applied") is not True:
        return {
            "module": module, "enabled": enabled, "applied": False,
            "message": "全局开关配置已保存，但尚未确认运行中的插件生效，请恢复软件管理接口后重试。",
        }
    state = "开启" if enabled else "关闭"
    linked = "“图来”和“常用指令”属于同一个插件，已一并同步。" if plugin.get("aliases") else ""
    return {
        "module": module,
        "enabled": enabled,
        "applied": True,
        "message": f"已全局{state}“{plugin['name']}”，所有群聊和私聊均已同步，软件中的功能状态也会同步更新。{linked}",
    }
