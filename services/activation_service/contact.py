"""Operator-only replacement of the fixed public WeChat contact record."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import re
from zoneinfo import ZoneInfo

from wechatrebot_manager.json_io import write_json_atomic
from wechatrebot_manager.licensing.contact import MAX_QR_BYTES, create_contact
from wechatrebot_manager.licensing.protocol import LicenseError

CONTACT_FILENAME = "wechat-contact.json"


def parse_expiry(value: str, now: int) -> int:
    """A date expires at its Beijing midnight; datetimes need an explicit offset."""
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            parsed = datetime.fromisoformat(value).replace(tzinfo=ZoneInfo("Asia/Shanghai"))
        else:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("A datetime requires an explicit timezone")
        expires_at = int(parsed.timestamp())
    except (ValueError, OverflowError, OSError) as exc:
        raise LicenseError("contact_invalid", "有效期须为带时区的 ISO 8601 时间，或 YYYY-MM-DD（北京时间当日零点截止）。") from exc
    if expires_at <= now:
        raise LicenseError("contact_expired", "二维码有效期必须晚于当前服务器时间。")
    return expires_at


def set_contact(data_dir: Path, image_path: Path, group_name: str, expiry: str, now: int) -> dict:
    expires_at = parse_expiry(expiry, now)
    with image_path.open("rb") as handle:
        # Bound this read before validation so a mistaken multi-gigabyte input
        # cannot consume unbounded memory.
        image = handle.read(MAX_QR_BYTES + 1)
    record = create_contact(image, group_name, expires_at)
    data_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_json_atomic(data_dir / CONTACT_FILENAME, record)
    return {"contact_configured": True, "group_name": record["group_name"],
            "expires_at": record["expires_at"], "mime_type": record["mime_type"]}


def clear_contact(data_dir: Path) -> dict:
    (data_dir / CONTACT_FILENAME).unlink(missing_ok=True)
    return {"contact_configured": False}
