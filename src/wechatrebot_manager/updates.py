from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
from importlib import resources
import json
import os
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

from .customer_logs import sanitize_log_text
from .install_paths import INSTALL_MARKER
from .json_io import write_json_atomic
from .payload import PayloadError, install_payload, sha256_file, verify_payload


PRODUCT_ID = "wechatrebot-desktop"
UPDATE_RESULT_FILE = "update-result.json"
MAX_MANIFEST_BYTES = 1024 * 1024
MAX_ARCHIVE_FILES = 200_000
MAX_UNCOMPRESSED_BYTES = 8 * 1024**3
ProgressCallback = Callable[[str, int, int], None]


class UpdateError(RuntimeError):
    """Raised when remote update metadata or package data cannot be trusted."""


@dataclass(frozen=True, slots=True)
class UpdateChannel:
    manifest_url: str
    channel: str
    key_id: str
    public_key: bytes
    check_interval_seconds: int = 21_600
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
    check_interval = payload.get("check_interval_seconds", 21_600)
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
        minimum = Version(str(payload.get("minimum_supported_version") or "0.0.0"))
    except (KeyError, TypeError, ValueError, InvalidVersion) as exc:
        raise UpdateError("更新版本格式错误。") from exc
    if not latest_text or str(latest) != latest_text:
        raise UpdateError("更新版本必须使用规范版本号。")
    if latest <= current:
        return None

    package = payload.get("package")
    if not isinstance(package, dict):
        raise UpdateError("更新清单缺少安装包信息。")
    if package.get("format") not in {None, "zip"}:
        raise UpdateError("更新包格式不受支持。")
    try:
        package_size = int(package["size"])
        package_sha256 = str(package["sha256"]).upper()
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("更新安装包摘要格式错误。") from exc
    if not (0 < package_size <= channel.max_package_bytes):
        raise UpdateError("更新包大小超出允许范围。")
    if len(package_sha256) != 64 or any(
        character not in "0123456789ABCDEF" for character in package_sha256
    ):
        raise UpdateError("更新包 SHA-256 格式错误。")
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
        mandatory=current < minimum,
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
    return parse_update_manifest(payload, channel, current_version=current_version)


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
            for index, item in enumerate(items, start=1):
                relative = _safe_archive_path(item.filename)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode):
                    raise UpdateError("更新包不能包含符号链接。")
                target = (root / relative).resolve()
                try:
                    target.relative_to(root)
                except ValueError as exc:
                    raise UpdateError("更新包文件越过暂存目录。") from exc
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
    payload_root = root / "payload"
    if not payload_root.is_dir():
        raise UpdateError("更新包缺少 payload 目录。")
    return payload_root


def prepare_update(
    release: UpdateRelease,
    channel: UpdateChannel,
    install_root: str | Path,
    *,
    progress_callback: ProgressCallback | None = None,
) -> PreparedUpdate:
    root = Path(install_root).expanduser().resolve()
    updates_root = (root / "updates").resolve()
    updates_root.mkdir(parents=True, exist_ok=True)
    try:
        updates_root.relative_to(root)
    except ValueError as exc:
        raise UpdateError("更新暂存目录无效。") from exc
    working = updates_root / f".pending-{release.version}-{uuid.uuid4().hex}"
    working.mkdir()
    archive = working / "update.zip.download"
    request = urllib.request.Request(
        release.package_url,
        headers={"Accept": "application/zip", "User-Agent": f"WeChatReBot/{release.version}"},
    )
    try:
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
            raise UpdateError("更新包 SHA-256 校验失败。")
        final_archive = working / "update.zip"
        os.replace(archive, final_archive)
        payload_root = extract_update_archive(
            final_archive,
            working / "extracted",
            progress_callback=progress_callback,
        )
        verify_payload(
            payload_root,
            allow_incomplete=False,
            progress_callback=progress_callback,
        )
        manager = payload_root / "manager" / "WeChatReBot.exe"
        if not manager.is_file():
            raise UpdateError("更新包缺少桌面管理器。")
        return PreparedUpdate(release, working, payload_root, final_archive)
    except Exception:
        shutil.rmtree(working, ignore_errors=True)
        raise


