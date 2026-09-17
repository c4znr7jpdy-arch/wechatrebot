"""Group-scoped gate for hidden meme emotion instructions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def group_allows_meme_prompt(
    policy_path: str | Path,
    event: Any,
    *,
    plugin_name: str = "meme_manager",
) -> bool:
    """Allow private/non-WeChat chats, and require an explicit WeChat group grant."""
    if event is None:
        return True
    try:
        group_id = str(event.get_group_id() or "")
        if not group_id.endswith("@chatroom"):
            return True
        if event.get_platform_name() != "aiocqhttp":
            return True
        account_id = str(event.get_self_id() or "")
    except Exception:
        return False
    if not account_id:
        return False
    try:
        payload = json.loads(Path(policy_path).read_text(encoding="utf-8-sig"))
        account = payload.get("accounts", {}).get(account_id, {})
        groups = account.get("groups", {}) if isinstance(account, dict) else {}
        policy = groups.get(group_id, {}) if isinstance(groups, dict) else {}
    except (OSError, ValueError, AttributeError):
        return False
    if not isinstance(policy, dict):
        return False
    if policy.get("bot_enabled", True) is not True:
        return False
    if policy.get("reply_mode", "mention") not in {"mention", "natural"}:
        return False
    allowed = policy.get("allowed_features", [])
    return isinstance(allowed, list) and plugin_name in allowed


__all__ = ["group_allows_meme_prompt"]
