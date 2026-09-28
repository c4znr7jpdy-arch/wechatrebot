from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import resources
import json
import os
import math
from pathlib import Path, PurePosixPath, PureWindowsPath
import shutil
import stat
import subprocess
import sys
import time
from typing import Callable
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.version import InvalidVersion, Version

from .install_paths import INSTALL_MARKER
from .json_io import write_json_atomic
from .payload import (
    PayloadError,
    _is_user_config,
    install_payload,
    payload_relative_paths,
    payload_removed_paths,
    payload_requires_runtime_stop,
    sha256_file,
    validate_incremental_base,
    verify_payload,
)
from .single_instance import SingleInstanceLock
from .update_errors import UpdateFailure
from .update_logger import UpdateLogger


PRODUCT_ID = "wechatrebot-desktop"
UPDATE_RESULT_FILE = "update-result.json"
UPDATE_JOURNAL_FILE = "update-journal.json"
UPDATE_STARTUP_TIMEOUT_SECONDS = 90
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_FILES = 200_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024**3
ProgressCallback = Callable[[str, int, int], None]


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


@dataclass(frozen=True, slots=True)
class PreparedUpdate:
    release: UpdateRelease
    working_root: Path
    payload_root: Path
    archive_path: Path


@dataclass(frozen=True, slots=True)
class UpdatePlan:
    plan_path: Path
    helper_executable: Path


