"""The model catalog and LivingMemory share one embedding-model selection."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Iterable

from .json_io import write_json_atomic
from .plugin_errors import ConfigConflictError

EMBEDDING_MODULE = "astrbot_plugin_livingmemory"


def embedding_config_path(root: str | Path) -> Path:
    return Path(root) / "app/astrbot/data/config" / f"{EMBEDDING_MODULE}_config.json"


def read_embedding_config(root: str | Path) -> dict[str, Any]:
    path = embedding_config_path(root)
    try:
        config = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise ValueError("长期记忆配置无法读取，请修复配置后重试。") from exc
    if not isinstance(config, dict):
        raise ValueError("长期记忆配置格式不正确。")
    return config


def selected_embedding_id(config: dict[str, Any], services: Iterable[Any]) -> str:
    """Match the runtime's original provider order before the UI sorts cards."""
    available = [item.provider_id for item in services
                 if item.service_kind == "embedding" and item.configured]
    settings = config.get("provider_settings")
    selected = str(settings.get("embedding_provider_id") or "").strip() if isinstance(settings, dict) else ""
    return selected if selected in available else next(iter(available), "")


def with_embedding_default(config: dict[str, Any], provider_id: str) -> dict[str, Any]:
    updated = copy.deepcopy(config)
    settings = updated.setdefault("provider_settings", {})
    if not isinstance(settings, dict):
        raise ValueError("长期记忆模型设置格式不正确。")
    settings["embedding_provider_id"] = provider_id
    return updated


def save_embedding_default(root: str | Path, provider_id: str, client, online: bool) -> str:
    if online:
        snapshot = client.get_config(EMBEDDING_MODULE)
        updated = with_embedding_default(snapshot["config"], provider_id)
        receipt = client.save_config(EMBEDDING_MODULE, updated, snapshot["revision"])
        state = receipt.get("state")
        if state not in {"applied", "pending_enable", "pending_setup"}:
            raise ConfigConflictError("默认向量模型已提交，但未能确认生效状态，请重新读取设置。")
        return state
    original = read_embedding_config(root)
    updated = with_embedding_default(original, provider_id)
    if read_embedding_config(root) != original:
        raise ConfigConflictError("长期记忆设置已变化，请重新读取后再选择默认向量模型。")
    write_json_atomic(embedding_config_path(root), updated)
    return "saved_for_start"
