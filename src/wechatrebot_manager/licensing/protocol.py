"""Strict signed requests and short-lived authorization leases."""

from __future__ import annotations

import base64
import calendar
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Any
from zoneinfo import ZoneInfo

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

LEASE_DOMAIN = b"wechatrebot.license.v1\n"
REQUEST_DOMAIN = b"wechatrebot.license.request.v1\n"
CODE_MODES = frozenset({"new", "renewal", "recovery", "migration"})
PLANS = {
    "trial": {"name": "一小时试用", "price_fen": 0, "months": 0, "seconds": 3600},
    "month": {"name": "月付", "price_fen": 6900, "months": 1, "seconds": 0},
    "year": {"name": "年付", "price_fen": 69900, "months": 12, "seconds": 0},
}
MAX_LEASE_SECONDS = 86400
ACCOUNT_PATTERN = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")


class LicenseError(ValueError):
    def __init__(self, code: str, message: str):
        self.code = code
        super().__init__(message)


def _pairs(items: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in items:
        if key in result:
            raise LicenseError("invalid_data", "授权数据包含重复字段。")
        result[key] = value
    return result


def parse_json(raw: bytes | str) -> dict:
    try:
        value = json.loads(raw, object_pairs_hook=_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    except (ValueError, UnicodeError) as exc:
        raise LicenseError("invalid_data", "授权数据格式无效。") from exc
    if not isinstance(value, dict):
        raise LicenseError("invalid_data", "授权数据必须是对象。")
    return value


def canonical(value: dict) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"), allow_nan=False).encode("utf-8")


def encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def decode(value: str, size: int) -> bytes:
    try:
        raw = base64.b64decode(value, validate=True)
        if len(raw) != size:
            raise ValueError()
        return raw
    except (ValueError, TypeError) as exc:
        raise LicenseError("invalid_signature", "授权签名或设备身份无效。") from exc


def public_key(key: Ed25519PrivateKey) -> str:
    return encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))


def fingerprint(value: str) -> str:
    return hashlib.sha256(decode(value, 32)).hexdigest()


def integer(value: Any) -> int:
    if type(value) is not int or value < 0:
        raise LicenseError("invalid_data", "授权时间或版本无效。")
    return value


def issue_lease(key: Ed25519PrivateKey, key_id: str, payload: dict) -> dict:
    return {"algorithm": "ed25519", "key_id": key_id, "payload": payload,
            "signature": encode(key.sign(LEASE_DOMAIN + canonical(payload)))}


def verify_lease(envelope: dict, keys: dict[str, str], device: str) -> dict:
    try:
        if set(envelope) != {"algorithm", "key_id", "payload", "signature"}:
            raise ValueError()
        if envelope["algorithm"] != "ed25519" or envelope["key_id"] not in keys:
            raise ValueError()
        payload = envelope["payload"]
        if not isinstance(payload, dict):
            raise ValueError()
        Ed25519PublicKey.from_public_bytes(decode(keys[envelope["key_id"]], 32)).verify(
            decode(envelope["signature"], 64), LEASE_DOMAIN + canonical(payload))
        required = {"schema_version", "audience", "license_id", "device", "account",
                    "plan", "status", "issued_at", "lease_until", "valid_until",
                    "revision", "request_nonce"}
        if set(payload) != required or type(payload["schema_version"]) is not int:
            raise ValueError()
        if payload["schema_version"] != 1 or payload["audience"] != "wechatrebot":
            raise ValueError()
        if payload["device"] != device or payload["plan"] not in PLANS:
            raise ValueError()
        if payload["status"] not in {"active", "revoked"}:
            raise ValueError()
        if not isinstance(payload["license_id"], str) or not payload["license_id"]:
            raise ValueError()
        if not isinstance(payload["account"], str) or (
            payload["account"] and not ACCOUNT_PATTERN.fullmatch(payload["account"])
        ):
            raise ValueError()
        if not isinstance(payload["request_nonce"], str) or not re.fullmatch(
            r"[a-f0-9]{32}", payload["request_nonce"]
        ):
            raise ValueError()
        issued, until, valid = (integer(payload[k]) for k in ("issued_at", "lease_until", "valid_until"))
        integer(payload["revision"])
        if until > valid or until > issued + MAX_LEASE_SECONDS or max(issued, valid) > 253402214400:
            raise ValueError()
        return payload
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise LicenseError("invalid_lease", "授权凭证校验失败，请重新验证。") from exc


def sign_request(key: Ed25519PrivateKey, action: str, body: dict, now: int, nonce: str) -> dict:
    payload = {"action": action, "body": body, "timestamp": now, "nonce": nonce,
               "public_key": public_key(key)}
    return {**payload, "signature": encode(key.sign(REQUEST_DOMAIN + canonical(payload)))}


def verify_request(value: dict, action: str, now: int) -> tuple[str, dict]:
    try:
        if set(value) != {"action", "body", "timestamp", "nonce", "public_key", "signature"}:
            raise ValueError()
        payload = {k: v for k, v in value.items() if k != "signature"}
        if value["action"] != action or abs(integer(value["timestamp"]) - now) > 300:
            raise ValueError()
        if not re.fullmatch(r"[a-f0-9]{32}", value["nonce"]) or not isinstance(value["body"], dict):
            raise ValueError()
        device = fingerprint(value["public_key"])
        Ed25519PublicKey.from_public_bytes(decode(value["public_key"], 32)).verify(
            decode(value["signature"], 64), REQUEST_DOMAIN + canonical(payload))
        return device, value["body"]
    except (InvalidSignature, ValueError, KeyError, TypeError) as exc:
        raise LicenseError("invalid_request", "设备请求校验失败，请校准时间后重试。") from exc


def extend_period(start: int, plan: str, anchor_day: int | None = None) -> int:
    spec = PLANS[plan]
    if spec["seconds"]:
        return start + spec["seconds"]
    current = datetime.fromtimestamp(start, ZoneInfo("Asia/Shanghai"))
    total = current.year * 12 + current.month - 1 + spec["months"]
    year, month = divmod(total, 12)
    month += 1
    day = min(anchor_day or current.day, calendar.monthrange(year, month)[1])
    return int(current.replace(year=year, month=month, day=day).timestamp())


def display_time(value: int) -> str:
    return datetime.fromtimestamp(value, timezone.utc).astimezone(
        ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S")