def _read_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UpdateError(f"更新配置无法读取：{exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("更新配置格式错误。")
    return payload


def _embedded_channel_payload() -> dict[str, object]:
    try:
        text = (
            resources.files("wechatrebot_manager")
            .joinpath("update-channel.json")
            .read_text(encoding="utf-8")
        )
        payload = json.loads(text)
    except (OSError, TypeError, json.JSONDecodeError) as exc:
        raise UpdateError(f"内置更新配置无法读取：{exc}") from exc
    if not isinstance(payload, dict):
        raise UpdateError("内置更新配置格式错误。")
    return payload


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


def load_update_channel(install_root: str | Path | None = None) -> UpdateChannel:
    payload = _embedded_channel_payload()
    if payload.get("schema_version") != 1:
        raise UpdateError("不支持的更新配置版本。")
    manifest_url = _safe_https_url(payload.get("manifest_url"), allow_empty=True)
    check_interval = payload.get("check_interval_seconds", 600)
    timeout = payload.get("request_timeout_seconds", 20)
    if install_root is not None:
        override_path = (
            Path(install_root).expanduser().resolve()
            / "config"
            / "update-channel.json"
        )
        if override_path.is_file():
            override = _read_json_object(override_path)
            if override.get("schema_version") != 1:
                raise UpdateError("本机更新配置版本不受支持。")
            manifest_url = _safe_https_url(
                override.get("manifest_url", manifest_url), allow_empty=True
            )
            check_interval = override.get(
                "check_interval_seconds", check_interval
            )
            timeout = override.get("request_timeout_seconds", timeout)
    try:
        public_key = base64.b64decode(
            str(payload["public_key_base64"]), validate=True
        )
        key_id = str(payload["key_id"]).strip()
        channel = str(payload.get("channel") or "stable").strip()
        check_interval_int = int(check_interval)
        timeout_int = int(timeout)
        max_package_bytes = int(payload.get("max_package_bytes", 2 * 1024**3))
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("更新配置缺少可信公钥或限制参数。") from exc
    expected_key_id = hashlib.sha256(public_key).hexdigest().upper()[:16]
    if len(public_key) != 32 or key_id != expected_key_id:
        raise UpdateError("更新公钥指纹不匹配。")
    if not channel or not (300 <= check_interval_int <= 604_800):
        raise UpdateError("更新检查间隔无效。")
    if not (5 <= timeout_int <= 120):
        raise UpdateError("更新请求超时无效。")
    if not (1024 * 1024 <= max_package_bytes <= 8 * 1024**3):
        raise UpdateError("更新包大小限制无效。")
    return UpdateChannel(
        manifest_url=manifest_url,
        channel=channel,
        key_id=key_id,
        public_key=public_key,
        check_interval_seconds=check_interval_int,
        request_timeout_seconds=timeout_int,
        max_package_bytes=max_package_bytes,
    )


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


def parse_update_manifest(
    payload: dict[str, object],
    channel: UpdateChannel,
    *,
    current_version: str,
    current_build: int = 0,
) -> UpdateRelease | None:
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
        current = Version(current_version)
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
    if current < minimum and latest <= current:
        raise MandatoryUpdateUnavailable("当前版本低于最低支持版本，但更新清单未提供可用的新版本。")
    if latest < current or (
        latest == current and (build is None or build <= current_build)
    ):
        return None

    package = payload.get("package")
    if not isinstance(package, dict):
        raise UpdateError("更新清单缺少安装包信息。")
    if package.get("format") not in {None, "zip"}:
        raise UpdateError("更新包格式不受支持。")
    try:
        package_size = int(package["size"])
        package_sha256 = str(package["sha256"]).upper()
        uncompressed_size = int(package.get("uncompressed_size", 0) or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("更新安装包摘要格式错误。") from exc
    if not (0 < package_size <= channel.max_package_bytes):
        raise UpdateError("更新包大小超出允许范围。")
    if len(package_sha256) != 64 or any(
        character not in "0123456789ABCDEF" for character in package_sha256
    ):
        raise UpdateError("更新包 SHA-256 格式错误。")
    if not (0 <= uncompressed_size <= MAX_UNCOMPRESSED_BYTES):
        raise UpdateError("更新包展开体积无效。")
    if build is not None and build < 0:
        raise UpdateError("更新构建号无效。")
    if not isinstance(payload.get("mandatory", False), bool):
        raise UpdateError("强制更新标记格式错误。")
    notes: list[str] = []
    for item in notes_value[:20]:
        if not isinstance(item, str):
            raise UpdateError("更新说明格式错误。")
        normalized = item.strip()
        if normalized:
            notes.append(normalized[:500])
    title = str(payload.get("title") or f"版本 {latest_text}").strip()[:100]
    published_at = str(payload.get("published_at") or "").strip()[:64]
    return UpdateRelease(
        version=latest_text,
        title=title,
        notes=tuple(notes),
        published_at=published_at,
        package_url=_safe_https_url(package.get("url")),
        package_size=package_size,
        package_sha256=package_sha256,
        mandatory=payload.get("mandatory") is True or current < minimum,
        build=build,
        minimum_supported_version=str(minimum),
        uncompressed_size=uncompressed_size,
    )


def _read_limited(response, limit: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if total > limit:
            raise UpdateError("更新清单过大。")
    return b"".join(chunks)


def check_for_update(
    channel: UpdateChannel,
    *,
    current_version: str,
    current_build: int = 0,
) -> UpdateRelease | None:
    if not channel.enabled:
        return None
    request = urllib.request.Request(
        channel.manifest_url,
        headers={
            "Accept": "application/json",
            "User-Agent": f"WeChatReBot/{current_version}",
            "Cache-Control": "no-cache",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=channel.request_timeout_seconds
        ) as response:
            _safe_https_url(response.geturl())
            content = _read_limited(response, MAX_MANIFEST_BYTES)
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise UpdateError(f"暂时无法连接更新服务：{exc}") from exc
    try:
        payload = json.loads(content.decode("utf-8-sig"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise UpdateError("更新清单不是有效 JSON。") from exc
    if not isinstance(payload, dict):
        raise UpdateError("更新清单根节点格式错误。")
    return parse_update_manifest(
        payload,
        channel,
        current_version=current_version,
        current_build=current_build,
    )


def _safe_archive_path(value: str) -> Path:
    if not value or "\\" in value:
        raise UpdateError("更新包包含不安全路径。")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in posix.parts)
        or not posix.parts
        or posix.parts[0] != "payload"
    ):
        raise UpdateError(f"更新包路径越界：{value}")
    if any(os.path.isreserved(part) for part in posix.parts):
        raise UpdateError(f"更新包包含 Windows 保留路径：{value}")
    return Path(*posix.parts)


def _extended_windows_path(path: Path) -> Path:
    """Return an absolute Win32 extended path without changing non-Windows paths."""
    resolved = path.expanduser().resolve()
    if os.name != "nt":
        return resolved
    raw = str(resolved)
    if raw.startswith("\\\\?\\"):
        return resolved
    if raw.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + raw[2:])
    return Path("\\\\?\\" + raw)


def _path_within(path: Path, parent: Path) -> bool:
    """Compare paths consistently when one side uses a Win32 extended prefix."""
    try:
        _extended_windows_path(path).relative_to(_extended_windows_path(parent))
        return True
    except ValueError:
        return False


def extract_update_archive(
    archive_path: str | Path,
    destination: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    archive = Path(archive_path).expanduser().resolve()
    root = Path(destination).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(archive) as package:
            items = package.infolist()
            if not items or len(items) > MAX_ARCHIVE_FILES:
                raise UpdateError("更新包文件数量异常。")
            total_size = sum(item.file_size for item in items)
            if total_size > MAX_UNCOMPRESSED_BYTES:
                raise UpdateError("更新包展开后体积超出限制。")
            planned: list[tuple[zipfile.ZipInfo, Path]] = []
            for item in items:
                relative = _safe_archive_path(item.filename)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise UpdateError("更新包不能包含符号链接。")
                planned.append((item, relative))

            # ZIP entries can legitimately be longer than the legacy Win32
            # MAX_PATH limit once the update's temporary directories are added.
            # Keep normal paths for ordinary archives and opt into the extended
            # namespace only when an entry needs it.
            filesystem_root = root
            if os.name == "nt" and any(
                len(str(root / relative)) >= 248 for _item, relative in planned
            ):
                filesystem_root = _extended_windows_path(root)

            for index, (item, relative) in enumerate(planned, start=1):
                target = filesystem_root / relative
                if not _path_within(target, filesystem_root):
                    raise UpdateError("更新包文件越过暂存目录。")
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with package.open(item) as source, target.open("wb") as output:
                        shutil.copyfileobj(source, output, length=1024 * 1024)
                if progress_callback is not None:
                    progress_callback("extract", index, len(items))
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as exc:
        raise UpdateError(f"更新包无法展开：{exc}") from exc
    payload_root = filesystem_root / "payload"
    if not payload_root.is_dir():
        raise UpdateError("更新包缺少 payload 目录。")
    return payload_root


def _is_preserved_user_config(relative: Path) -> bool:
    return _is_user_config(relative)


def _update_target_paths(payload_root: Path) -> set[Path]:
    paths = set(payload_relative_paths(payload_root))
    paths.update(payload_removed_paths(payload_root))
    if Path("manager/WeChatReBot.exe") in paths:
        paths.update({Path("启动机器人.exe"), Path("卸载机器人.exe")})
    paths.update({Path(INSTALL_MARKER), Path("config/installed-payload.json")})
    return paths


def _validate_update_baseline(payload_root: Path, install_root: Path) -> None:
    validate_incremental_base(payload_root, install_root)


def assert_update_runtime_stopped(
    payload_root: str | Path, install_root: str | Path
) -> None:
    """Read-only guard: never replace runtime files while their users are alive."""
    payload = Path(payload_root).expanduser().resolve()
    if not payload_requires_runtime_stop(payload):
        return
    root = Path(install_root).expanduser().resolve()
    if os.name != "nt":
        raise UpdateError("当前平台无法确认机器人运行状态，已取消运行时更新。")
    from .launcher import process_identity
    from .uninstaller import _iter_windows_process_ids, _process_module_paths

    processes = _iter_windows_process_ids()
    if not processes:
        raise UpdateError("无法检查机器人进程状态，已取消运行时更新。")
    changed_binaries = {
        os.path.normcase(str((root / relative).resolve()))
        for relative in _update_target_paths(payload)
        if relative.suffix.casefold() in {".dll", ".pyd", ".exe"}
        and relative.parts[0].casefold() in {"app", "runtime"}
    }
    blockers: list[int] = []
    for pid in processes:
        if pid in {0, os.getpid()}:
            continue
        identity = process_identity(pid)
        in_runtime = bool(
            identity
            and any(
                _path_within(Path(identity.executable), root / directory)
                for directory in ("app", "runtime")
            )
        )
        holds_binary = bool(changed_binaries) and any(
            os.path.normcase(str(path.resolve())) in changed_binaries
            for path in _process_module_paths(pid)
        )
        if in_runtime or holds_binary:
            blockers.append(pid)
    if blockers:
        raise UpdateFailure(
            "UPD-1005",
            "更新文件仍被进程占用：" + ", ".join(map(str, blockers)),
            public_message="请先停止机器人；若微信仍占用待更新文件，请退出微信后重试。",
        )


def _estimated_rollback_bytes(
    install_root: Path, payload_root: Path | None = None
) -> int:
    """Estimate the files the helper must retain to make rollback possible."""
    if payload_root is not None:
        return sum(
            (install_root / relative).stat().st_size
            for relative in _update_target_paths(payload_root)
            if not _is_preserved_user_config(relative)
            and (install_root / relative).is_file()
            and not (install_root / relative).is_symlink()
        )
    manifest_path = install_root / "config" / "installed-payload.json"
    try:
        manifest = _read_json_object(manifest_path)
    except UpdateError:
        manifest = {}
    entries = manifest.get("files", [])
    total = 0
    seen: set[str] = set()
    if isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            try:
                relative = Path(*PurePosixPath(str(entry.get("path") or "")).parts)
                key = relative.as_posix().casefold()
                candidate = (install_root / relative).resolve()
                candidate.relative_to(install_root)
            except (OSError, ValueError):
                continue
            if key in seen or _is_preserved_user_config(relative):
                continue
            seen.add(key)
            try:
                if candidate.is_file() and not candidate.is_symlink():
                    total += candidate.stat().st_size
            except OSError:
                continue
    for relative in (
        Path("config/installed-payload.json"),
        Path(INSTALL_MARKER),
        Path("卸载机器人.exe"),
    ):
        try:
            candidate = install_root / relative
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
        except OSError:
            continue
    return total


def _ensure_update_disk_space(
    install_root: Path,
    release: UpdateRelease,
    *,
    uncompressed_size: int | None = None,
    staged: bool = False,
    payload_root: Path | None = None,
) -> None:
    expanded = uncompressed_size or release.uncompressed_size
    if not expanded:
        expanded = max(release.package_size * 4, 64 * 1024**2)
    rollback = (
        _estimated_rollback_bytes(install_root, payload_root)
        if payload_root is not None
        else 0
    )
    # Before extraction the changed file set is unknown. Check download space
    # now and the exact rollback budget below after verifying the payload, rather
    # than stat every installed runtime file for a small component update.
    # Before downloading, both the extracted payload and install transaction
    # staging must fit. Once downloaded/extracted, only install staging remains
    # to be allocated. Keep two old-file snapshots: the crash-safe outer journal
    # and install_payload's own in-process transaction rollback.
    required = (0 if staged else release.package_size) + expanded * (1 if staged else 2)
    required += rollback * 2 + 64 * 1024**2
    try:
        free = shutil.disk_usage(install_root).free
    except OSError as exc:
        raise UpdateFailure("UPD-1015", "无法检查更新目录所在磁盘空间。") from exc
    if free < required:
        missing_mb = max(1, math.ceil((required - free) / (1024 * 1024)))
        raise UpdateFailure(
            "UPD-1004",
            f"预计需要 {required} 字节，可用 {free} 字节。",
            public_message=f"磁盘空间不足，请至少释放 {missing_mb} MB 空间后重试。",
        )


def prepare_update(
    release: UpdateRelease,
    channel: UpdateChannel,
    install_root: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
) -> PreparedUpdate:
    root = Path(install_root).expanduser().resolve()
    updates_root = (root / "updates").resolve()
    try:
        updates_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateFailure("UPD-1015", str(exc)) from exc
    try:
        updates_root.relative_to(root)
    except ValueError as exc:
        raise UpdateFailure("UPD-1015", "更新暂存目录越过安装根目录。") from exc
    working = updates_root / f".pending-{release.version}-{uuid.uuid4().hex}"
    logger = UpdateLogger(root)
    _ensure_update_disk_space(root, release)
    try:
        working.mkdir()
    except OSError as exc:
        raise UpdateFailure("UPD-1015", str(exc)) from exc
    archive = working / "update.zip.download"
    request = urllib.request.Request(
        release.package_url,
        headers={"Accept": "application/zip", "User-Agent": f"WeChatReBot/{release.version}"},
    )
    try:
        logger.write("download_started", state="downloading", version=release.version)
        digest = hashlib.sha256()
        received = 0
        with urllib.request.urlopen(
            request, timeout=channel.request_timeout_seconds
        ) as response, archive.open("wb") as output:
            _safe_https_url(response.geturl())
            length = response.headers.get("Content-Length")
            if length is not None and int(length) != release.package_size:
                raise UpdateError("更新包大小与发布清单不一致。")
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                received += len(chunk)
                if received > min(channel.max_package_bytes, release.package_size):
                    raise UpdateError("更新包下载体积超出发布清单。")
                output.write(chunk)
                digest.update(chunk)
                if progress_callback is not None:
                    progress_callback("download", received, release.package_size)
        if received != release.package_size:
            raise UpdateError("更新包下载不完整。")
        if digest.hexdigest().upper() != release.package_sha256:
            raise UpdateFailure("UPD-1003", "下载文件摘要和签名清单不一致。")
        final_archive = working / "update.zip"
        os.replace(archive, final_archive)
        try:
            payload_root = extract_update_archive(
                final_archive,
                working / "extracted",
                progress_callback=progress_callback,
            )
        except UpdateError as exc:
            raise UpdateFailure("UPD-1012", str(exc)) from exc
        actual_uncompressed = sum(
            item.stat().st_size
            for item in payload_root.rglob("*")
            if item.is_file()
        )
        if release.uncompressed_size and actual_uncompressed != release.uncompressed_size:
            raise UpdateFailure("UPD-1003", "更新包展开体积和签名清单不一致。")
        verified = verify_payload(
            payload_root,
            progress_callback=progress_callback,
        )
        _validate_update_baseline(payload_root, root)
        _ensure_update_disk_space(
            root,
            release,
            uncompressed_size=actual_uncompressed,
            staged=True,
            payload_root=payload_root,
        )
        manager = payload_root / "manager" / "WeChatReBot.exe"
        if not manager.is_file() and verified.manifest.get("schema_version") != 2:
            raise UpdateError("更新包缺少桌面管理器。")
        logger.write("package_verified", state="verified", version=release.version)
        return PreparedUpdate(release, working, payload_root, final_archive)
    except Exception as exc:
        if isinstance(exc, UpdateFailure):
            logger.write(
                "prepare_failed",
                state="failed",
                version=release.version,
                error_code=exc.definition.code,
                message=exc.detail,
            )
        elif isinstance(exc, PayloadError):
            logger.write(
                "package_verification_failed",
                state="failed",
                version=release.version,
                error_code="UPD-1003",
                message=str(exc),
            )
        else:
            logger.write(
                "download_failed",
                state="failed",
                version=release.version,
                error_code="UPD-1002",
                message=str(exc),
            )
        shutil.rmtree(_extended_windows_path(working), ignore_errors=True)
        if isinstance(exc, UpdateFailure):
            raise
        if isinstance(exc, PayloadError):
            raise UpdateFailure("UPD-1003", str(exc)) from exc
        if isinstance(exc, PermissionError):
            raise UpdateFailure("UPD-1015", str(exc)) from exc
        if isinstance(exc, (OSError, urllib.error.URLError)):
            raise UpdateFailure("UPD-1002", str(exc)) from exc
        if isinstance(exc, UpdateError):
            raise UpdateFailure("UPD-1003", str(exc)) from exc
        raise


def create_update_plan(
    prepared: PreparedUpdate,
    install_root: str | Path,
    *,
    executable: str | Path | None = None,
    parent_pid: int | None = None,
    current_version: str = "",
) -> UpdatePlan:
    root = Path(install_root).expanduser().resolve()
    updates_root = (root / "updates").resolve()
    working = prepared.working_root.resolve()
    try:
        working.relative_to(updates_root)
    except ValueError as exc:
        raise UpdateError("更新计划引用了暂存目录之外的文件。") from exc
    if not _path_within(prepared.payload_root, working):
        raise UpdateError("更新计划引用了暂存目录之外的文件。")
    verified = verify_payload(prepared.payload_root)
    _validate_update_baseline(prepared.payload_root, root)
    source_executable = Path(executable or sys.executable).expanduser().resolve()
    if not source_executable.is_file() or source_executable.suffix.casefold() != ".exe":
        raise UpdateError("无法创建独立更新程序。")
    helper = working / "WeChatReBot-Updater.exe"
    helper_source = prepared.payload_root / "manager" / "WeChatReBot.exe"
    if not helper_source.is_file():
        if verified.manifest.get("schema_version") != 2:
            raise UpdateError("更新包缺少可运行的独立更新程序。")
        helper_source = source_executable
    shutil.copy2(helper_source, helper)
    application_pid = int(parent_pid if parent_pid is not None else os.getpid())
    update_id = uuid.uuid4().hex
    plan_path = working / "update-plan.json"
    write_json_atomic(
        plan_path,
        {
            "schema_version": 1,
            "update_id": update_id,
            "version": prepared.release.version,
            "current_version": current_version,
            "install_root": str(root),
            "payload_root": str(prepared.payload_root.resolve()),
            "target_executable": str(root / "manager" / "WeChatReBot.exe"),
            # parent_pid is retained for update helpers shipped before 0.8.7.
            "parent_pid": application_pid,
            "wait_pids": list(
                _update_wait_pids(
                    source_executable,
                    application_pid,
                    include_bootloader=parent_pid is None,
                )
            ),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    )
    return UpdatePlan(plan_path, helper)


def launch_update_helper(plan: UpdatePlan) -> None:
    environment = os.environ.copy()
    environment["WECHATREBOT_UPDATE_HELPER"] = "1"
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
        subprocess, "DETACHED_PROCESS", 0
    )
    subprocess.Popen(
        [str(plan.helper_executable), "apply-update", "--plan", str(plan.plan_path)],
        cwd=plan.plan_path.parent,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _wait_for_process_exit(pid: int, timeout_seconds: int = 180) -> None:
    if pid <= 0 or pid == os.getpid():
        return
    if os.name == "nt":
        import ctypes

        synchronize = 0x00100000
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return
        try:
            result = ctypes.windll.kernel32.WaitForSingleObject(
                handle, timeout_seconds * 1000
            )
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
        if result == 0x00000102:
            raise UpdateError("等待旧版本退出超时。")
        return
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.25)
    raise UpdateError("等待旧版本退出超时。")


def _process_executable(pid: int) -> Path | None:
    """Return a Windows process image path without requiring elevated access."""
    if os.name != "nt" or pid <= 0:
        return None
    import ctypes
    from ctypes import wintypes

    query_limited_information = 0x1000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(
        query_limited_information,
        False,
        pid,
    )
    if not handle:
        return None
    try:
        size = wintypes.DWORD(32_768)
        buffer = ctypes.create_unicode_buffer(size.value)
        if not kernel32.QueryFullProcessImageNameW(
            handle,
            0,
            buffer,
            ctypes.byref(size),
        ):
            return None
        return Path(buffer.value)
    finally:
        kernel32.CloseHandle(handle)


def _update_wait_pids(
    source_executable: Path,
    application_pid: int,
    *,
    include_bootloader: bool,
) -> tuple[int, ...]:
    """Include PyInstaller's one-file bootloader when it owns the same EXE."""
    candidates = [application_pid]
    if include_bootloader and os.name == "nt" and getattr(sys, "frozen", False):
        bootloader_pid = os.getppid()
        bootloader_executable = _process_executable(bootloader_pid)
        if (
            bootloader_pid > 0
            and bootloader_pid != application_pid
            and bootloader_executable is not None
            and os.path.normcase(str(bootloader_executable.resolve()))
            == os.path.normcase(str(source_executable.resolve()))
        ):
            candidates.append(bootloader_pid)
    return tuple(dict.fromkeys(candidates))


def _plan_wait_pids(plan: dict[str, object], parent_pid: int) -> tuple[int, ...]:
    raw = plan.get("wait_pids", [parent_pid])
    if not isinstance(raw, list) or not raw or len(raw) > 4:
        raise UpdateError("更新计划包含无效的进程等待列表。")
    try:
        result = tuple(dict.fromkeys(int(item) for item in raw))
    except (TypeError, ValueError) as exc:
        raise UpdateError("更新计划包含无效的进程等待列表。") from exc
    if any(pid < 0 for pid in result):
        raise UpdateError("更新计划包含无效的进程等待列表。")
    return result


def _validated_plan(plan_path: str | Path) -> tuple[dict[str, object], Path, Path, Path]:
    path = Path(plan_path).expanduser().resolve()
    payload = _read_json_object(path)
    try:
        root = Path(str(payload["install_root"])).expanduser().resolve()
        payload_root = Path(str(payload["payload_root"])).expanduser().resolve()
        target = Path(str(payload["target_executable"])).expanduser().resolve()
        parent_pid = int(payload["parent_pid"])
        update_id = str(payload["update_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("更新计划缺少必要字段。") from exc
    updates_root = (root / "updates").resolve()
    expected_target = (root / "manager" / "WeChatReBot.exe").resolve()
    try:
        path.relative_to(updates_root)
    except ValueError as exc:
        raise UpdateError("更新计划路径越过安装目录。") from exc
    if not _path_within(payload_root, path.parent):
        raise UpdateError("更新计划路径越过安装目录。")
    if (
        payload.get("schema_version") != 1
        or len(update_id) != 32
        or any(character not in "0123456789abcdef" for character in update_id)
        or target != expected_target
        or parent_pid < 0
        or not (root / INSTALL_MARKER).is_file()
    ):
        raise UpdateError("更新计划与当前安装不匹配。")
    _plan_wait_pids(payload, parent_pid)
    return payload, root, payload_root, target


def _restart_manager(
    target: Path,
    install_root: Path,
    *,
    update_id: str = "",
    current_version: str = "",
    version: str = "",
) -> subprocess.Popen | None:
    if not target.is_file():
        return None
    environment = os.environ.copy()
    for name, value in (
        ("WECHATREBOT_UPDATE_ID", update_id),
        ("WECHATREBOT_UPDATE_FROM", current_version),
        ("WECHATREBOT_UPDATE_TO", version),
    ):
        if value:
            environment[name] = value
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(
        [str(target)],
        cwd=install_root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def _snapshot_update_targets(
    payload_root: Path,
    install_root: Path,
    rollback_root: Path,
    *,
    update_id: str,
    version: str,
) -> dict[str, object]:
    paths = _update_target_paths(payload_root)
    files_root = rollback_root / "files"
    files_root.mkdir(parents=True, exist_ok=True)
    snapshots: list[dict[str, object]] = []
    for index, relative in enumerate(sorted(paths, key=lambda item: item.as_posix().casefold())):
        target = (install_root / relative).resolve()
        try:
            target.relative_to(install_root)
        except ValueError as exc:
            raise UpdateError("更新文件路径越过安装目录。") from exc
        if (
            _is_preserved_user_config(relative)
            and target.is_file()
            and not target.is_symlink()
        ):
            continue
        if target.exists() and (not target.is_file() or target.is_symlink()):
            raise UpdateError(f"更新目标不是普通文件：{relative.as_posix()}")
        exists = target.is_file()
        backup_relative = f"files/{index:08X}" if exists else ""
        if exists:
            shutil.copy2(target, rollback_root / backup_relative)
        snapshots.append(
            {
                "path": relative.as_posix(),
                "existed": exists,
                "backup": backup_relative,
            }
        )
    return {
        "schema_version": 1,
        "update_id": update_id,
        "version": version,
        "state": "backed_up",
        "rollback_root": rollback_root.relative_to(install_root).as_posix(),
        "files": snapshots,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def _restore_update_targets(
    journal: dict[str, object], install_root: Path
) -> None:
    rollback_relative = Path(*PurePosixPath(str(journal.get("rollback_root") or "")).parts)
    rollback_root = (install_root / rollback_relative).resolve()
    try:
        rollback_root.relative_to(install_root)
    except ValueError as exc:
        raise UpdateFailure("UPD-1010", "回滚目录越过安装根目录。") from exc
    files = journal.get("files")
    if not isinstance(files, list):
        raise UpdateFailure("UPD-1010", "回滚清单格式无效。")
    failures: list[str] = []
    for entry in reversed(files):
        try:
            if not isinstance(entry, dict):
                raise OSError("回滚文件条目无效")
            relative = Path(*PurePosixPath(str(entry.get("path") or "")).parts)
            target = (install_root / relative).resolve()
            target.relative_to(install_root)
            if entry.get("existed") is True:
                backup = (rollback_root / str(entry.get("backup") or "")).resolve()
                backup.relative_to(rollback_root)
                if not backup.is_file():
                    raise OSError(f"回滚备份缺失：{relative.as_posix()}")
                target.parent.mkdir(parents=True, exist_ok=True)
                staged = target.with_name(f".{target.name}.restore-{uuid.uuid4().hex}")
                shutil.copy2(backup, staged)
                os.replace(staged, target)
            else:
                target.unlink(missing_ok=True)
        except (OSError, ValueError) as exc:
            failures.append(str(exc))
    if failures:
        raise UpdateFailure("UPD-1010", "；".join(failures[:5]))


def _startup_confirmed(flag_path: Path, update_id: str) -> bool:
    try:
        payload = _read_json_object(flag_path)
    except UpdateError:
        return False
    return payload.get("update_id") == update_id and payload.get("ok") is True


def _wait_for_startup_confirmation(
    flag_path: Path,
    update_id: str,
    process: subprocess.Popen | None,
    *,
    timeout_seconds: int = UPDATE_STARTUP_TIMEOUT_SECONDS,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _startup_confirmed(flag_path, update_id):
            return
        if process is not None and process.poll() is not None:
            # PyInstaller's one-file parent stays alive until the application
            # child exits. Keep polling briefly for OS process hand-off.
            time.sleep(0.5)
        else:
            time.sleep(0.25)
    raise UpdateFailure("UPD-1009", "新版本未在规定时间内完成启动确认。")


def _stop_restarted_manager(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except Exception:
        try:
            process.kill()
            process.wait(timeout=5)
        except Exception:
            return


def apply_update_plan(
    plan_path: str | Path,
    *,
    restart: bool = True,
) -> int:
    try:
        plan_probe = _read_json_object(Path(plan_path).expanduser().resolve())
    except UpdateError:
        plan_probe = {}
    if plan_probe.get("action") == "recover-interrupted-update":
        return recover_update_plan(plan_path)
    root: Path | None = None
    target: Path | None = None
    version = ""
    current_version = ""
    update_id = ""
    logger: UpdateLogger | None = None
    rollback_root: Path | None = None
    journal_path: Path | None = None
    journal: dict[str, object] | None = None
    restarted: subprocess.Popen | None = None
    lock: SingleInstanceLock | None = None
    failure: BaseException | None = None
    result = 1
    try:
        plan, root, payload_root, target = _validated_plan(plan_path)
        version = str(plan.get("version") or "")
        current_version = str(plan.get("current_version") or "")
        update_id = str(plan["update_id"])
        logger = UpdateLogger(root)
        lock = SingleInstanceLock(f"update-helper:{root}")
        if not lock.acquire():
            logger.write("duplicate_helper", state="rejected", version=version)
            return 2
        logger.write("apply_started", state="waiting", version=version)
        for pid in _plan_wait_pids(plan, int(plan["parent_pid"])):
            try:
                _wait_for_process_exit(pid)
            except UpdateError as exc:
                raise UpdateFailure("UPD-1005", str(exc)) from exc
        verify_payload(payload_root)
        _validate_update_baseline(payload_root, root)
        assert_update_runtime_stopped(payload_root, root)
        rollback_root = root / "updates" / f".rollback-{update_id}"
        rollback_root.mkdir(parents=True, exist_ok=False)
        journal = _snapshot_update_targets(
            payload_root,
            root,
            rollback_root,
            update_id=update_id,
            version=version,
        )
        journal_path = root / "updates" / UPDATE_JOURNAL_FILE
        write_json_atomic(journal_path, journal)
        logger.write("backup_created", state="backed_up", version=version)

        def log_install_progress(phase: str, completed: int, total: int) -> None:
            percent = min(100, max(0, round(completed * 100 / total))) if total else 0
            if completed == 1 or completed == total or percent in {
                10, 20, 30, 40, 50, 60, 70, 80, 90
            }:
                logger.write(
                    "install_progress",
                    state=phase,
                    version=version,
                    details={"percent": percent, "completed": completed, "total": total},
                )

        install_payload(
            payload_root,
            root,
            progress_callback=log_install_progress,
            preserve_existing_user_config=True,
        )
        # Manager-only updates must also refresh the standalone uninstall
        # entrypoint.  Otherwise a customer who double-clicks the old copy
        # could still run pre-update deletion rules.
        if Path("manager/WeChatReBot.exe") in payload_relative_paths(payload_root):
            from .installed_entries import ensure_launcher_executable
            from .uninstaller import ensure_uninstall_executable

            if not ensure_launcher_executable(root):
                raise UpdateError("无法刷新启动程序。")
            if not ensure_uninstall_executable(root):
                raise UpdateError("无法刷新卸载程序。")
        write_json_atomic(
            root / INSTALL_MARKER,
            {"schema_version": 1, "state": "ready"},
        )
        journal["state"] = "awaiting_startup"
        write_json_atomic(journal_path, journal)
        flag_path = root / "updates" / f"startup-success-{update_id}.json"
        flag_path.unlink(missing_ok=True)
        logger.write("files_applied", state="awaiting_startup", version=version)
        if restart:
            restarted = _restart_manager(
                target,
                root,
                update_id=update_id,
                current_version=current_version,
                version=version,
            )
            if restarted is None:
                raise UpdateFailure("UPD-1009", "更新后的管理器程序不存在。")
            _wait_for_startup_confirmation(flag_path, update_id, restarted)
        write_json_atomic(
            root / "updates" / UPDATE_RESULT_FILE,
            {
                "schema_version": 1,
                "ok": True,
                "version": version,
                **({"status": "success", "from_version": current_version} if restart else {}),
                "message": "更新已完成。",
            },
        )
        if journal_path is not None:
            journal_path.unlink(missing_ok=True)
        if rollback_root is not None:
            shutil.rmtree(_extended_windows_path(rollback_root), ignore_errors=True)
        flag_path.unlink(missing_ok=True)
        logger.write("apply_succeeded", state="success", version=version)
        result = 0
    except Exception as exc:
        failure = exc
        if logger is not None:
            if isinstance(exc, UpdateFailure):
                code = exc.definition.code
                detail = exc.detail
            elif isinstance(exc, PermissionError):
                code = "UPD-1008"
                detail = str(exc)
            else:
                code = "UPD-1007"
                detail = str(exc)
            logger.write(
                "apply_failed",
                state="rolling_back" if journal is not None else "failed",
                version=version,
                error_code=code,
                message=detail or str(exc),
            )
        if journal is not None and root is not None:
            _stop_restarted_manager(restarted)
            try:
                journal["state"] = "rolling_back"
                if journal_path is not None:
                    write_json_atomic(journal_path, journal)
                _restore_update_targets(journal, root)
                if journal_path is not None:
                    journal_path.unlink(missing_ok=True)
                if rollback_root is not None:
                    shutil.rmtree(_extended_windows_path(rollback_root), ignore_errors=True)
                result = 1
            except UpdateFailure as rollback_error:
                failure = rollback_error
                code = rollback_error.definition.code
                if logger is not None:
                    logger.write(
                        "rollback_failed",
                        state="failed",
                        version=version,
                        error_code=code,
                        message=rollback_error.detail,
                    )
                result = 1
        elif rollback_root is not None:
            shutil.rmtree(_extended_windows_path(rollback_root), ignore_errors=True)
        if root is not None:
            try:
                if isinstance(failure, UpdateFailure):
                    error = failure.to_public_dict()
                elif isinstance(failure, PermissionError):
                    error = UpdateFailure("UPD-1008").to_public_dict()
                else:
                    error = UpdateFailure("UPD-1007").to_public_dict()
                write_json_atomic(
                    root / "updates" / UPDATE_RESULT_FILE,
                    {
                        "schema_version": 1,
                        "ok": False,
                        "status": "failed",
                        "version": version,
                        "from_version": current_version,
                        "error_code": error["code"],
                        "title": error["title"],
                        "message": error["message"],
                    },
                )
            except OSError:
                pass
        if restart and root is not None and target is not None:
            try:
                _restart_manager(target, root)
            except OSError:
                result = 1
    finally:
        if lock is not None:
            lock.release()
    return result


def recover_update_plan(plan_path: str | Path) -> int:
    plan_path = Path(plan_path).expanduser().resolve()
    root: Path | None = None
    logger: UpdateLogger | None = None
    lock: SingleInstanceLock | None = None
    target: Path | None = None
    try:
        plan = _read_json_object(plan_path)
        root = Path(str(plan.get("install_root") or "")).expanduser().resolve()
        updates_root = (root / "updates").resolve()
        plan_path.relative_to(updates_root)
        target = (root / "manager" / "WeChatReBot.exe").resolve()
        update_id = str(plan.get("update_id") or "")
        if (
            plan.get("schema_version") != 1
            or plan.get("action") != "recover-interrupted-update"
            or len(update_id) != 32
            or any(character not in "0123456789abcdef" for character in update_id)
            or Path(str(plan.get("target_executable") or "")).resolve() != target
            or not (root / INSTALL_MARKER).is_file()
        ):
            raise UpdateError("自动恢复计划与当前安装不匹配。")
        wait_pids = _plan_wait_pids(plan, int(plan.get("parent_pid") or 0))
        logger = UpdateLogger(root)
        lock = SingleInstanceLock(f"update-helper:{root}")
        for pid in wait_pids:
            _wait_for_process_exit(pid)
        if not lock.acquire():
            logger.write("recovery_skipped", state="another_helper_active")
            return 2
        journal_path = updates_root / UPDATE_JOURNAL_FILE
        journal = _read_json_object(journal_path)
        if journal.get("update_id") != update_id:
            raise UpdateError("自动恢复记录与更新目录不匹配。")
        rollback_relative = Path(
            *PurePosixPath(str(journal.get("rollback_root") or "")).parts
        )
        rollback_root = (root / rollback_relative).resolve()
        rollback_root.relative_to(updates_root)
        flag_path = updates_root / f"startup-success-{update_id}.json"
        if _startup_confirmed(flag_path, update_id):
            write_json_atomic(
                updates_root / UPDATE_RESULT_FILE,
                {
                    "schema_version": 1,
                    "ok": True,
                    "status": "success",
                    "version": journal.get("version", ""),
                    "message": "更新已完成。",
                },
            )
            logger.write(
                "recovery_confirmed_success",
                state="success",
                version=str(journal.get("version") or ""),
            )
        else:
            try:
                _restore_update_targets(journal, root)
            except UpdateFailure as exc:
                write_json_atomic(
                    updates_root / UPDATE_RESULT_FILE,
                    {
                        "schema_version": 1,
                        "ok": False,
                        "status": "failed",
                        "version": journal.get("version", ""),
                        "error_code": "UPD-1010",
                        "title": UpdateFailure("UPD-1010").definition.title,
                        "message": UpdateFailure("UPD-1010").definition.message,
                    },
                )
                logger.write(
                    "recovery_failed",
                    state="failed",
                    version=str(journal.get("version") or ""),
                    error_code="UPD-1010",
                    message=exc.detail,
                )
                return 1
            failure_code = (
                "UPD-1009"
                if journal.get("state") == "awaiting_startup"
                else "UPD-1007"
            )
            failure = UpdateFailure(failure_code)
            write_json_atomic(
                updates_root / UPDATE_RESULT_FILE,
                {
                    "schema_version": 1,
                    "ok": False,
                    "status": "failed",
                    "version": journal.get("version", ""),
                    "error_code": failure.definition.code,
                    "title": failure.definition.title,
                    "message": failure.definition.message,
                },
            )
            logger.write(
                "recovery_rolled_back",
                state="success",
                version=str(journal.get("version") or ""),
                error_code=failure.definition.code,
            )
        journal_path.unlink(missing_ok=True)
        shutil.rmtree(_extended_windows_path(rollback_root), ignore_errors=True)
        flag_path.unlink(missing_ok=True)
        if target is not None:
            _restart_manager(target, root)
        return 0
    except (OSError, ValueError, UpdateError, UpdateFailure) as exc:
        if root is not None:
            try:
                failure = UpdateFailure("UPD-1010")
                write_json_atomic(
                    root / "updates" / UPDATE_RESULT_FILE,
                    {
                        "schema_version": 1,
                        "ok": False,
                        "status": "failed",
                        "error_code": failure.definition.code,
                        "title": failure.definition.title,
                        "message": failure.definition.message,
                    },
                )
            except OSError:
                pass
        if logger is not None:
            logger.write(
                "recovery_failed",
                state="failed",
                error_code=(
                    exc.definition.code if isinstance(exc, UpdateFailure) else "UPD-1010"
                ),
                message=exc.detail if isinstance(exc, UpdateFailure) else str(exc),
            )
        if root is not None and target is not None:
            try:
                _restart_manager(target, root)
            except OSError:
                pass
        return 1
    finally:
        if lock is not None:
            lock.release()


def recover_interrupted_update(
    install_root: str | Path,
    *,
    executable: str | Path | None = None,
    parent_pid: int | None = None,
) -> bool:
    """Schedule rollback recovery before opening the normal manager window."""
    root = Path(install_root).expanduser().resolve()
    updates_root = (root / "updates").resolve()
    journal_path = updates_root / UPDATE_JOURNAL_FILE
    if not journal_path.is_file():
        return False
    logger = UpdateLogger(root)
    try:
        journal = _read_json_object(journal_path)
        update_id = str(journal.get("update_id") or "")
        if len(update_id) != 32 or any(
            character not in "0123456789abcdef" for character in update_id
        ):
            raise UpdateError("自动恢复记录中的更新编号无效。")
        flag_path = updates_root / f"startup-success-{update_id}.json"
        if _startup_confirmed(flag_path, update_id):
            rollback_relative = Path(
                *PurePosixPath(str(journal.get("rollback_root") or "")).parts
            )
            rollback_root = (root / rollback_relative).resolve()
            rollback_root.relative_to(updates_root)
            write_json_atomic(
                updates_root / UPDATE_RESULT_FILE,
                {
                    "schema_version": 1,
                    "ok": True,
                    "status": "success",
                    "version": journal.get("version", ""),
                    "message": "更新已完成。",
                },
            )
            shutil.rmtree(_extended_windows_path(rollback_root), ignore_errors=True)
            journal_path.unlink(missing_ok=True)
            flag_path.unlink(missing_ok=True)
            logger.write(
                "startup_recovery_cleanup",
                state="success",
                version=str(journal.get("version") or ""),
            )
            return False
        lock = SingleInstanceLock(f"update-helper:{root}")
        if not lock.acquire():
            # An active updater is waiting for this manager to acknowledge its
            # startup. Let the normal window open and emit that handshake.
            return False
        try:
            source = Path(executable or sys.executable).expanduser().resolve()
            if not source.is_file() or source.suffix.casefold() != ".exe":
                raise UpdateError("无法创建独立恢复程序。")
            helper = updates_root / f".recovery-helper-{update_id}.exe"
            shutil.copy2(source, helper)
            pid = int(parent_pid if parent_pid is not None else os.getpid())
            plan_path = updates_root / f".recovery-plan-{update_id}.json"
            write_json_atomic(
                plan_path,
                {
                    "schema_version": 1,
                    "action": "recover-interrupted-update",
                    "update_id": update_id,
                    "install_root": str(root),
                    "target_executable": str(root / "manager" / "WeChatReBot.exe"),
                    "parent_pid": pid,
                    "wait_pids": list(
                        _update_wait_pids(
                            source,
                            pid,
                            include_bootloader=parent_pid is None,
                        )
                    ),
                },
            )
            environment = os.environ.copy()
            environment["WECHATREBOT_UPDATE_HELPER"] = "1"
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
                subprocess, "DETACHED_PROCESS", 0
            )
            subprocess.Popen(
                [str(helper), "apply-update", "--plan", str(plan_path)],
                cwd=root,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
                creationflags=flags,
            )
            logger.write(
                "recovery_helper_started",
                state="interrupted_update",
                version=str(journal.get("version") or ""),
            )
            return True
        finally:
            lock.release()
    except (OSError, ValueError, UpdateError) as exc:
        logger.write(
            "recovery_helper_start_failed",
            state="failed",
            error_code="UPD-1010",
            message=str(exc),
        )
        try:
            write_json_atomic(
                updates_root / UPDATE_RESULT_FILE,
                {
                    "schema_version": 1,
                    "ok": False,
                    "status": "failed",
                    "version": journal.get("version", "") if "journal" in locals() else "",
                    "error_code": "UPD-1010",
                    "title": UpdateFailure("UPD-1010").definition.title,
                    "message": UpdateFailure("UPD-1010").definition.message,
                },
            )
        except OSError:
            pass
        return False


def cleanup_stale_update_workdirs(
    install_root: str | Path,
    *,
    minimum_age_seconds: int = 3600,
) -> int:
    updates = Path(install_root).expanduser().resolve() / "updates"
    try:
        candidates = tuple(updates.glob(".pending-*"))
    except OSError:
        return 0
    cutoff = time.time() - max(60, minimum_age_seconds)
    removed = 0
    for candidate in candidates:
        try:
            if candidate.is_symlink() or (
                hasattr(candidate, "is_junction") and candidate.is_junction()
            ):
                continue
            if not candidate.is_dir() or candidate.stat().st_mtime > cutoff:
                continue
            candidate.resolve().relative_to(updates.resolve())
            shutil.rmtree(_extended_windows_path(candidate))
            removed += 1
        except (OSError, ValueError):
            continue
    return removed


def consume_update_result(install_root: str | Path) -> dict[str, object] | None:
    target = (
        Path(install_root).expanduser().resolve()
        / "updates"
        / UPDATE_RESULT_FILE
    )
    try:
        payload = json.loads(target.read_text(encoding="utf-8-sig"))
        target.unlink()
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        return None
    if payload.get("status") not in {"success", "failed"}:
        payload["status"] = "success" if payload.get("ok") is True else "failed"
    return payload


__all__ = [
    "PreparedUpdate",
    "PRODUCT_ID",
    "UpdateChannel",
    "UpdateError",
    "MandatoryUpdateUnavailable",
    "UpdatePlan",
    "UpdateRelease",
    "apply_update_plan",
    "assert_update_runtime_stopped",
    "canonical_manifest_bytes",
    "check_for_update",
    "cleanup_stale_update_workdirs",
    "consume_update_result",
    "create_update_plan",
    "extract_update_archive",
    "launch_update_helper",
    "load_update_channel",
    "parse_update_manifest",
    "prepare_update",
    "recover_interrupted_update",
    "recover_update_plan",
]
