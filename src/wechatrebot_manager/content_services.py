"""Provision the publisher's content services while preserving customer settings."""
from __future__ import annotations

import json
from pathlib import Path

from .config_lock import CONFIG_FILE_LOCK
from .json_io import write_json_atomic
from .port_config import _load_object


CONTENT_SERVICE_FIELDS = ("content_api_id", "content_api_key", "weather_detail_token")
CONTENT_SERVICE_TEMPLATE = Path("config/templates/content-services.json")
CONTENT_SERVICE_CONFIG = Path("app/astrbot/data/config/jiang_commands_config.json")


def read_content_service_defaults(path: Path) -> dict[str, str]:
    """Read the three explicitly distributed service credentials without logging them."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("热点与天气默认服务配置无法读取。") from None
    if (not isinstance(payload, dict) or set(payload) != set(CONTENT_SERVICE_FIELDS)
            or any(not isinstance(payload[key], str) or not payload[key].strip()
                   for key in CONTENT_SERVICE_FIELDS)):
        raise ValueError("热点与天气默认服务配置须包含服务账号、密钥和天气详情令牌。")
    return {key: payload[key].strip() for key in CONTENT_SERVICE_FIELDS}


def apply_content_service_defaults(install_root: Path) -> None:
    """Fill missing service credentials on new installs and on upgrades."""
    template = install_root / CONTENT_SERVICE_TEMPLATE
    if not template.is_file():
        return
    defaults = read_content_service_defaults(template)
    with CONFIG_FILE_LOCK:
        path = install_root / CONTENT_SERVICE_CONFIG
        config = _load_object(path)
        updates = {}
        account_fields = CONTENT_SERVICE_FIELDS[:2]
        # Account and key belong together. A partial custom account must not
        # receive the publisher's unrelated credential.
        if all(not str(config.get(key) or "").strip()
               or config[key] == defaults[key] for key in account_fields):
            updates.update({key: defaults[key] for key in account_fields
                            if not str(config.get(key) or "").strip()})
        if not str(config.get("weather_detail_token") or "").strip():
            updates["weather_detail_token"] = defaults["weather_detail_token"]
        if updates:
            write_json_atomic(path, {**config, **updates})
