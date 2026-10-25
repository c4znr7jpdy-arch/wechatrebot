"""Per-install device identities protected by the current Windows user."""

from __future__ import annotations

import base64
import ctypes
from dataclasses import dataclass
import os
import hashlib
from pathlib import Path
from typing import Protocol

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat

from ..json_io import write_json_atomic
from .protocol import LicenseError, fingerprint, parse_json, public_key


class Protection(Protocol):
    def protect(self, value: bytes) -> bytes: ...
    def unprotect(self, value: bytes) -> bytes: ...


class _DataBlob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_ulong), ("data", ctypes.POINTER(ctypes.c_ubyte))]


class _BootEnvironment(ctypes.Structure):
    _fields_ = [
        ("identifier", ctypes.c_ubyte * 16),
        ("firmware_type", ctypes.c_uint32),
        ("flags", ctypes.c_uint64),
    ]


def boot_session_id() -> str:
    """Read the kernel boot GUID; wall-clock-derived guesses are not acceptable."""
    if os.name != "nt":
        raise LicenseError("boot_identity", "无法确认 Windows 启动会话，自动化已暂停。")
    try:
        ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        query = ntdll.NtQuerySystemInformation
        query.argtypes = (ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32))
        query.restype = ctypes.c_long
        information = _BootEnvironment()
        returned = ctypes.c_uint32()
        # SystemBootEnvironmentInformation exposes BootIdentifier without needing
        # firmware writes, subprocesses, administrator rights or hardware serials.
        status = query(90, ctypes.byref(information), ctypes.sizeof(information), ctypes.byref(returned))
        if status < 0 or returned.value < 16:
            raise OSError("Windows boot identity unavailable")
        identifier = bytes(information.identifier)
        if not any(identifier):
            raise OSError("Windows boot identity is empty")
        return identifier.hex()
    except OSError as exc:
        raise LicenseError(
            "boot_identity", "无法确认 Windows 启动会话，自动化已暂停；请修复系统环境后联网验证。"
        ) from exc


