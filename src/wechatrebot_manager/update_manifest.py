"""Signed update manifest types and validation rules, independent of update I/O."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import urllib.parse

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version

from .payload import _valid_digest


PRODUCT_ID = "wechatrebot-desktop"
MAX_UNCOMPRESSED_BYTES = 8 * 1024**3
MAX_INCREMENTAL_PACKAGES = 64


class UpdateError(RuntimeError):
    """Raised when remote update metadata or package data cannot be trusted."""


class MandatoryUpdateUnavailable(UpdateError):
    """Raised when the signed policy blocks this client but offers no newer build."""


@dataclass(frozen=True, slots=True)
class UpdateChannel:
    manifest_url: str
    channel: str
    key_id: str
    public_key: bytes
    check_interval_seconds: int = 600
    request_timeout_seconds: int = 20
    max_package_bytes: int = 2 * 1024**3

    @property
    def enabled(self) -> bool:
        return bool(self.manifest_url)


@dataclass(frozen=True, slots=True)
class UpdateRelease:
    version: str
    title: str
    notes: tuple[str, ...]
    published_at: str
    package_url: str
    package_size: int
    package_sha256: str
    mandatory: bool
    build: int | None = None
    minimum_supported_version: str = ""
    uncompressed_size: int = 0
    package_kind: str = "default"
    base_fingerprint: str = ""
    environment_size: int = 0


def _safe_https_url(value: object, *, allow_empty: bool = False) -> str:
    if allow_empty and (value is None or str(value).strip() == ""):
        return ""
    if not isinstance(value, str):
        raise UpdateError("更新地址格式错误。")
    url = value.strip()
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise UpdateError("更新地址必须是无账号信息的 HTTPS 地址。")
    return url


def canonical_manifest_bytes(payload: dict[str, object]) -> bytes:
    unsigned = dict(payload)
    unsigned.pop("signature", None)
    return json.dumps(
        unsigned,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _verify_manifest_signature(
    payload: dict[str, object], channel: UpdateChannel
) -> None:
    signature = payload.get("signature")
    if not isinstance(signature, dict):
        raise UpdateError("更新清单缺少数字签名。")
    if (
        signature.get("algorithm") != "ed25519"
        or signature.get("key_id") != channel.key_id
    ):
        raise UpdateError("更新清单签名身份不匹配。")
    try:
        value = base64.b64decode(str(signature["value"]), validate=True)
        Ed25519PublicKey.from_public_bytes(channel.public_key).verify(
            value, canonical_manifest_bytes(payload)
        )
    except (KeyError, TypeError, ValueError, InvalidSignature) as exc:
        raise UpdateError("更新清单数字签名校验失败。") from exc


def _manifest_release_metadata(
    payload: dict[str, object], channel: UpdateChannel
) -> dict[str, object]:
    _verify_manifest_signature(payload, channel)
    if (
        payload.get("schema_version") != 1
        or payload.get("product") != PRODUCT_ID
        or payload.get("channel") != channel.channel
    ):
        raise UpdateError("更新清单与当前产品或更新通道不匹配。")
    notes_value = payload.get("notes", [])
    if not isinstance(notes_value, list):
        raise UpdateError("更新说明格式错误。")
    try:
        latest_text = str(payload["version"]).strip()
        latest = Version(latest_text)
        minimum_text = str(payload.get("minimum_supported_version") or "0.0.0")
        minimum = Version(minimum_text)
    except (KeyError, TypeError, ValueError, InvalidVersion) as exc:
        raise UpdateError("更新版本格式错误。") from exc
    if not latest_text or str(latest) != latest_text:
        raise UpdateError("更新版本必须使用规范版本号。")
    try:
        build_value = payload.get("build")
        build = int(build_value) if build_value is not None else None
    except (TypeError, ValueError) as exc:
        raise UpdateError("更新构建号格式错误。") from exc
    if build is not None and build < 0:
        raise UpdateError("更新构建号无效。")
    if not isinstance(payload.get("mandatory", False), bool):
        raise UpdateError("强制更新标记格式错误。")
    environment_size = payload.get("environment_size", 0)
    if type(environment_size) is not int or not 0 <= environment_size <= MAX_UNCOMPRESSED_BYTES:
        raise UpdateError("运行环境下载大小无效。")
    notes: list[str] = []
    for item in notes_value:
        if not isinstance(item, str):
            raise UpdateError("更新说明格式错误。")
        normalized = item.strip()
        if normalized:
            notes.append(normalized[:500])
    return {
        "version": latest_text,
        "title": str(payload.get("title") or f"版本 {latest_text}").strip()[:100],
        "notes": tuple(notes[:20]),
        "published_at": str(payload.get("published_at") or "").strip()[:64],
        "mandatory": payload.get("mandatory") is True,
        "build": build,
        "minimum_supported_version": str(minimum),
        "environment_size": environment_size,
    }


def _manifest_package_fields(
    package: object, channel: UpdateChannel
) -> dict[str, object]:
    if not isinstance(package, dict):
        raise UpdateError("更新清单缺少安装包信息。")
    if package.get("format") not in (None, "zip"):
        raise UpdateError("更新包格式不受支持。")
    if (
        type(package.get("size")) is not int
        or type(package.get("uncompressed_size", 0)) is not int
    ):
        raise UpdateError("更新安装包大小格式错误。")
    try:
        package_size = int(package["size"])
        package_sha256 = package["sha256"]
        uncompressed_size = int(package.get("uncompressed_size", 0))
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("更新安装包摘要格式错误。") from exc
    if not (0 < package_size <= channel.max_package_bytes):
        raise UpdateError("更新包大小超出允许范围。")
    if not _valid_digest(package_sha256):
        raise UpdateError("更新包 SHA-256 格式错误。")
    if not (0 <= uncompressed_size <= MAX_UNCOMPRESSED_BYTES):
        raise UpdateError("更新包展开体积无效。")
    return {
        "package_url": _safe_https_url(package.get("url")),
        "package_size": package_size,
        "package_sha256": package_sha256.upper(),
        "uncompressed_size": uncompressed_size,
    }


def _manifest_package_releases(
    payload: dict[str, object], channel: UpdateChannel, metadata: dict[str, object]
) -> tuple[UpdateRelease, ...]:
    component_only = _component_only_manifest(payload)
    releases = [] if component_only else [UpdateRelease(
        **metadata, **_manifest_package_fields(payload.get("package"), channel)
    )]
    candidates = payload.get("incremental_packages", [])
    if not isinstance(candidates, list) or len(candidates) > MAX_INCREMENTAL_PACKAGES:
        raise UpdateError("增量更新包列表格式错误或数量超限。")
    if component_only and not candidates:
        raise UpdateError("组件更新清单没有可用的基线安装包。")
    fingerprints: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict) or not _valid_digest(candidate.get("base_fingerprint")):
            raise UpdateError("增量更新包基线指纹格式错误。")
        fingerprint = candidate["base_fingerprint"].upper()
        if fingerprint in fingerprints:
            raise UpdateError("增量更新包包含重复基线指纹。")
        fingerprints.add(fingerprint)
        releases.append(UpdateRelease(
            **metadata, **_manifest_package_fields(candidate.get("package"), channel),
            package_kind="incremental", base_fingerprint=fingerprint,
        ))
    return tuple(releases)


def _component_only_manifest(payload: dict[str, object]) -> bool:
    protocol = payload.get("component_update_protocol")
    if "component_update_protocol" in payload and (type(protocol) is not int or protocol != 1):
        raise UpdateError("组件更新协议版本不受支持。")
    component_only = protocol == 1 and "package" not in payload
    if component_only and not isinstance(payload.get("version"), str):
        raise UpdateError("组件更新版本格式错误。")
    build = payload.get("build")
    if component_only and build is not None and (type(build) is not int or build < 0):
        raise UpdateError("组件更新构建号必须为非负整数。")
    return component_only


def manifest_package_releases(
    payload: dict[str, object], channel: UpdateChannel
) -> tuple[UpdateRelease, ...]:
    """Verify every signed default or component package before publication."""
    metadata = _manifest_release_metadata(payload, channel)
    return _manifest_package_releases(payload, channel, metadata)


def is_newer_update(
    version: str, build: int | None, *, current_version: str, current_build: int = 0
) -> bool:
    """Use the same version/build ordering for update offers and installation rechecks."""
    try:
        latest, current = Version(version), Version(current_version)
    except (TypeError, ValueError, InvalidVersion) as exc:
        raise UpdateError("更新版本格式错误。") from exc
    return latest > current or (latest == current and build is not None and build > current_build)
