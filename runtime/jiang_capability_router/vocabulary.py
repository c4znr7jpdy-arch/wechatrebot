"""Supplement live entries with reviewed phrases; never register new capabilities."""
from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)
_KINDS = {"command", "tool", "declaration", "natural", "plugin"}


def validate(payload: dict) -> dict[tuple[str, str, str], tuple[str, ...]]:
    if (not isinstance(payload, dict) or type(payload.get("schema_version")) is not int
            or payload["schema_version"] != 1 or not isinstance(payload.get("entries"), list)):
        raise ValueError("Invalid vocabulary schema")
    result = {}
    for entry in payload["entries"]:
        if not isinstance(entry, dict):
            raise ValueError("Vocabulary entry must be an object")
        kind, owner = entry.get("kind"), entry.get("plugin", "")
        names, phrases = entry.get("names"), entry.get("phrases")
        if (not isinstance(kind, str) or kind not in _KINDS or not isinstance(owner, str)
                or owner != owner.strip() or (kind == "natural" and owner)
                or (kind != "natural" and not owner)):
            raise ValueError("Invalid vocabulary entry owner or kind")
        for values in (names, phrases):
            if not isinstance(values, list) or not values or any(
                not isinstance(value, str) or not value.strip() or value != value.strip()
                or len(value) > 100 or any(ord(char) < 32 for char in value) for value in values
            ):
                raise ValueError("Vocabulary names and phrases must be nonempty strings")
            if len({value.casefold() for value in values}) != len(values):
                raise ValueError("Duplicate vocabulary values")
        if kind == "plugin" and any(name.casefold() != owner.casefold() for name in names):
            raise ValueError("Plugin vocabulary must target its own owner")
        for name in names:
            key = (kind, owner.casefold(), name.casefold())
            if key in result:
                raise ValueError("Duplicate vocabulary target")
            result[key] = tuple(phrases)
    return result


@lru_cache(maxsize=1)
def load() -> dict[tuple[str, str, str], tuple[str, ...]]:
    try:
        path = Path(__file__).with_name("vocabulary.json")
        if path.stat().st_size > 512 * 1024:
            raise ValueError("Vocabulary exceeds 512 KiB")
        return validate(json.loads(path.read_text(encoding="utf-8-sig")))
    except (OSError, ValueError, TypeError, AttributeError):
        logger.exception("Capability vocabulary unavailable; using live metadata search")
        return {}


def phrases_for(item) -> tuple[str, ...]:
    if not isinstance(item, dict):
        key = ("natural", "", str(getattr(item, "capability_id", "")))
    else:
        owner = str(item.get("plugin") or "")
        if item.get("effective_command"):
            name = item.get("handler_name") or str(item.get("handler_full_name", "")).rsplit(".", 1)[-1]
            key = ("command", owner, name)
        elif str(item.get("id", "")).startswith("decl:"):
            key = ("declaration", owner, item["id"].rsplit(":", 1)[-1])
        elif item.get("capability_id"):
            key = ("natural", "", item["capability_id"])
        elif owner:
            key = ("tool", owner, item.get("name", ""))
        else:
            key = ("plugin", item.get("name", ""), item.get("name", ""))
    return load().get(tuple(str(value).casefold() for value in key), ())
