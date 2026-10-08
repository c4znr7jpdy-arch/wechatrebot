"""Account-scoped group behavior and scheduled-delivery policy storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import threading
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .feature_policy import DeliveryFeature, load_delivery_features
from .json_io import write_json_atomic
from .schedules import ScheduleService


_ACCOUNT_ID = re.compile(r"^[A-Za-z0-9_.@:-]{1,128}$")
_GROUP_ID = re.compile(r"^[A-Za-z0-9_.-]+@chatroom$")
_MODULE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
_CLOCK = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_REPLY_MODES = frozenset({"mention", "natural", "off"})
DEFAULT_GROUP_FEATURES = (
    "astrbot_plugin_angel_heart",
    "astrbot_plugin_bittorrent",
    "astrbot_plugin_office_assistant",
    "astrbot_plugin_music",
    "astrbot_plugin_parser",
    "astrbot_plugin_twitter",
    "astrbot_plugin_qq_group_daily_analysis",
    "jiang_commands",
    "jiang_commands.setu",
    "jiang_group_notice",
    "jiang_image",
    "jiang_mystool",
    "jiang_rocom",
)
_POLICY_FIELDS = frozenset(
    {
        "bot_enabled",
        "reply_mode",
        "allowed_features",
        "scheduled_enabled",
        "quiet_hours",
        "daily_limit",
    }
)
_STORE_LOCK = threading.RLock()


class GroupPolicyError(ValueError):
    """A safe, user-facing group policy validation or persistence error."""


@dataclass(frozen=True, slots=True)
class QuietHours:
    enabled: bool = False
    start: str = "22:00"
    end: str = "08:00"
    timezone: str = "Asia/Shanghai"

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "start": self.start,
            "end": self.end,
            "timezone": self.timezone,
        }


@dataclass(frozen=True, slots=True)
class GroupPolicy:
    bot_enabled: bool = True
    reply_mode: str = "mention"
    allowed_features: tuple[str, ...] = DEFAULT_GROUP_FEATURES
    # This permits only explicitly saved, enabled tasks whose run is verified
    # at the bridge; it never seeds a task or enables AI proactive chat.
    scheduled_enabled: bool = True
    quiet_hours: QuietHours = QuietHours()
    daily_limit: int = 3
    updated_at: str = ""
    updated_by: str = ""

    def to_dict(self) -> dict[str, object]:
        return {
            "bot_enabled": self.bot_enabled,
            "reply_mode": self.reply_mode,
            "effective_reply_mode": self.reply_mode if self.bot_enabled else "off",
            "allowed_features": list(self.allowed_features),
            "scheduled_enabled": self.scheduled_enabled,
            "quiet_hours": self.quiet_hours.to_dict(),
            "daily_limit": self.daily_limit,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
        }


@dataclass(frozen=True, slots=True)
class _ProactiveSpec:
    module: str
    display_name: str
    target_paths: tuple[tuple[str, ...], ...]
    enable_paths: tuple[tuple[str, ...], ...]


_PROACTIVE_SPECS = (
    _ProactiveSpec(
        "astrbot_plugin_proactive_chat",
        "主动聊天",
        (("group_settings", "session_list"),),
        (("group_settings", "enable"),),
    ),
    _ProactiveSpec(
        "astrbot_plugin_qq_group_daily_analysis",
        "群聊分析",
        (("auto_analysis", "scheduled_group_list"),),
        (
            ("auto_analysis", "enabled"),
            ("auto_analysis", "enable"),
            ("auto_analysis", "scheduled_enabled"),
        ),
    ),
)


def _nested(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = payload
    for name in path:
        if not isinstance(value, dict):
            return None
        value = value.get(name)
    return value


def _contains_group(value: Any, group_id: str) -> bool:
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        return False
    for item in values:
        if not isinstance(item, str):
            continue
        match = re.search(r"([A-Za-z0-9_.-]+@chatroom)", item)
        if match and match.group(1) == group_id:
            return True
    return False


class GroupPolicyService:
    """Manage local group policy without performing remote group actions."""

    def __init__(
        self,
        install_root: str | Path,
        *,
        schedules: ScheduleService | None = None,
    ) -> None:
        self.install_root = Path(install_root).expanduser().resolve()
        self.path = self.install_root / "data" / "manager" / "group-policies.json"
        self.schedules = schedules or ScheduleService(self.install_root)

    @staticmethod
    def _validate_account(account_id: str) -> str:
        account = str(account_id or "").strip()
        if not _ACCOUNT_ID.fullmatch(account):
            raise GroupPolicyError("机器人账号标识无效，请重新连接微信后刷新群列表。")
        return account

    @staticmethod
    def _validate_scope(account_id: str, group_id: str) -> tuple[str, str]:
        account = GroupPolicyService._validate_account(account_id)
        group = str(group_id or "").strip()
        if not _GROUP_ID.fullmatch(group):
            raise GroupPolicyError("群标识无效，请从当前账号的群列表重新选择。")
        return account, group

    def _read(self) -> dict[str, Any]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except FileNotFoundError:
            return {"schema_version": 1, "accounts": {}, "audit": []}
        except (OSError, json.JSONDecodeError) as exc:
            raise GroupPolicyError("群聊策略无法读取，原配置未被修改。") from exc
        if (
            not isinstance(payload, dict)
            or payload.get("schema_version") != 1
            or not isinstance(payload.get("accounts"), dict)
        ):
            raise GroupPolicyError("群聊策略格式不正确，原配置未被修改。")
        if not isinstance(payload.get("audit", []), list):
            raise GroupPolicyError("群聊策略审计记录格式不正确，原配置未被修改。")
        payload.setdefault("audit", [])
        return payload

    @staticmethod
    def _raw_policy(payload: dict[str, Any], account_id: str, group_id: str) -> dict[str, Any]:
        account = payload.get("accounts", {}).get(account_id)
        groups = account.get("groups") if isinstance(account, dict) else None
        raw = groups.get(group_id) if isinstance(groups, dict) else None
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _policy(raw: dict[str, Any]) -> GroupPolicy:
        quiet_raw = raw.get("quiet_hours")
        quiet = quiet_raw if isinstance(quiet_raw, dict) else {}
        allowed = raw.get("allowed_features")
        if "allowed_features" not in raw:
            allowed = list(DEFAULT_GROUP_FEATURES)
        features = tuple(
            item for item in (allowed if isinstance(allowed, list) else [])
            if isinstance(item, str) and _MODULE_ID.fullmatch(item)
        )
        return GroupPolicy(
            bot_enabled=raw.get("bot_enabled") if isinstance(raw.get("bot_enabled"), bool) else True,
            reply_mode=raw.get("reply_mode") if raw.get("reply_mode") in _REPLY_MODES else "mention",
            allowed_features=tuple(dict.fromkeys(features)),
            scheduled_enabled=(
                raw.get("scheduled_enabled")
                if isinstance(raw.get("scheduled_enabled"), bool)
                else True
            ),
            quiet_hours=QuietHours(
                enabled=quiet.get("enabled") if isinstance(quiet.get("enabled"), bool) else False,
                start=quiet.get("start") if isinstance(quiet.get("start"), str) else "22:00",
                end=quiet.get("end") if isinstance(quiet.get("end"), str) else "08:00",
                timezone=(
                    quiet.get("timezone")
                    if isinstance(quiet.get("timezone"), str)
                    else "Asia/Shanghai"
                ),
            ),
            daily_limit=(
                raw.get("daily_limit")
                if isinstance(raw.get("daily_limit"), int)
                and not isinstance(raw.get("daily_limit"), bool)
                else 3
            ),
            updated_at=str(raw.get("updated_at") or ""),
            updated_by=str(raw.get("updated_by") or ""),
        )

    @staticmethod
    def _revision(policy: GroupPolicy) -> str:
        canonical = json.dumps(policy.to_dict(), ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]

    def _public(
        self,
        account_id: str,
        group_id: str,
        policy: GroupPolicy,
        *,
        configured: bool,
    ) -> dict[str, object]:
        return {
            "account_id": account_id,
            "group_id": group_id,
            "configured": configured,
            **policy.to_dict(),
            "revision": self._revision(policy),
            "permissions": self.permissions(account_id, group_id),
        }

    def get(self, account_id: str, group_id: str) -> dict[str, object]:
        account, group = self._validate_scope(account_id, group_id)
        with _STORE_LOCK:
            payload = self._read()
            raw = self._raw_policy(payload, account, group)
        return self._public(account, group, self._policy(raw), configured=bool(raw))

    def list_for_account(self, account_id: str) -> dict[str, object]:
        account = self._validate_account(account_id)
        with _STORE_LOCK:
            payload = self._read()
            raw_account = payload.get("accounts", {}).get(account)
            groups = raw_account.get("groups") if isinstance(raw_account, dict) else {}
            items = [
                self._public(account, group_id, self._policy(raw), configured=True)
                for group_id, raw in (groups.items() if isinstance(groups, dict) else ())
                if _GROUP_ID.fullmatch(str(group_id)) and isinstance(raw, dict)
            ]
        return {"account_id": account, "items": items, "total": len(items)}

    def _features(self) -> tuple[DeliveryFeature, ...]:
        features = load_delivery_features(self.install_root)
        features = features or ()
        if any(item.module == "jiang_commands" and item.visible for item in features) and not any(item.module == "jiang_commands.setu" for item in features):
            features += (DeliveryFeature("jiang_commands.setu", "图来", "none"),)
        return features

    def _normalize(
        self,
        values: dict[str, Any],
        current: GroupPolicy,
        *,
        actor_id: str,
    ) -> GroupPolicy:
        if not isinstance(values, dict):
            raise GroupPolicyError("群聊策略格式不正确。")
        unknown = set(values).difference(_POLICY_FIELDS)
        if unknown:
            raise GroupPolicyError("群聊策略包含当前版本不支持的设置。")

        bot_enabled = values.get("bot_enabled", current.bot_enabled)
        scheduled_enabled = values.get("scheduled_enabled", current.scheduled_enabled)
        if any(not isinstance(value, bool) for value in (
            bot_enabled, scheduled_enabled
        )):
            raise GroupPolicyError("群聊开关格式不正确。")
        reply_mode = values.get("reply_mode", current.reply_mode)
        if reply_mode not in _REPLY_MODES:
            raise GroupPolicyError("回复方式不正确，请重新选择。")

        allowed = values.get("allowed_features", list(current.allowed_features))
        if not isinstance(allowed, list) or any(
            not isinstance(item, str) or not _MODULE_ID.fullmatch(item) for item in allowed
        ):
            raise GroupPolicyError("允许功能清单格式不正确。")
        allowed = list(dict.fromkeys(allowed))
        if "allowed_features" in values:
            available = {feature.module for feature in self._features()}
            unavailable = sorted(set(allowed).difference(available))
            if unavailable:
                raise GroupPolicyError("允许功能清单包含未交付或已移除的功能。")
        # The conversation core is no longer an optional per-chat feature.
        # Keep the persisted grant compatible with older installed runtimes.
        if any(feature.module == "astrbot_plugin_angel_heart" for feature in self._features()):
            if "astrbot_plugin_angel_heart" not in allowed:
                allowed.insert(0, "astrbot_plugin_angel_heart")

        daily_limit = values.get("daily_limit", current.daily_limit)
        if (
            isinstance(daily_limit, bool)
            or not isinstance(daily_limit, int)
            or not 0 <= daily_limit <= 100
        ):
            raise GroupPolicyError("每日定时推送上限应为 0 到 100。")

        quiet_value = values.get("quiet_hours", current.quiet_hours.to_dict())
        if not isinstance(quiet_value, dict):
            raise GroupPolicyError("安静时段格式不正确。")
        quiet_unknown = set(quiet_value).difference({"enabled", "start", "end", "timezone"})
        if quiet_unknown:
            raise GroupPolicyError("安静时段包含当前版本不支持的设置。")
        quiet_base = current.quiet_hours.to_dict()
        quiet_base.update(quiet_value)
        if not isinstance(quiet_base["enabled"], bool):
            raise GroupPolicyError("安静时段开关格式不正确。")
        if not _CLOCK.fullmatch(str(quiet_base["start"])) or not _CLOCK.fullmatch(
            str(quiet_base["end"])
        ):
            raise GroupPolicyError("安静时段应使用 HH:MM 格式。")
        zone_name = str(quiet_base["timezone"])
        try:
            ZoneInfo(zone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise GroupPolicyError("安静时段的时区不可用。") from None

        actor = str(actor_id or "local-console").strip()
        if len(actor) > 128 or any(ord(char) < 32 for char in actor):
            raise GroupPolicyError("操作人标识格式不正确。")
        return GroupPolicy(
            bot_enabled=bot_enabled,
            reply_mode=reply_mode,
            allowed_features=tuple(allowed),
            scheduled_enabled=scheduled_enabled,
            quiet_hours=QuietHours(
                enabled=quiet_base["enabled"],
                start=str(quiet_base["start"]),
                end=str(quiet_base["end"]),
                timezone=zone_name,
            ),
            daily_limit=daily_limit,
            updated_at=datetime.now(timezone.utc).isoformat(),
            updated_by=actor or "local-console",
        )

    def save(
        self,
        account_id: str,
        group_id: str,
        values: dict[str, Any],
        *,
        expected_revision: str | None = None,
        actor_id: str = "local-console",
    ) -> dict[str, object]:
        account, group = self._validate_scope(account_id, group_id)
        with _STORE_LOCK:
            payload = self._read()
            raw = self._raw_policy(payload, account, group)
            current = self._policy(raw)
            if expected_revision and expected_revision != self._revision(current):
                raise GroupPolicyError("群聊策略已在其他窗口修改，请刷新后重试。")
            normalized = self._normalize(values, current, actor_id=actor_id)
            accounts = payload.setdefault("accounts", {})
            account_payload = accounts.setdefault(account, {"groups": {}})
            if not isinstance(account_payload, dict):
                raise GroupPolicyError("当前账号的群聊策略格式不正确，原配置未被修改。")
            groups = account_payload.setdefault("groups", {})
            if not isinstance(groups, dict):
                raise GroupPolicyError("当前账号的群聊策略格式不正确，原配置未被修改。")
            stored = normalized.to_dict()
            stored.pop("effective_reply_mode", None)
            groups[group] = stored
            changed = sorted(
                key
                for key in _POLICY_FIELDS
                if current.to_dict().get(key) != normalized.to_dict().get(key)
            )
            audit = payload.setdefault("audit", [])
            audit.append(
                {
                    "time": normalized.updated_at,
                    "account_id": account,
                    "group_id": group,
                    "actor_id": normalized.updated_by,
                    "action": "save_group_policy",
                    "changed_fields": changed,
                }
            )
            payload["audit"] = audit[-500:]
            write_json_atomic(self.path, payload)
        # Read the persisted value back before reporting success.  The manager
        # uses this receipt as the source of truth for the saved/effective
        # distinction, so a successful file write alone is not enough.
        verified = self.get(account, group)
        return {
            **verified,
            "saved": True,
            "apply_state": "applied",
            "message": "群聊策略已安全保存；未向群内发送消息。",
        }

    def preview(
        self,
        account_id: str,
        group_id: str,
        values: dict[str, Any],
        *,
        expected_revision: str | None = None,
    ) -> dict[str, object]:
        """Validate a draft and describe its customer-visible impact.

        Preview is deliberately read-only.  It shares the same normalization
        path as ``save`` so the confirmation shown by the manager cannot drift
        from the values that will eventually be persisted.
        """
        account, group = self._validate_scope(account_id, group_id)
        with _STORE_LOCK:
            payload = self._read()
            raw = self._raw_policy(payload, account, group)
            current = self._policy(raw)
            if expected_revision and expected_revision != self._revision(current):
                raise GroupPolicyError("群聊策略已在其他窗口修改，请刷新后重试。")
            proposed = self._normalize(values, current, actor_id="preview")
        changed = sorted(
            key for key in _POLICY_FIELDS
            if current.to_dict().get(key) != proposed.to_dict().get(key)
        )
        impact: list[str] = []
        if "bot_enabled" in changed:
            impact.append("关闭后本群不再回复消息；开启后恢复当前回复方式。" if not proposed.bot_enabled else "本群重新允许机器人回复。")
        if "reply_mode" in changed:
            impact.append({"mention": "仅在被点名或引用时回复。", "natural": "按群聊上下文自然接话。", "off": "不在本群回复。"}[proposed.reply_mode])
        if "allowed_features" in changed:
            impact.append("允许功能清单会改变本群可调用的功能。")
        if "scheduled_enabled" in changed or "quiet_hours" in changed or "daily_limit" in changed:
            impact.append("定时任务会按新的主动消息开关、安静时段和每日上限重新检查。")
        if not impact:
            impact.append("没有检测到行为变化，保存后仍会回读确认当前规则。")
        return {
            "account_id": account,
            "group_id": group,
            "current": self._public(account, group, current, configured=bool(raw)),
            "proposed": self._public(account, group, proposed, configured=True),
            "changed_fields": changed,
            "impact": impact,
            "will_send_messages": False,
            "requires_group_admin": False,
            "message": "保存只更新本机群聊规则，不会向群内发送消息。",
        }

    @staticmethod
    def permissions(account_id: str, group_id: str) -> dict[str, object]:
        GroupPolicyService._validate_scope(account_id, group_id)
        return {
            "group_role": "unknown",
            "can_manage_members": False,
            "can_edit_group_announcement": False,
            "can_perform_group_admin_actions": False,
            "can_edit_local_policy": True,
            "message": "微信接口尚未提供可靠的群主或管理员身份，因此不会推断权限。",
        }

    def _enabled_plugins(self) -> set[str] | None:
        path = self.install_root / "app" / "astrbot" / "data" / "cmd_config.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return set()
        plugin_set = payload.get("plugin_set") if isinstance(payload, dict) else None
        if not isinstance(plugin_set, list):
            return set()
        names = {str(item) for item in plugin_set}
        return None if "*" in names else names

    def _plugin_sources(self, group_id: str) -> list[dict[str, object]]:
        config_root = self.install_root / "app" / "astrbot" / "data" / "config"
        enabled_plugins = self._enabled_plugins()
        result: list[dict[str, object]] = []
        for spec in _PROACTIVE_SPECS:
            path = config_root / f"{spec.module}_config.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (FileNotFoundError, OSError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict) or not any(
                _contains_group(_nested(payload, item), group_id) for item in spec.target_paths
            ):
                continue
            flags = [_nested(payload, item) for item in spec.enable_paths]
            configured_enabled = any(value is True for value in flags)
            globally_enabled = enabled_plugins is None or spec.module in enabled_plugins
            enabled = configured_enabled and globally_enabled
            result.append(
                {
                    "id": f"feature:{spec.module}",
                    "name": spec.display_name,
                    "source_kind": "feature",
                    "enabled": enabled,
                    "status": (
                        "已启用"
                        if enabled
                        else "功能未启用"
                        if not globally_enabled
                        else "已暂停"
                    ),
                    "next_run_time": "",
                    "schedule_text": "由功能设置决定",
                }
            )
        return result

    def proactive_summary(self, account_id: str, group_id: str) -> dict[str, object]:
        policy = self.get(account_id, group_id)
        schedules = self.schedules.list()
        schedule_items = [
            {
                "id": str(item.get("id") or ""),
                "name": str(item.get("name") or "定时任务"),
                "source_kind": "schedule",
                "feature_kind": str(item.get("source_kind") or "custom"),
                "enabled": bool(item.get("enabled")),
                "status": str(item.get("status") or ""),
                "next_run_time": str(item.get("next_run_time") or ""),
                "schedule_text": str(item.get("schedule_text") or ""),
            }
            for item in schedules.get("items", [])
            if isinstance(item, dict)
            and item.get("target_type") == "group"
            and item.get("target_id") == group_id
        ]
        items = schedule_items + self._plugin_sources(group_id)
        deduplicated = {str(item["id"]): item for item in items}
        items = list(deduplicated.values())
        enabled_count = sum(1 for item in items if item["enabled"])
        return {
            "account_id": policy["account_id"],
            "group_id": policy["group_id"],
            "items": items,
            "total": len(items),
            "running": enabled_count,
            "paused": len(items) - enabled_count,
            "message": "主动聊天由各功能自行配置；定时任务按本群设置执行。",
        }

    def overview(self, account_id: str, group_id: str) -> dict[str, object]:
        policy = self.get(account_id, group_id)
        allowed = set(policy["allowed_features"])
        features = [
            {
                "id": item.module,
                "name": item.display_name,
                "allowed": item.module in allowed,
                "globally_enabled_by_default": True,
            }
            for item in self._features()
            if item.visible and item.module != "astrbot_plugin_angel_heart"
        ]
        return {
            "policy": policy,
            "features": features,
            "proactive": self.proactive_summary(account_id, group_id),
            "permissions": policy["permissions"],
        }


__all__ = [
    "DEFAULT_GROUP_FEATURES",
    "GroupPolicy",
    "GroupPolicyError",
    "GroupPolicyService",
    "QuietHours",
]
