"""Product-level configuration entry for the robot's conversation core."""

from __future__ import annotations

import ast
import copy
from typing import Any

from .brand import BrandProfile
from .customer_config import RobotSettings, load_robot_settings, save_robot_settings
from .contact_directory import ContactDirectoryService
from .plugin_center import (
    ConfigConflictError,
    PluginCenterError,
    PluginCenterService,
    ROBOT_CORE_FEATURE_ID,
    _validate_config,
)
from .runtime_config import normalize_admin_id


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
        settings = load_robot_settings(self.features.install_root)
        result["identity"] = {
            "robot_name": settings.profile.display_name,
            "admin_id": settings.admin_id,
        }
        robot = ContactDirectoryService(self.features.install_root).self_profile(wait=True)
        result["identity"]["wechat_nickname"] = str(robot.get("nickname") or robot.get("name") or "")
        if settings.profile.name_source == "wechat":
            result["identity"]["robot_name"] = ""
        self._normalize_identity(result, settings.profile.display_name)
        result["template_schema"] = self._template_schema(result["advanced_schema"])
        return result

    @staticmethod
    def _default_identity(robot_name: str) -> str:
        return (
            f"你是{robot_name}，是群聊里的熟人型机器人。"
            "说话自然、直接、简短，不使用机械的客服措辞。"
        )

    def _normalize_identity(self, detail: dict[str, Any], robot_name: str) -> None:
        default_identity = self._default_identity(robot_name)
        legacy_identity = (
            "你拥有强大的检索能力和庞大的知识储备和记忆能力。\n"
            "你喜爱跟群友进行语言游戏沟通。\n"
            "群友对AI的善意测试你必须回应满足群友的期待，展示你的能力。"
        )
        for schema_name in ("basic_schema", "model_schema", "advanced_schema"):
            schema = detail.get(schema_name)
            if not isinstance(schema, dict):
                continue
            self._rename_core_schema_copy(schema)
            personality = schema.get("personality")
            wake = schema.get("wake_interaction")
            if isinstance(personality, dict):
                field = personality.get("items", {}).get("ai_self_identity")
                if isinstance(field, dict):
                    field["default"] = default_identity
            if isinstance(wake, dict):
                field = wake.get("items", {}).get("alias")
                if isinstance(field, dict):
                    field["default"] = robot_name
        config = detail.get("advanced_config")
        if not isinstance(config, dict):
            return
        personality = config.get("personality")
        if isinstance(personality, dict):
            current = str(personality.get("ai_self_identity") or "").strip()
            if not current or current == legacy_identity:
                personality["ai_self_identity"] = default_identity
            else:
                personality["ai_self_identity"] = (
                    current.replace("\u59dc\u5c0f\u59b9\u673a\u5668\u4eba", robot_name)
                    .replace("\u59dc\u5c0f\u59b9", robot_name)
                    .replace("群聊里的熟人型助手", "群聊里的熟人型机器人")
                    .replace("群聊助手", "群聊机器人")
                )
        wake = config.get("wake_interaction")
        if isinstance(wake, dict):
            aliases = str(wake.get("alias") or "").strip()
            if not aliases or aliases in {"AI|助手", "AI|助理", "助手", "助理"}:
                wake["alias"] = robot_name
            else:
                wake["alias"] = aliases.replace("\u59dc\u5c0f\u59b9", robot_name)

    @classmethod
    def _rename_core_schema_copy(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key, child in value.items():
                value[key] = cls._rename_core_schema_copy(child)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                value[index] = cls._rename_core_schema_copy(child)
        elif isinstance(value, str):
            return value.replace("助理", "机器人").replace("助手", "机器人")
        return value

    def save_global(
        self,
        config: dict[str, Any],
        revision: str,
        identity: dict[str, Any] | None,
    ) -> dict[str, Any]:
        current = load_robot_settings(self.features.install_root)
        values = identity if isinstance(identity, dict) else {}
        robot_name = str(values.get("robot_name") or "").strip()
        name_source = "custom" if robot_name else "wechat"
        if not robot_name:
            robot = ContactDirectoryService(self.features.install_root).self_profile(wait=True)
            robot_name = str(robot.get("nickname") or robot.get("name") or "").strip()
            if not robot_name:
                raise PluginCenterError("尚未读取到机器人微信昵称，请连接微信或填写自定义名称。")
        admin_id = str(values.get("admin_id") or "").strip()
        normalized_admin_id = normalize_admin_id(admin_id) or ""
        profile = BrandProfile(
            display_name=robot_name,
            short_name=robot_name,
            persona_name=robot_name,
            name_source=name_source,
            avatar_path=current.profile.avatar_path,
            welcome_text=(
                current.profile.welcome_text.replace(current.profile.display_name, robot_name)
            ),
            help_title=(
                current.profile.help_title.replace(current.profile.display_name, robot_name)
            ),
        ).normalized()
        errors = profile.validate()
        if errors:
            raise PluginCenterError("；".join(errors))

        normalized = copy.deepcopy(config)
        wake = normalized.setdefault("wake_interaction", {})
        if not isinstance(wake, dict):
            raise PluginCenterError("点名与唤醒配置格式不正确。")
        aliases = [part.strip() for part in str(wake.get("alias") or "").split("|") if part.strip()]
        former_names = {
            current.profile.display_name.casefold(),
            current.profile.short_name.casefold(),
            current.profile.persona_name.casefold(),
            "\u59dc\u5c0f\u59b9".casefold(),
            "\u59dc\u5c0f\u59b9\u673a\u5668\u4eba".casefold(),
            "ai",
            "助手".casefold(),
            "助理".casefold(),
        }
        aliases = [
            item for item in aliases
            if item.casefold() not in former_names and item.casefold() != robot_name.casefold()
        ]
        wake["alias"] = "|".join([robot_name, *aliases])

        personality = normalized.setdefault("personality", {})
        if not isinstance(personality, dict):
            raise PluginCenterError("群聊身份配置格式不正确。")
        prompt = str(personality.get("ai_self_identity") or "").strip()
        if not prompt:
            prompt = self._default_identity(robot_name)
        for former in sorted(
            {
                current.profile.display_name,
                current.profile.short_name,
                current.profile.persona_name,
                "\u59dc\u5c0f\u59b9\u673a\u5668\u4eba",
                "\u59dc\u5c0f\u59b9",
            },
            key=len,
            reverse=True,
        ):
            if former and former != robot_name:
                prompt = prompt.replace(former, robot_name)
        personality["ai_self_identity"] = (
            prompt.replace("群聊里的熟人型助手", "群聊里的熟人型机器人")
            .replace("群聊助手", "群聊机器人")
        )

        saved_core = self.features.save(
            ROBOT_CORE_FEATURE_ID, normalized, revision or None
        )
        saved_settings = save_robot_settings(
            self.features.install_root,
            RobotSettings(profile=profile, admin_id=normalized_admin_id),
            apply_reply_identity=True,
        )
        saved_core["brand"] = {
            **saved_settings.profile.to_dict(),
            "admin_id": saved_settings.admin_id,
        }
        saved_core["message"] = (
            "机器人名称与群聊身份已更新；管理员权限及底层初始人格重启机器人后生效。"
            if saved_core.get("apply_state") == "applied"
            else "机器人名称、管理员、群聊身份和初始人格已保存；重启机器人后生效。"
        )
        return saved_core

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
