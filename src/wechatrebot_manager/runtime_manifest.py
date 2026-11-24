"""Signed, complete runtime inventories independent of application versions."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path

from .payload import PayloadError, _checked_target, _safe_relative_path, _valid_digest
from .update_manifest import UpdateChannel, UpdateError, _manifest_package_fields, _verify_manifest_signature


ENVIRONMENT_MANIFEST = Path("legal/runtime-environment.json")
ENVIRONMENT_RECEIPT = Path("runtime/environment-receipt.json")
ENVIRONMENT_JOURNAL = Path("updates/environment-transaction.json")
ENGINE_PATH = Path("app/engine/python")
MAX_ENVIRONMENT_MANIFEST = 32 * 1024**2
MAX_ENVIRONMENT_FILES = 100_000
MAX_ENVIRONMENT_PACKAGES = 900
REQUIRED_RUNTIME_FILES = frozenset({
    "runtime/python/python.exe", "runtime/ffmpeg/bin/ffmpeg.exe", "runtime/ffmpeg/bin/ffprobe.exe",
})


def runtime_path(value: str | Path) -> Path:
    relative = _safe_relative_path(value.as_posix() if isinstance(value, Path) else value)
    parts = tuple(part.casefold() for part in relative.parts)
    if len(parts) < 3 or parts[:2] not in {("runtime", "python"), ("runtime", "ffmpeg")}:
        raise PayloadError("运行环境文件必须位于受管的 Python 或 FFmpeg 目录。")
    if parts[:4] == ("runtime", "python", "lib", "site-packages") and len(parts) > 4:
        name = parts[4]
        if name in {"astrbot", "wechatrebot_manager"} or name.startswith("astrbot-"):
            raise PayloadError("引擎和共享授权代码必须随应用交付。")
    return relative


def runtime_file(value: str | Path) -> bool:
    try:
        runtime_path(value)
        return True
    except PayloadError:
        return False


@dataclass(frozen=True)
class RuntimeFile:
    path: str
    sha256: str
    size: int

    def as_dict(self) -> dict:
        return {"path": self.path, "sha256": self.sha256, "size": self.size}


def content_id(files: tuple[RuntimeFile, ...]) -> str:
    items = sorted((item.as_dict() for item in files), key=lambda item: item["path"].casefold())
    return hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest().upper()


@dataclass(frozen=True)
class RuntimePackage:
    identity: str
    url: str
    size: int
    sha256: str
    uncompressed_size: int
    files: tuple[RuntimeFile, ...]


@dataclass(frozen=True)
class RuntimeEnvironment:
    identity: str
    python_abi: str
    packages: tuple[RuntimePackage, ...]
    manifest: dict

    @property
    def files(self) -> tuple[RuntimeFile, ...]:
        return tuple(item for package in self.packages for item in package.files)


def parse_environment(payload: object, channel: UpdateChannel) -> RuntimeEnvironment:
    if not isinstance(payload, dict):
        raise UpdateError("运行环境清单格式错误。")
    _verify_manifest_signature(payload, channel)
    if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
            or payload.get("product") != "wechatrebot-runtime" or payload.get("platform") != "windows-x64"):
        raise UpdateError("运行环境协议或系统架构不受支持。")
    abi = payload.get("python_abi")
    if not isinstance(abi, str) or not abi.startswith("cp3") or not abi[2:].isdigit():
        raise UpdateError("运行环境 Python 版本无效。")
    values = payload.get("packages")
    if not isinstance(values, list) or not 1 <= len(values) <= MAX_ENVIRONMENT_PACKAGES:
        raise UpdateError("运行环境下载包列表无效。")
    packages, seen, identities = [], set(), set()
    for value in values:
        if not isinstance(value, dict) or not isinstance(value.get("files"), list) or not value["files"]:
            raise UpdateError("运行环境文件列表无效。")
        fields = _manifest_package_fields(value.get("package"), channel)
        files = []
        for item in value["files"]:
            if not isinstance(item, dict):
                raise UpdateError("运行环境文件条目无效。")
            path = runtime_path(item.get("path")).as_posix()
            key = path.casefold()
            if (key in seen or not _valid_digest(item.get("sha256"))
                    or type(item.get("size")) is not int or not 0 <= item["size"] <= 8 * 1024**3):
                raise UpdateError("运行环境文件路径、大小或摘要无效。")
            seen.add(key)
            files.append(RuntimeFile(path, item["sha256"].upper(), item["size"]))
            if len(seen) > MAX_ENVIRONMENT_FILES:
                raise UpdateError("运行环境文件数量超限。")
        files = tuple(files)
        identity = content_id(files)
        if value.get("id") != identity or identity in identities:
            raise UpdateError("运行环境下载包指纹无效或重复。")
        identities.add(identity)
        packages.append(RuntimePackage(identity, fields["package_url"], fields["package_size"],
                                       fields["package_sha256"], fields["uncompressed_size"], files))
    if not REQUIRED_RUNTIME_FILES.issubset(seen):
        raise UpdateError("运行环境缺少 Python 或音视频工具。")
    if any("/".join(key.split("/")[:i]) in seen for key in seen for i in range(1, len(key.split("/")))):
        raise UpdateError("运行环境路径存在文件与目录冲突。")
    files = tuple(item for package in packages for item in package.files)
    identity = environment_id(abi, files)
    if payload.get("id") != identity or sum(item.size for item in files) > 8 * 1024**3:
        raise UpdateError("运行环境整体指纹或大小无效。")
    return RuntimeEnvironment(identity, abi, tuple(packages), payload)


def environment_id(abi: str, files: tuple[RuntimeFile, ...]) -> str:
    return hashlib.sha256(f"windows-x64:{abi}:{content_id(files)}".encode()).hexdigest().upper()


def load_environment(root: Path, channel: UpdateChannel) -> RuntimeEnvironment | None:
    path = _checked_target(root, ENVIRONMENT_MANIFEST)
    if not path.exists():
        return None  # Existing releases keep their original startup/update behavior.
    if path.stat().st_size > MAX_ENVIRONMENT_MANIFEST:
        raise UpdateError("运行环境清单过大。")
    try:
        return parse_environment(json.loads(path.read_text(encoding="utf-8")), channel)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdateError("运行环境清单无法读取。") from exc
