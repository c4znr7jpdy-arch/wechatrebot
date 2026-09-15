"""Product-level configuration entry for the robot's conversation core."""

from __future__ import annotations

import ast
import copy
from typing import Any

from .plugin_center import (
    ConfigConflictError,
    PluginCenterError,
    PluginCenterService,
    ROBOT_CORE_FEATURE_ID,
    _validate_config,
)


_READ_ENDPOINTS = frozenset({"profiles", "chats", "chat_status"})
_WRITE_ENDPOINTS = frozenset({
    "profiles/create", "profiles/update", "profiles/delete", "bindings/set",
})


class RobotCoreSettingsService:
    def __init__(self, features: PluginCenterService) -> None:
        self.features = features

    def detail(self) -> dict[str, Any]:
        result = self.features.detail(ROBOT_CORE_FEATURE_ID)
        result["display_name"] = "机器人核心"
        result["template_schema"] = self._template_schema(result["advanced_schema"])
        return result

    def _template_schema(self, schema: dict[str, Any]) -> dict[str, Any]:
        # Read the installed store's actual whitelist, rather than silently
        # accepting controls that the runtime discards on template save.
        path = (
            self.features.install_root / "app" / "astrbot" / "data" / "plugins"
            / "astrbot_plugin_angel_heart" / "core" / "chat_profile.py"
        )
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
            groups = next(
                ast.literal_eval(node.value)
                for node in tree.body
                if (
                    isinstance(node, ast.AnnAssign)
                    and isinstance(node.target, ast.Name)
                    and node.target.id == "TEMPLATE_GROUPS"
                ) or (
                    isinstance(node, ast.Assign)
                    and any(isinstance(t, ast.Name) and t.id == "TEMPLATE_GROUPS" for t in node.targets)
                )
            )
        except (OSError, SyntaxError, ValueError, StopIteration):
            return {}
        if not isinstance(groups, dict):
            return {}
        result = {}
        for key, fields in groups.items():
            section = schema.get(key)
            if not isinstance(fields, dict) or not isinstance(section, dict):
                continue
            items = section.get("items", {})
            selected = {k: copy.deepcopy(items[k]) for k in fields if k in items}
            if selected:
                result[key] = {**copy.deepcopy(section), "items": selected}
        return result

    def request(
        self, method: str, endpoint: str, payload: dict[str, Any] | None = None,
    ) -> Any:
        if not (
            (method == "GET" and endpoint in _READ_ENDPOINTS)
            or (method == "POST" and endpoint in _WRITE_ENDPOINTS)
        ):
            raise PluginCenterError("核心配置请求无效。")
        body = copy.deepcopy(payload) if payload is not None else None
        if method == "POST" and not isinstance(body, dict):
            raise PluginCenterError("核心配置内容必须为对象。")
        if endpoint in {"profiles/create", "profiles/update"} and body is not None:
            if "config" in body:
                schema = self.detail()["template_schema"]
                if not schema:
                    raise PluginCenterError("当前版本暂不支持模板编辑，请更新完整程序。")
                config = body["config"]
                if not isinstance(config, dict):
                    raise PluginCenterError("模板配置内容必须为对象。")
                for group, values in config.items():
                    if group not in schema or not isinstance(values, dict):
                        raise PluginCenterError("模板包含不支持的配置分组。")
                    if set(values) - set(schema[group]["items"]):
                        raise PluginCenterError("模板包含不支持的配置项。")
                _validate_config(schema, config)
            if endpoint == "profiles/update" and "updated_at" in body:
                expected = body.pop("updated_at")
                snapshot = self.request("GET", "profiles")
                current = next((t for t in snapshot.get("templates", []) if t.get("id") == body.get("id")), None)
                if current is None or current.get("updated_at") != expected:
                    raise ConfigConflictError("模板已被其他窗口修改，请重新读取后保存。")
        path = self.features.extension_path(ROBOT_CORE_FEATURE_ID, endpoint)
        return self.features.engine.request(method, path, body)
