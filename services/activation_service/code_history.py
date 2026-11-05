"""Encrypted, administrator-only activation-code history at rest."""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import stat

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from wechatrebot_manager.licensing.protocol import LicenseError

KEY_FILENAME = "activation-history.key"
_AAD_PREFIX = b"wechatrebot-activation-history-v1:"


def _unavailable() -> LicenseError:
    return LicenseError("history_unavailable", "发码历史加密资料不可用，请检查并恢复配套的历史密钥及数据库。")


def load_key(path: Path, *, create: bool = False) -> bytes:
    """Creation is serialized by the caller's activation-database transaction."""
    try:
        if create:
            try:
                descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as handle:
                    handle.write(secrets.token_bytes(32))
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(path, 0o600)
                if os.name == "posix":
                    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
        before = path.lstat()
        reparse = getattr(before, "st_file_attributes", 0) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or reparse:
            raise _unavailable()
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0))
        with os.fdopen(descriptor, "rb") as handle:
            current = os.fstat(handle.fileno())
            if ((before.st_dev, before.st_ino) != (current.st_dev, current.st_ino)
                    or not stat.S_ISREG(current.st_mode) or current.st_nlink != 1):
                raise _unavailable()
            key = handle.read(33)
        if len(key) != 32:
            raise _unavailable()
        return key
    except OSError as exc:
        raise _unavailable() from exc


def encrypt_payload(key: bytes, code_id: str, *, code: str, owner_ref: str, reference: str) -> str:
    raw = json.dumps({"code": code, "owner_ref": owner_ref, "reference": reference},
                     ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = secrets.token_bytes(12)
    encrypted = AESGCM(key).encrypt(nonce, raw, _AAD_PREFIX + code_id.encode("utf-8"))
    return "v1:" + base64.b64encode(nonce + encrypted).decode("ascii")


def decrypt_payload(key: bytes, code_id: str, payload: str) -> dict[str, str]:
    try:
        if not payload.startswith("v1:") or len(payload) > 4096:
            raise ValueError("Invalid history format")
        raw = base64.b64decode(payload[3:], validate=True)
        decoded = AESGCM(key).decrypt(raw[:12], raw[12:], _AAD_PREFIX + code_id.encode("utf-8"))
        value = json.loads(decoded)
        if (not isinstance(value, dict) or set(value) != {"code", "owner_ref", "reference"}
                or any(not isinstance(item, str) or not item for item in value.values())):
            raise ValueError("Invalid history contents")
        return value
    except (InvalidTag, ValueError, TypeError, UnicodeError) as exc:
        raise _unavailable() from exc