def create_update_plan(
    prepared: PreparedUpdate,
    install_root: str | Path,
    *,
    executable: str | Path | None = None,
    parent_pid: int | None = None,
) -> UpdatePlan:
    root = Path(install_root).expanduser().resolve()
    updates_root = (root / "updates").resolve()
    working = prepared.working_root.resolve()
    try:
        working.relative_to(updates_root)
        prepared.payload_root.resolve().relative_to(working)
    except ValueError as exc:
        raise UpdateError("更新计划引用了暂存目录之外的文件。") from exc
    source_executable = Path(executable or sys.executable).expanduser().resolve()
    if not source_executable.is_file() or source_executable.suffix.casefold() != ".exe":
        raise UpdateError("无法创建独立更新程序。")
    helper = working / "WeChatReBot-Updater.exe"
    shutil.copy2(source_executable, helper)
    plan_path = working / "update-plan.json"
    write_json_atomic(
        plan_path,
        {
            "schema_version": 1,
            "version": prepared.release.version,
            "install_root": str(root),
            "payload_root": str(prepared.payload_root.resolve()),
            "target_executable": str(root / "manager" / "WeChatReBot.exe"),
            "parent_pid": int(parent_pid if parent_pid is not None else os.getpid()),
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


def _validated_plan(plan_path: str | Path) -> tuple[dict[str, object], Path, Path, Path]:
    path = Path(plan_path).expanduser().resolve()
    payload = _read_json_object(path)
    try:
        root = Path(str(payload["install_root"])).expanduser().resolve()
        payload_root = Path(str(payload["payload_root"])).expanduser().resolve()
        target = Path(str(payload["target_executable"])).expanduser().resolve()
        parent_pid = int(payload["parent_pid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise UpdateError("更新计划缺少必要字段。") from exc
    updates_root = (root / "updates").resolve()
    expected_target = (root / "manager" / "WeChatReBot.exe").resolve()
    try:
        path.relative_to(updates_root)
        payload_root.relative_to(path.parent)
    except ValueError as exc:
        raise UpdateError("更新计划路径越过安装目录。") from exc
    if (
        payload.get("schema_version") != 1
        or target != expected_target
        or parent_pid < 0
        or not (root / INSTALL_MARKER).is_file()
    ):
        raise UpdateError("更新计划与当前安装不匹配。")
    return payload, root, payload_root, target


def _restart_manager(target: Path, install_root: Path) -> None:
    if not target.is_file():
        return
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.Popen(
        [str(target)],
        cwd=install_root,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=flags,
    )


def apply_update_plan(
    plan_path: str | Path,
    *,
    restart: bool = True,
) -> int:
    root: Path | None = None
    target: Path | None = None
    version = ""
    try:
        plan, root, payload_root, target = _validated_plan(plan_path)
        version = str(plan.get("version") or "")
        _wait_for_process_exit(int(plan["parent_pid"]))
        install_payload(payload_root, root, allow_incomplete=False)
        write_json_atomic(
            root / INSTALL_MARKER,
            {"schema_version": 1, "state": "ready"},
        )
        write_json_atomic(
            root / "updates" / UPDATE_RESULT_FILE,
            {
                "schema_version": 1,
                "ok": True,
                "version": version,
                "message": "更新已完成。",
            },
        )
        result = 0
    except Exception as exc:
        result = 1
        if root is not None:
            try:
                write_json_atomic(
                    root / "updates" / UPDATE_RESULT_FILE,
                    {
                        "schema_version": 1,
                        "ok": False,
                        "version": version,
                        "message": sanitize_log_text(str(exc))[:1000],
                    },
                )
            except OSError:
                pass
    if restart and root is not None and target is not None:
        try:
            _restart_manager(target, root)
        except OSError:
            result = 1
    return result


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
            shutil.rmtree(candidate)
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
    return payload


__all__ = [
    "PreparedUpdate",
    "PRODUCT_ID",
    "UpdateChannel",
    "UpdateError",
    "UpdatePlan",
    "UpdateRelease",
    "apply_update_plan",
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
]
