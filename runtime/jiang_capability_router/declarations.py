"""Read the same reviewed plugin metadata for AI discovery and command help."""
from __future__ import annotations

import json
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)


def load_declarations(star) -> list[dict]:
    """Read optional metadata beside the already loaded plugin, never import it."""
    module_file = getattr(getattr(star, "module", None), "__file__", None)
    if not module_file:
        return []
    path = Path(module_file).parent / "capabilities.json"
    if not path.is_file():
        return []
    try:
        if path.stat().st_size > 65536:
            raise ValueError("capabilities.json exceeds 64 KiB")
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        if data.get("schema_version") != 1 or not isinstance(data.get("capabilities"), list):
            raise ValueError("unsupported capability declaration")
        result = []
        seen = set()
        for item in data["capabilities"]:
            if not isinstance(item, dict):
                raise ValueError("capability must be an object")
            key = item.get("id")
            if not isinstance(key, str) or not key.isidentifier() or key in seen:
                raise ValueError("capability id must be unique and an identifier")
            seen.add(key)
            description = item.get("description")
            examples = item.get("examples", [])
            if not isinstance(description, str) or not description.strip():
                raise ValueError("description is required")
            if not isinstance(examples, list) or any(not isinstance(x, str) for x in examples):
                raise ValueError("examples must be strings")
            handler = item.get("handler", "")
            if not isinstance(handler, str) or (handler and not handler.isidentifier()):
                raise ValueError("handler must be a registered method name")
            pattern = item.get("message_pattern", "")
            if not isinstance(pattern, str) or len(pattern) > 1000:
                raise ValueError("invalid message pattern")
            if handler and not pattern:
                raise ValueError("executable declarations require a message pattern")
            if pattern:
                re.compile(pattern)
            # Declarations can only opt into read operations explicitly; unknown is write.
            effect = item.get("effect", "write")
            if effect not in {"read", "write"}:
                raise ValueError("invalid effect")
            permission = item.get("permission", "everyone")
            if permission not in {"everyone", "member", "admin", "group_admin"}:
                raise ValueError("invalid permission")
            result.append({"id": f"decl:{star.name}:{key}", "effect": effect,
                           "description": description, "examples": examples,
                           "handler": handler, "message_pattern": pattern,
                           "permission": permission})
        return result
    except (OSError, ValueError, TypeError, AttributeError, re.error):
        logger.exception("Invalid capability declaration for %s", star.name)
        return []


def declaration_visible(item: dict, event) -> bool:
    """Apply declared minimum visibility without changing native handler checks."""
    permission = item.get("permission", "everyone")
    if permission == "admin":
        return event.is_admin()
    if permission == "group_admin" and event.get_group_id():
        return event.is_admin()
    return True
