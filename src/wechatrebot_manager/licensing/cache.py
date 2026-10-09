"""Protected, atomic lease cache and bounded local run permission."""

from __future__ import annotations

import base64
import math
from pathlib import Path
import re

from ..json_io import write_json_atomic
from .device import Protection, protected_bytes
from .protocol import CODE_MODES, PLANS, LicenseError, canonical, parse_json


def empty_state(device_id: str) -> dict:
    return {
        "schema_version": 1, "device_id": device_id, "lease": None,
        "revision": 0, "issued_at": 0, "trusted_time": 0,
        "wall_time": 0, "monotonic_time": 0, "boot_id": "", "requires_online": False,
        "run_grant": None, "pause_reason": "", "refresh_after": 0,
        "refresh_failures": 0, "last_error": "", "remote_denial": "",
        "pending_preview": None,
    }


def _number(value: object) -> bool:
    return type(value) in {int, float} and math.isfinite(value) and value >= 0


def _validate_preview(preview: object) -> None:
    if preview is None:
        return
    if not isinstance(preview, dict) or set(preview) != {
        "preview_id", "code_hash", "mode", "license_id", "replaces_license_id",
        "current_license_id", "plan", "valid_until", "expires_at",
    }:
        raise ValueError()
    if not isinstance(preview["preview_id"], str) or not 1 <= len(preview["preview_id"]) <= 256:
        raise ValueError()
    if not isinstance(preview["code_hash"], str) or not re.fullmatch(r"[a-f0-9]{64}", preview["code_hash"]):
        raise ValueError()
    if preview["mode"] not in CODE_MODES or preview["plan"] not in PLANS:
        raise ValueError()
    if not all(isinstance(preview[k], str) and len(preview[k]) <= 128
               for k in ("license_id", "replaces_license_id", "current_license_id")):
        raise ValueError()
    if not all(type(preview[k]) is int and 0 <= preview[k] <= 253402214400
               for k in ("valid_until", "expires_at")):
        raise ValueError()
    if preview["replaces_license_id"] and (
        preview["mode"] != "migration" or not preview["license_id"]
        or preview["license_id"] == preview["replaces_license_id"]
        or preview["replaces_license_id"] != preview["current_license_id"]
    ):
        raise ValueError()


def _validate(value: dict, device_id: str) -> dict:
    if (set(value) != set(empty_state(device_id)) or type(value["schema_version"]) is not int
            or value["schema_version"] != 1):
        raise ValueError()
    if value["device_id"] != device_id or type(value["requires_online"]) is not bool:
        raise ValueError()
    if value["lease"] is not None and not isinstance(value["lease"], dict):
        raise ValueError()
    if not isinstance(value["boot_id"], str) or (
        value["boot_id"] and not re.fullmatch(r"[a-f0-9]{32}", value["boot_id"])
    ):
        raise ValueError()
    if value["lease"] is not None and not value["boot_id"]:
        raise ValueError()
    for field in ("revision", "issued_at", "refresh_failures"):
        if type(value[field]) is not int or value[field] < 0:
            raise ValueError()
    for field in ("trusted_time", "wall_time", "monotonic_time", "refresh_after"):
        if not _number(value[field]):
            raise ValueError()
    for field in ("pause_reason", "last_error", "remote_denial"):
        if not isinstance(value[field], str) or len(value[field]) > 100:
            raise ValueError()
    _validate_preview(value["pending_preview"])
    grant = value["run_grant"]
    if grant is not None:
        if not isinstance(grant, dict) or set(grant) != {
            "license_id", "device_id", "account", "valid_until", "revision", "granted_at", "run_id",
        }:
            raise ValueError()
        if not all(isinstance(grant[k], str) and grant[k] for k in ("license_id", "device_id", "account")):
            raise ValueError()
        if grant["device_id"] != device_id:
            raise ValueError()
        if not isinstance(grant["run_id"], str) or not re.fullmatch(r"[a-f0-9]{32}", grant["run_id"]):
            raise ValueError()
        if not all(type(grant[k]) is int and grant[k] >= 0 for k in ("valid_until", "revision", "granted_at")):
            raise ValueError()
    return value


class LeaseCache:
    def __init__(self, directory: Path, protection: Protection):
        self.path = directory / "state.json"
        self.protection = protection

    def read(self, device_id: str) -> dict:
        try:
            if not self.path.exists():
                return empty_state(device_id)
            if self.path.stat().st_size > 2_000_000:
                raise ValueError()
            envelope = parse_json(self.path.read_bytes())
            if (set(envelope) != {"schema_version", "protected"}
                    or type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1):
                raise ValueError()
            raw = self.protection.unprotect(protected_bytes(envelope["protected"]))
            return _validate(parse_json(raw), device_id)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LicenseError("cache_error", "本地授权记录无法验证，请联系管理员恢复授权。") from exc

    def write(self, state: dict) -> None:
        try:
            _validate(state, state["device_id"])
            ciphertext = self.protection.protect(canonical(state))
            write_json_atomic(self.path, {
                "schema_version": 1, "protected": base64.b64encode(ciphertext).decode("ascii"),
            })
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise LicenseError("storage_error", "无法保存授权状态，请检查磁盘和目录权限。") from exc
