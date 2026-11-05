"""Build helpers for manager-only delta releases. Publishing lives in publish_github_release.py."""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tomllib
import urllib.request
import zipfile

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version

COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = COMMERCIAL_ROOT / "scripts"
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
for search_root in (COMMERCIAL_ROOT, SCRIPTS_ROOT, SOURCE_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

from wechatrebot_manager.payload import LEGACY_READY_MANIFEST_STATUS, sha256_file
from wechatrebot_manager.updates import (
    PRODUCT_ID,
    UpdateChannel,
    canonical_manifest_bytes,
    fresh_stable_manifest_url,
    parse_update_manifest,
)


ONEFILE_SAFE_UPDATER_VERSION = Version("0.8.7")


@dataclass(frozen=True, slots=True)
class BuiltManagerUpdate:
    version: str
    archive_path: Path
    manifest_path: Path
    package_url: str
    package_size: int
    package_sha256: str


def _project_version(root: Path = COMMERCIAL_ROOT) -> str:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(Version(str(payload["project"]["version"])))


def _require_safe_delta_base(live_version: str) -> None:
    if Version(live_version) < ONEFILE_SAFE_UPDATER_VERSION:
        raise ValueError(
            f"线上稳定版 {live_version} 仍使用旧单文件更新器，不能发布管理器增量包；"
            "请先通过完整载荷发布 0.8.7。"
        )


def _load_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"JSON 根节点不是对象：{path}")
    return payload


def _signing_key(path: Path, channel: UpdateChannel) -> Ed25519PrivateKey:
    key = serialization.load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, Ed25519PrivateKey):
        raise ValueError("更新签名私钥必须是 Ed25519")
    public_bytes = key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    key_id = hashlib.sha256(public_bytes).hexdigest().upper()[:16]
    if key_id != channel.key_id or public_bytes != channel.public_key:
        raise ValueError("签名私钥与客户端内置更新公钥不匹配")
    return key


def _validated_manager(manager_build: Path, version: str) -> Path:
    manifest_path = manager_build / "manager-build-manifest.json"
    manifest = _load_json(manifest_path)
    executable = manager_build / "dist" / "WeChatReBot.exe"
    self_test = manifest.get("self_test")
    if not isinstance(self_test, dict):
        raise ValueError("管理器构建清单缺少自检结果")
    expected_hash = str(manifest.get("sha256") or "").upper()
    if (
        manifest.get("schema_version") != 1
        or manifest.get("manager_version") != version
        or manifest.get("filename") != executable.name
        or not executable.is_file()
        or int(manifest.get("size") or 0) != executable.stat().st_size
        or expected_hash != sha256_file(executable)
        or self_test.get("ok") is not True
        or self_test.get("frozen") is not True
        or self_test.get("architecture") != 64
        or self_test.get("manager_version") != version
    ):
        raise ValueError("管理器构建清单、自检结果或 EXE 摘要不一致")
    return executable


def build_manager_update(
    *,
    manager_build: Path,
    output: Path,
    version: str,
    private_key_path: Path,
    channel: UpdateChannel,
    asset_prefix: str,
    title: str,
    notes: tuple[str, ...],
    minimum_supported_version: str,
    build: int | None = None,
    mandatory: bool = False,
) -> BuiltManagerUpdate:
    executable = _validated_manager(manager_build.resolve(), version)
    output = output.resolve()
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"更新输出目录不是空目录：{output}")
    output.mkdir(parents=True, exist_ok=True)

    payload_manifest = {
        "schema_version": 1,
        "source_manifest_status": LEGACY_READY_MANIFEST_STATUS,
        "release_version": version,
        "files": [
            {
                "path": "manager/WeChatReBot.exe",
                "sha256": sha256_file(executable),
            }
        ],
    }
    if build is not None:
        if build < 0:
            raise ValueError("构建号不能小于零")
        payload_manifest["release_build"] = build
    temporary_archive = output / "manager-update.zip"
    with zipfile.ZipFile(
        temporary_archive,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=6,
        allowZip64=True,
    ) as package:
        package.writestr(
            "payload/release-manifest.json",
            json.dumps(payload_manifest, ensure_ascii=False, indent=2) + "\n",
        )
        package.write(executable, "payload/manager/WeChatReBot.exe")

    package_sha256 = sha256_file(temporary_archive)
    archive_name = (
        f"wechatrebot-{version}-manager-{package_sha256[:12].lower()}.zip"
    )
    archive_path = output / archive_name
    temporary_archive.replace(archive_path)
    package_url = f"{asset_prefix.rstrip('/')}/{archive_name}"
    with zipfile.ZipFile(archive_path, "r") as package:
        uncompressed_size = sum(item.file_size for item in package.infolist())
    key = _signing_key(private_key_path.resolve(), channel)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "product": PRODUCT_ID,
        "channel": channel.channel,
        "version": version,
        "minimum_supported_version": str(Version(minimum_supported_version)),
        "published_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "title": title.strip()[:100] or f"版本 {version}",
        "notes": [item.strip()[:500] for item in notes if item.strip()][:20],
        "mandatory": mandatory,
        "package": {
            "url": package_url,
            "format": "zip",
            "size": archive_path.stat().st_size,
            "sha256": package_sha256,
            "payload_file_count": 1,
            "uncompressed_size": uncompressed_size,
        },
    }
    if build is not None:
        manifest["build"] = build
    signature = key.sign(canonical_manifest_bytes(manifest))
    manifest["signature"] = {
        "algorithm": "ed25519",
        "key_id": channel.key_id,
        "value": base64.b64encode(signature).decode("ascii"),
    }
    manifest_path = output / "stable.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    parsed = parse_update_manifest(
        manifest,
        channel,
        current_version="0.0.0",
    )
    if parsed is None or parsed.version != version:
        raise ValueError("生成后的签名更新清单无法通过客户端校验")
    return BuiltManagerUpdate(
        version=version,
        archive_path=archive_path,
        manifest_path=manifest_path,
        package_url=package_url,
        package_size=archive_path.stat().st_size,
        package_sha256=package_sha256,
    )


def _run(argv: list[str], *, root: Path = COMMERCIAL_ROOT, env=None) -> None:
    print("+", subprocess.list2cmdline(argv), flush=True)
    subprocess.run(argv, cwd=root, env=env, check=True)


def _fetch_live_manifest(channel: UpdateChannel) -> dict[str, object]:
    request = urllib.request.Request(
        fresh_stable_manifest_url(channel.manifest_url),
        headers={
            "Accept": "application/json",
            "Cache-Control": "no-cache",
            "User-Agent": "WeChatReBot-Publisher/1",
        },
    )
    with urllib.request.urlopen(
        request, timeout=channel.request_timeout_seconds
    ) as response:
        content = response.read(1024 * 1024)
    payload = json.loads(content.decode("utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError("线上更新清单根节点不是对象")
    parse_update_manifest(payload, channel, current_version="0.0.0")
    return payload


def _prompt_publish(version: str, package_size: int) -> bool:
    size_mib = package_size / 1024**2
    answer = input(
        f"准备发布 {version}（{size_mib:.2f} MiB）到稳定通道，输入 {version} 确认："
    ).strip()
    return answer == version