class WindowsProtection:
    """DPAPI without CRYPTPROTECT_LOCAL_MACHINE or a plaintext fallback."""

    def protect(self, value: bytes) -> bytes:
        return self._convert(value, decrypt=False)

    def unprotect(self, value: bytes) -> bytes:
        return self._convert(value, decrypt=True)

    @staticmethod
    def _convert(value: bytes, *, decrypt: bool) -> bytes:
        if os.name != "nt":
            raise LicenseError("device_protection", "设备授权需要 Windows 用户凭据保护。")
        source_buffer = ctypes.create_string_buffer(value)
        source = _DataBlob(len(value), ctypes.cast(source_buffer, ctypes.POINTER(ctypes.c_ubyte)))
        result = _DataBlob()
        try:
            crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            convert = crypt32.CryptUnprotectData if decrypt else crypt32.CryptProtectData
            convert.argtypes = (
                ctypes.POINTER(_DataBlob), ctypes.c_void_p, ctypes.c_void_p,
                ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(_DataBlob),
            )
            convert.restype = ctypes.c_int
            kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
            kernel32.LocalFree.restype = ctypes.c_void_p
            # CRYPTPROTECT_UI_FORBIDDEN: background runtime checks must never open a prompt.
            if not convert(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
                raise OSError("DPAPI failed")
            try:
                if not result.data or not result.size:
                    raise OSError("DPAPI returned no data")
                return ctypes.string_at(result.data, result.size)
            finally:
                kernel32.LocalFree(result.data)
        except OSError as exc:
            raise LicenseError(
                "device_protection", "无法读取当前 Windows 用户的设备凭据，请联系管理员迁移授权。"
            ) from exc


@dataclass(frozen=True)
class DeviceIdentity:
    key: Ed25519PrivateKey

    @property
    def public_key(self) -> str:
        return public_key(self.key)

    @property
    def device_id(self) -> str:
        return fingerprint(self.public_key)


def _read_smbios() -> bytes:
    """Read firmware with the Windows API, without processes or cached files."""
    if os.name != "nt":
        raise OSError("Windows firmware identity is unavailable")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    read = kernel32.GetSystemFirmwareTable
    read.argtypes = (ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p, ctypes.c_uint32)
    read.restype = ctypes.c_uint32
    provider = int.from_bytes(b"RSMB", "big")
    size = read(provider, 0, None, 0)
    if not 8 <= size <= 16 * 1024 * 1024:
        raise OSError("Invalid firmware table size")
    buffer = ctypes.create_string_buffer(size)
    if read(provider, 0, buffer, size) != size:
        raise OSError("Firmware table changed or could not be read")
    return buffer.raw


def _smbios_system_uuid(raw: bytes) -> bytes:
    """Extract Type 1 UUID (DSP0134); never use a generic model or MAC address."""
    if len(raw) < 8 or int.from_bytes(raw[4:8], "little") != len(raw) - 8:
        raise ValueError("Invalid SMBIOS header")
    if (raw[1], raw[2]) < (2, 6):
        raise ValueError("Unsupported SMBIOS UUID encoding")
    table = raw[8:]
    offset = 0
    identifier = None
    while offset < len(table):
        if offset + 4 > len(table):
            raise ValueError("Truncated SMBIOS structure")
        kind, length = table[offset:offset + 2]
        if length < 4 or offset + length > len(table):
            raise ValueError("Invalid SMBIOS structure length")
        end = table.find(b"\x00\x00", offset + length)
        if end < 0:
            raise ValueError("Unterminated SMBIOS string section")
        if kind == 1:
            if length < 25 or identifier is not None:
                raise ValueError("Missing or ambiguous system UUID")
            identifier = table[offset + 8:offset + 24]
            if identifier in (bytes(16), b"\xff" * 16):
                raise ValueError("System UUID is not assigned")
        offset = end + 2
        if kind == 127:
            break
    if identifier is None:
        raise ValueError("System UUID is missing")
    return identifier


def stable_machine_id(_root: str | Path | None = None) -> str:
    """Hash firmware identity independently of install path, user and local keys.

    This is a continuity signal, not hardware attestation. Unsupported/missing
    firmware IDs fail closed; no random or installation-specific fallback is
    allowed to restore trial eligibility. Raw firmware identifiers are never
    persisted or transmitted. The per-install signing key remains separate.
    """
    try:
        identifier = _smbios_system_uuid(_read_smbios())
        return hashlib.sha256(b"wechatrebot:trial-machine:smbios-v1:" + identifier).hexdigest()
    except (OSError, ValueError, AttributeError) as exc:
        raise LicenseError(
            "device_identity_unavailable", "无法确认当前设备身份，请检查系统环境后重试或联系群主。"
        ) from exc


def protected_bytes(value: object) -> bytes:
    try:
        if not isinstance(value, str) or len(value) > 2_000_000:
            raise ValueError()
        return base64.b64decode(value, validate=True)
    except (ValueError, TypeError) as exc:
        raise LicenseError("device_protection", "本地授权凭据损坏，请联系管理员迁移授权。") from exc


def load_device(directory: Path, protection: Protection, *, create: bool = False) -> DeviceIdentity | None:
    path = directory / "device.json"
    if not path.exists():
        if (directory / "state.json").exists():
            raise LicenseError("device_missing", "设备凭据已丢失，请联系管理员迁移授权。")
        if not create:
            return None
        identity = DeviceIdentity(Ed25519PrivateKey.generate())
        raw = identity.key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())
        try:
            ciphertext = protection.protect(raw)
            write_json_atomic(path, {
                "schema_version": 1, "public_key": identity.public_key,
                "protected_key": base64.b64encode(ciphertext).decode("ascii"),
            })
        except OSError as exc:
            raise LicenseError("storage_error", "无法保存设备凭据，请检查磁盘和目录权限。") from exc
        return identity
    try:
        if path.stat().st_size > 32_768:
            raise ValueError()
        value = parse_json(path.read_bytes())
        if (set(value) != {"schema_version", "public_key", "protected_key"}
                or type(value["schema_version"]) is not int or value["schema_version"] != 1):
            raise ValueError()
        identity = DeviceIdentity(Ed25519PrivateKey.from_private_bytes(
            protection.unprotect(protected_bytes(value["protected_key"]))
        ))
        if identity.public_key != value["public_key"]:
            raise ValueError()
        return identity
    except (OSError, ValueError, TypeError, KeyError) as exc:
        raise LicenseError(
            "device_error", "设备凭据无法验证，请联系管理员迁移授权；不会自动生成新设备。"
        ) from exc
