from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from collections.abc import Callable

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from packaging.version import Version

COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = COMMERCIAL_ROOT / "scripts"
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
for search_root in (SCRIPTS_ROOT, SOURCE_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

import oss_release_storage as oss_storage
from wechatrebot_manager.payload import LEGACY_READY_MANIFEST_STATUS, sha256_file
from wechatrebot_manager.updates import (
    PRODUCT_ID,
    UpdateChannel,
    UpdateRelease,
    canonical_manifest_bytes,
    load_update_channel,
    parse_update_manifest,
    prepare_update,
)


DEFAULT_HOST = "root@ss.xiuxianjyj.xin"
DEFAULT_REMOTE_ROOT = "/srv/wechatrebot-updates"
DEFAULT_BASE_URL = "https://ss.xiuxianjyj.xin/wechatrebot-updates"
ONEFILE_SAFE_UPDATER_VERSION = Version("0.8.7")
MAX_DOWNLOAD_METADATA_BYTES = 256 * 1024


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


def _fetch_download_version(base_url: str, *, timeout: int = 20) -> str:
    url = f"{base_url.rstrip('/')}/download.json"
    parsed = urllib.parse.urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("下载页版本地址必须是无账号信息的 HTTPS 地址")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    last_error: BaseException | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(MAX_DOWNLOAD_METADATA_BYTES + 1)
            break
        except (OSError, urllib.error.URLError) as exc:
            last_error = exc
            if attempt == 2:
                raise ValueError("暂时无法读取下载页版本信息") from exc
            time.sleep(0.5 * (attempt + 1))
    else:  # pragma: no cover - loop always returns or raises
        raise ValueError("暂时无法读取下载页版本信息") from last_error
    if len(body) > MAX_DOWNLOAD_METADATA_BYTES:
        raise ValueError("下载页版本信息过大")
    try:
        payload = json.loads(body.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            raise ValueError
        return str(Version(str(payload["version"])))
    except (KeyError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("下载页版本信息无效") from exc


def _published_version_floor(stable_version: str, download_version: str) -> str:
    return str(max(Version(stable_version), Version(download_version)))


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
    base_url: str,
    title: str,
    notes: tuple[str, ...],
    minimum_supported_version: str,
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
    package_url = f"{base_url.rstrip('/')}/releases/{archive_name}"
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
        "package": {
            "url": package_url,
            "format": "zip",
            "size": archive_path.stat().st_size,
            "sha256": package_sha256,
            "payload_file_count": 1,
        },
    }
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
        channel.manifest_url,
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


def _public_package_head(release: BuiltManagerUpdate, timeout: int) -> None:
    request = urllib.request.Request(
        release.package_url,
        method="HEAD",
        headers={"Cache-Control": "no-cache", "User-Agent": "WeChatReBot-Publisher/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.geturl() != release.package_url:
            raise ValueError("公网更新包地址发生重定向")
        length = response.headers.get("Content-Length")
        if response.status != 200 or int(length or 0) != release.package_size:
            raise ValueError("公网更新包状态或大小与发布清单不一致")


def _safe_remote_inputs(host: str, remote_root: str) -> None:
    allowed_host = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-@")
    allowed_path = allowed_host | {"/"}
    if not host or any(character not in allowed_host for character in host):
        raise ValueError("SSH 主机参数包含不安全字符")
    if (
        not remote_root.startswith("/")
        or remote_root == "/"
        or any(character not in allowed_path for character in remote_root)
    ):
        raise ValueError("服务器发布目录必须是明确的绝对路径")


def _ssh(host: str, command: str) -> None:
    _run(["ssh.exe", "-o", "BatchMode=yes", host, command])


def _scp(local_path: Path, host: str, remote_path: str) -> None:
    _run(["scp.exe", "-p", str(local_path), f"{host}:{remote_path}"])


def _publish(
    release: BuiltManagerUpdate,
    *,
    channel: UpdateChannel,
    previous_version: str,
    host: str,
    remote_root: str,
    smoke_root: Path,
    skip_remote_smoke: bool,
    after_switch: Callable[[], None] | None = None,
    upload_archive_to_server: bool = True,
) -> None:
    _safe_remote_inputs(host, remote_root)
    nonce = uuid.uuid4().hex
    releases_root = f"{remote_root}/releases"
    manifests_root = f"{remote_root}/manifests"
    remote_archive = f"{releases_root}/{release.archive_path.name}"
    archive_upload = f"{remote_archive}.upload-{nonce}"
    manifest_upload = f"{remote_root}/stable.json.upload-{nonce}"
    previous_manifest = f"{manifests_root}/stable-{previous_version}.json"
    rollback_manifest = f"{manifests_root}/.previous-{nonce}.json"
    current_manifest_archive = f"{manifests_root}/stable-{release.version}.json"

    quoted_root = shlex.quote(remote_root)
    quoted_releases = shlex.quote(releases_root)
    quoted_manifests = shlex.quote(manifests_root)
    _ssh(
        host,
        f"set -eu; mkdir -p {quoted_releases} {quoted_manifests}; "
        f"test \"$(readlink -f {quoted_root})\" = {quoted_root}",
    )
    if upload_archive_to_server:
        archive_candidate = archive_upload
        archive_move = (
            f"mv -f -- {shlex.quote(archive_upload)} {shlex.quote(remote_archive)}"
        )
        try:
            _ssh(
                host,
                "set -eu; "
                f"test \"$(stat -c%s {shlex.quote(remote_archive)})\" -eq {release.package_size}; "
                f"printf '%s  %s\\n' {release.package_sha256} {shlex.quote(remote_archive)} "
                "| sha256sum -c -",
            )
        except subprocess.CalledProcessError:
            _scp(release.archive_path, host, archive_upload)
        else:
            archive_candidate = remote_archive
            archive_move = ":"
            print("服务器已存在摘要一致的更新包，跳过重复上传。", flush=True)
        _ssh(
            host,
            "set -eu; "
            f"test \"$(stat -c%s {shlex.quote(archive_candidate)})\" -eq {release.package_size}; "
            f"printf '%s  %s\\n' {release.package_sha256} {shlex.quote(archive_candidate)} "
            "| sha256sum -c -; "
            f"{archive_move}",
        )
    else:
        print("更新包使用 OSS/CDN，服务器仅发布签名清单。", flush=True)
    _public_package_head(release, channel.request_timeout_seconds)

    _scp(release.manifest_path, host, manifest_upload)
    manifest_hash = sha256_file(release.manifest_path)
    manifest_size = release.manifest_path.stat().st_size
    stable = f"{remote_root}/stable.json"
    _ssh(
        host,
        "set -eu; "
        f"test \"$(stat -c%s {shlex.quote(manifest_upload)})\" -eq {manifest_size}; "
        f"printf '%s  %s\\n' {manifest_hash} {shlex.quote(manifest_upload)} "
        "| sha256sum -c -; "
        f"cp -f -- {shlex.quote(stable)} {shlex.quote(rollback_manifest)}; "
        f"cp -f -- {shlex.quote(stable)} {shlex.quote(previous_manifest)}; "
        f"mv -f -- {shlex.quote(manifest_upload)} {shlex.quote(stable)}; "
        f"cp -f -- {shlex.quote(stable)} {shlex.quote(current_manifest_archive)}",
    )

    try:
        live = _fetch_live_manifest(channel)
        parsed = parse_update_manifest(
            live,
            channel,
            current_version=previous_version,
        )
        if (
            parsed is None
            or parsed.version != release.version
            or parsed.package_sha256 != release.package_sha256
            or parsed.package_size != release.package_size
            or parsed.package_url != release.package_url
        ):
            raise ValueError("线上稳定清单与本地发布产物不一致")
        if not skip_remote_smoke:
            prepared = prepare_update(parsed, channel, smoke_root)
            if sha256_file(prepared.archive_path) != release.package_sha256:
                raise ValueError("线上下载后的更新包摘要不一致")
            manager = prepared.payload_root / "manager" / "WeChatReBot.exe"
            if not manager.is_file():
                raise ValueError("线上更新包缺少管理器")
        if after_switch is not None:
            after_switch()
    except Exception:
        rollback_upload = f"{remote_root}/stable.json.rollback-{nonce}"
        _ssh(
            host,
            "set -eu; "
            f"cp -f -- {shlex.quote(rollback_manifest)} {shlex.quote(rollback_upload)}; "
            f"mv -f -- {shlex.quote(rollback_upload)} {shlex.quote(stable)}",
        )
        raise


def _prompt_publish(version: str, package_size: int) -> bool:
    size_mib = package_size / 1024**2
    answer = input(
        f"准备发布 {version}（{size_mib:.2f} MiB）到稳定通道，输入 {version} 确认："
    ).strip()
    return answer == version


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="一键测试、构建、签名并发布管理器增量更新。"
    )
    parser.add_argument("--title", default="插件与管理器更新")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--minimum-supported-version", default="0.7.0")
    parser.add_argument(
        "--private-key",
        type=Path,
        default=COMMERCIAL_ROOT / "config" / "local" / "update-signing-private.pem",
    )
    parser.add_argument("--manager-build", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--remote-root", default=DEFAULT_REMOTE_ROOT)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--artifact-storage",
        choices=("server", "oss"),
        help="大文件存储后端；默认读取 release/oss-publish.json 的 enabled 设置。",
    )
    parser.add_argument("--asset-base-url", help="覆盖安装包的 OSS/CDN 公网地址。")
    parser.add_argument(
        "--oss-config",
        type=Path,
        default=oss_storage.DEFAULT_CONFIG_PATH,
    )
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-remote-smoke", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)

    if sys.version_info[:2] != (3, 13) or platform.architecture()[0] != "64bit":
        parser.error("必须使用 Python 3.13 x64 执行发布脚本")
    if not args.dry_run and (
        not shutil.which("ssh.exe") or not shutil.which("scp.exe")
    ):
        parser.error("找不到 Windows OpenSSH 的 ssh.exe 或 scp.exe")

    version = _project_version()
    channel = load_update_channel()
    oss_config = oss_storage.load_oss_release_storage(args.oss_config)
    artifact_storage = args.artifact_storage or (
        "oss" if oss_config.enabled else "server"
    )
    asset_base_url = (
        args.asset_base_url
        or (oss_config.public_base_url if artifact_storage == "oss" else args.base_url)
    ).rstrip("/")
    live_manifest = _fetch_live_manifest(channel)
    live_version = str(Version(str(live_manifest["version"])))
    download_version = _fetch_download_version(args.base_url)
    published_floor = _published_version_floor(live_version, download_version)
    try:
        _require_safe_delta_base(live_version)
    except ValueError as exc:
        parser.error(str(exc))
    if Version(version) < Version(published_floor) or (
        Version(version) == Version(published_floor) and not args.dry_run
    ):
        parser.error(
            f"项目版本 {version} 必须高于线上已发布版本 {published_floor}"
            f"（更新通道 {live_version}，下载页 {download_version}）；请先提升版本号"
        )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (args.output or COMMERCIAL_ROOT / "artifacts" / f"publish-{version}-{timestamp}").resolve()
    if output.exists() and any(output.iterdir()):
        parser.error(f"输出目录不是空目录：{output}")
    output.mkdir(parents=True, exist_ok=True)

    if not args.skip_tests:
        test_env = os.environ.copy()
        test_env["PYTHONPATH"] = str(SOURCE_ROOT)
        _run([sys.executable, "-m", "pytest", "-q"], env=test_env)

    if args.manager_build:
        manager_build = args.manager_build.expanduser().resolve()
    else:
        manager_build = output / "manager-build"
        _run(
            [
                sys.executable,
                str(COMMERCIAL_ROOT / "scripts" / "build_manager.py"),
                "--output",
                str(manager_build),
                "--host-python",
                sys.executable,
            ]
        )

    release = build_manager_update(
        manager_build=manager_build,
        output=output / "update",
        version=version,
        private_key_path=args.private_key.expanduser(),
        channel=channel,
        base_url=asset_base_url,
        title=args.title,
        notes=tuple(args.note),
        minimum_supported_version=args.minimum_supported_version,
    )
    summary = {
        "version": release.version,
        "archive": str(release.archive_path),
        "package_url": release.package_url,
        "size": release.package_size,
        "sha256": release.package_sha256,
        "manifest": str(release.manifest_path),
        "artifact_storage": artifact_storage,
        "oss_upload_enabled": artifact_storage == "oss" and not args.dry_run,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        print("本地构建完成；--dry-run 未上传服务器。")
        return 0
    if not args.yes and not _prompt_publish(version, release.package_size):
        print("未确认版本号，已取消上传。")
        return 2

    if artifact_storage == "oss":
        oss_storage.upload_release_objects(
            oss_config,
            (
                oss_storage.OssReleaseObject(
                    release.archive_path,
                    "releases",
                    release.package_sha256,
                    "application/zip",
                    "public,max-age=3600",
                ),
            ),
        )

    _publish(
        release,
        channel=channel,
        previous_version=live_version,
        host=args.host,
        remote_root=args.remote_root.rstrip("/"),
        smoke_root=output / "remote-smoke",
        skip_remote_smoke=args.skip_remote_smoke,
        upload_archive_to_server=artifact_storage == "server",
    )
    print(
        json.dumps(
            {
                **summary,
                "published": True,
                "manifest_url": channel.manifest_url,
                "previous_version": live_version,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
