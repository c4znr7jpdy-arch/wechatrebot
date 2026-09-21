"""Pure runtime group-feature policy, copied into the pinned engine patch."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


DEFAULT_GROUP_FEATURES = (
    "astrbot_plugin_angel_heart",
    "astrbot_plugin_bittorrent",
    "astrbot_plugin_music",
    "astrbot_plugin_parser",
    "astrbot_plugin_twitter",
    "astrbot_plugin_qq_group_daily_analysis",
    "jiang_commands",
    "jiang_group_notice",
    "jiang_image",
    "jiang_mystool",
    "jiang_rocom",
)
_INFRASTRUCTURE = {"astrbot_plugin_patch_at_wxid", "jiang_capability_router", "astrbot_plugin_angel_heart"}


def group_plugins(
    policy_path: Path,
    account_id: str,
    group_id: str,
    globally_enabled: list[str] | None,
    stars: Iterable[Any],
) -> list[str] | None:
    """Return engine names intersected with group grants; None denies the event.

    Missing group entries use the product defaults. Corrupt policy and missing
    account identity fail closed. Reserved handlers are filtered by the engine.
    """
    if not account_id or not group_id.endswith("@chatroom"):
        return None
    raw: dict[str, Any] = {}
    try:
        payload = json.loads(policy_path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        payload = {"schema_version": 1, "accounts": {}}
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("accounts"), dict):
        return None
    account = payload["accounts"].get(account_id, {})
    if not isinstance(account, dict) or not isinstance(account.get("groups", {}), dict):
        return None
    raw = account.get("groups", {}).get(group_id, {})
    if not isinstance(raw, dict) or raw.get("bot_enabled", True) is not True:
        return None
    if raw.get("reply_mode", "mention") not in {"mention", "natural"}:
        return None
    allowed = raw.get("allowed_features", list(DEFAULT_GROUP_FEATURES))
    if not isinstance(allowed, list) or any(not isinstance(item, str) for item in allowed):
        return None
    permitted = set(allowed) | _INFRASTRUCTURE
    enabled = None if globally_enabled is None or globally_enabled == ["*"] else set(globally_enabled)
    result: set[str] = set()
    for star in stars:
        root = str(getattr(star, "root_dir_name", "") or "")
        name = str(getattr(star, "name", "") or "")
        if root not in permitted:
            continue
        if enabled is not None and root not in enabled and name not in enabled:
            continue
        result.update(value for value in (root, name) if value)
    return sorted(result)
