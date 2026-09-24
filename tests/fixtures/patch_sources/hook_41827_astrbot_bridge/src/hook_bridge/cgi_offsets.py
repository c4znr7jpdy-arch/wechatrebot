from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any


VIDEO_CGI_KEYWORDS = ("uploadvideo", "sendsight", "video")


def _items(raw: Mapping[str, Any], key: str) -> Iterable[Mapping[str, Any]]:
    value = raw.get(key)
    if not isinstance(value, list):
        return ()
    return (item for item in value if isinstance(item, Mapping))


def _cgi_name(item: Mapping[str, Any]) -> str:
    value = item.get("cgi")
    return value if isinstance(value, str) else ""


def _offset_hex(item: Mapping[str, Any]) -> str:
    value = item.get("offset_hex")
    if isinstance(value, str):
        return value[:32]
    value = item.get("offset")
    if isinstance(value, int):
        return hex(value)
    return ""


def cgi_offset_summary(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Return a safe, non-secret summary of HOOK CGI offset diagnostics."""

    request_count = raw.get("request_cgi_count")
    response_count = raw.get("response_cgi_count")
    if not isinstance(request_count, int):
        request_count = len(list(_items(raw, "request_cgi_list")))
    if not isinstance(response_count, int):
        response_count = len(list(_items(raw, "response_cgi_list")))

    hits: list[str] = []
    for direction, key in (
        ("request", "request_cgi_list"),
        ("response", "response_cgi_list"),
    ):
        for item in _items(raw, key):
            cgi = _cgi_name(item)
            if not cgi:
                continue
            cgi_lower = cgi.casefold()
            if not any(keyword in cgi_lower for keyword in VIDEO_CGI_KEYWORDS):
                continue
            offset = _offset_hex(item)
            hits.append(f"{direction}:{cgi}@{offset}" if offset else f"{direction}:{cgi}")

    return {
        "cgi_request_count": request_count,
        "cgi_response_count": response_count,
        "cgi_video_hits": tuple(sorted(set(hits))[:20]),
    }


def cgi_offset_entries(raw: Mapping[str, Any]) -> tuple[tuple[str, str, str], ...]:
    """Return sanitized CGI entries as (direction, cgi, offset_hex)."""

    entries: list[tuple[str, str, str]] = []
    for direction, key in (
        ("request", "request_cgi_list"),
        ("response", "response_cgi_list"),
    ):
        for item in _items(raw, key):
            cgi = _cgi_name(item)
            if cgi:
                entries.append((direction, cgi, _offset_hex(item)))
    return tuple(sorted(set(entries)))
