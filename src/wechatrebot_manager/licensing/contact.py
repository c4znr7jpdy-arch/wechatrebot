"""Shared public WeChat group QR data, separate from subscription entitlement."""
from __future__ import annotations

import base64
from pathlib import Path

from .protocol import LicenseError, integer, parse_json

CONTACT_ENDPOINT = "/v1/contact"
MAX_QR_BYTES = 512 * 1024
MAX_CONTACT_RESPONSE_BYTES = 768 * 1024
_FIELDS = {"schema_version", "group_name", "expires_at", "mime_type", "image_base64"}


def create_contact(image: bytes, group_name: str, expires_at: int) -> dict:
    mime = "image/png" if image.startswith(b"\x89PNG\r\n\x1a\n") else (
        "image/jpeg" if image.startswith(b"\xff\xd8\xff") else "")
    return parse_contact({
        "schema_version": 1, "group_name": group_name, "expires_at": expires_at,
        "mime_type": mime, "image_base64": base64.b64encode(image).decode("ascii"),
    })


def parse_contact(value: object) -> dict:
    try:
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise ValueError()
        if type(value["schema_version"]) is not int or value["schema_version"] != 1:
            raise ValueError()
        name = value["group_name"]
        if (not isinstance(name, str) or not name.strip() or len(name) > 80
                or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)):
            raise ValueError()
        expires = integer(value["expires_at"])
        if not 0 < expires <= 253402214400:
            raise ValueError()
        encoded = value["image_base64"]
        if not isinstance(encoded, str) or len(encoded) > ((MAX_QR_BYTES + 2) // 3) * 4:
            raise ValueError()
        image = base64.b64decode(encoded, validate=True)
        if not image or len(image) > MAX_QR_BYTES:
            raise ValueError()
        mime = value["mime_type"]
        if not ((mime == "image/png" and image.startswith(b"\x89PNG\r\n\x1a\n"))
                or (mime == "image/jpeg" and image.startswith(b"\xff\xd8\xff"))):
            raise ValueError()
        return {**value, "group_name": name.strip()}
    except (KeyError, TypeError, ValueError) as exc:
        raise LicenseError("contact_invalid", "微信群二维码资料无效，请稍后刷新或联系维护者。") from exc


def load_contact(path: Path) -> dict | None:
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_CONTACT_RESPONSE_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(raw) > MAX_CONTACT_RESPONSE_BYTES:
        raise LicenseError("contact_invalid", "微信群二维码资料过大，请联系维护者。")
    try:
        return parse_contact(parse_json(raw))
    except LicenseError as exc:
        raise LicenseError("contact_invalid", "微信群二维码资料无效，请稍后刷新或联系维护者。") from exc


def contact_snapshot(record: dict | None, now: float, source: str) -> dict:
    result = {"state": "unavailable", "group_name": "", "expires_at": 0, "remaining_seconds": 0,
              "image_data_url": "", "message": "微信群二维码尚未配置，请稍后刷新。", "source": source}
    if record is None:
        return result
    record = parse_contact(record)
    result.update(group_name=record["group_name"], expires_at=record["expires_at"],
                  remaining_seconds=max(0, record["expires_at"] - now))
    if now >= record["expires_at"]:
        result.update(state="expired", message="微信群二维码已到期，请刷新获取新二维码。")
    else:
        result.update(state="ready", message="使用微信扫码入群，联系管理员领取激活码。",
                      image_data_url=f'data:{record["mime_type"]};base64,{record["image_base64"]}')
    return result
