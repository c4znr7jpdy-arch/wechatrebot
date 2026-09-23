from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tomllib
import urllib.error
import urllib.request
import uuid
import zipfile

from packaging.version import Version


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = COMMERCIAL_ROOT / "scripts"
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
for search_root in (SCRIPTS_ROOT, SOURCE_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

import publish_manager_update as publisher
import oss_release_storage as oss_storage
import build_native_installer
from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.updates import (
    UpdateChannel,
    load_update_channel,
    parse_update_manifest,
)


@dataclass(frozen=True, slots=True)
class BuiltFullUpdate:
    version: str
    archive_path: Path
    manifest_path: Path
    package_url: str
    package_size: int
    package_sha256: str


@dataclass(frozen=True, slots=True)
class BuiltCustomerPackage:
    version: str
    archive_path: Path
    checksum_path: Path
    package_url: str
    checksum_url: str
    package_size: int
    package_sha256: str
    installer: dict[str, object] | None = None
    payload_path: Path | None = None
    payload_url: str | None = None


@dataclass(frozen=True, slots=True)
class StagedDownloadSite:
    metadata_upload: str
    rollback_metadata: str
    nonce: str


def _project_version(root: Path = COMMERCIAL_ROOT) -> str:
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return str(Version(str(payload["project"]["version"])))


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    print("+", subprocess.list2cmdline(argv), flush=True)
    subprocess.run(argv, cwd=COMMERCIAL_ROOT, env=env, check=True)


def _release_from_output(
    output: Path,
    *,
    version: str,
    channel: UpdateChannel,
    previous_version: str,
) -> BuiltFullUpdate:
    manifest_path = output / "stable.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    release = parse_update_manifest(
        payload,
        channel,
        current_version=previous_version,
    )
    if release is None or release.version != version:
        raise ValueError("完整载荷发布清单版本无效")
    archive_path = output / f"wechatrebot-{version}-update.zip"
    if (
        not archive_path.is_file()
        or archive_path.stat().st_size != release.package_size
        or sha256_file(archive_path) != release.package_sha256
    ):
        raise ValueError("完整载荷更新包与签名清单不一致")
    return BuiltFullUpdate(
        version=version,
        archive_path=archive_path,
        manifest_path=manifest_path,
        package_url=release.package_url,
        package_size=release.package_size,
        package_sha256=release.package_sha256,
    )


def _customer_package_from_output(
    package_root: Path,
    *,
    version: str,
    base_url: str,
    native: bool = False,
    manager_exe: Path | None = None,
    output_root: Path | None = None,
    build_installer: bool = True,
) -> BuiltCustomerPackage:
    if native:
        if manager_exe is None or output_root is None:
            raise ValueError("标准安装器构建缺少管理器或输出目录")
        return _native_customer_package_from_output(
            package_root,
            version=version,
            base_url=base_url,
            manager_exe=manager_exe,
            output_root=output_root,
            build_installer=build_installer,
        )
    original_archive = package_root.parent / f"{package_root.name}.zip"
    if not original_archive.is_file():
        candidates = tuple(
            package_root.parent.glob(f"{package_root.name}-*.zip")
        )
        if len(candidates) != 1:
            raise ValueError("无法唯一确定已生成的客户完整安装包")
        original_archive = candidates[0]
    original_checksum = original_archive.with_name(original_archive.name + ".sha256")
    if not original_archive.is_file() or not original_checksum.is_file():
        raise ValueError("客户完整安装包或校验文件不存在")
    digest = sha256_file(original_archive)
    expected_checksum = f"{digest}  {original_archive.name}"
    if original_checksum.read_text(encoding="ascii").strip() != expected_checksum:
        raise ValueError("客户完整安装包校验文件不一致")

    required_entries = {
        f"{package_root.name}/WeChatReBot.exe",
        f"{package_root.name}/payload.wrb",
        f"{package_root.name}/DISTRIBUTION-MANIFEST.json",
        f"{package_root.name}/START-HERE.txt",
    }
    with zipfile.ZipFile(original_archive) as archive:
        entries = set(archive.namelist())
        if not required_entries.issubset(entries):
            raise ValueError("客户完整安装包缺少启动程序、载荷或安装说明")

    archive_name = (
        f"WeChatReBot-{version}-Windows-x64-{digest[:12].lower()}.zip"
    )
    archive_path = original_archive.with_name(archive_name)
    checksum_path = archive_path.with_suffix(archive_path.suffix + ".sha256")
    if original_archive != archive_path:
        original_archive.replace(archive_path)
        original_checksum.unlink()
    checksum_path.write_text(
        f"{digest}  {archive_path.name}\n",
        encoding="ascii",
    )
    return BuiltCustomerPackage(
        version=version,
        archive_path=archive_path,
        checksum_path=checksum_path,
        package_url=f"{base_url.rstrip('/')}/downloads/{archive_path.name}",
        checksum_url=f"{base_url.rstrip('/')}/downloads/{checksum_path.name}",
        package_size=archive_path.stat().st_size,
        package_sha256=digest,
    )


def _copy_or_link(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if sha256_file(target) != sha256_file(source):
            raise ValueError(f"标准安装器载荷已存在但摘要不一致：{target}")
        return
    try:
        target.hardlink_to(source)
    except OSError:
        shutil.copy2(source, target)


def _wrap_native_installer(
    setup: Path,
    output_root: Path,
    version: str,
    payload: Path,
) -> tuple[Path, Path]:
    archive_root = output_root / "distribution"
    archive_root.mkdir(parents=True, exist_ok=True)
    archive_path = archive_root / f"WeChatReBot-{version}-Windows-x64-online.zip"
    instructions = (
        f"小妹 {version} Windows 在线安装包\n\n"
        f"1. 完整解压本 ZIP。\n2. 双击 {setup.name}，按安装向导操作。\n"
        f"3. 安装时保持联网，将另外下载约 {payload.stat().st_size / 1024**2:.1f} MB 运行组件。\n"
        "已有安装可升级或修复，无需先卸载。修改安装目录前请备份原有数据。\n"
        "支持 Windows 10/11 x64；微信版本 4.1.8.27；需要 Edge WebView2。\n"
        "本安装程序未做 Windows 代码签名；文件完整性由公开 SHA-256 校验。\n"
    )
    with zipfile.ZipFile(
        archive_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=1,
    ) as archive:
        archive.write(setup, setup.name)
        archive.writestr("安装说明.txt", instructions.encode("utf-8-sig"))
    digest = sha256_file(archive_path)
    named = archive_path.with_name(f"{archive_path.stem}-{digest[:12].lower()}.zip")
    if named.exists() and sha256_file(named) != digest:
        raise ValueError(f"标准安装器压缩包已存在但摘要不一致：{named}")
    if named != archive_path:
        archive_path.replace(named)
    checksum = named.with_suffix(named.suffix + ".sha256")
    checksum.write_text(f"{digest}  {named.name}\n", encoding="ascii")
    return named, checksum


def _native_customer_package_from_output(
    package_root: Path,
    *,
    version: str,
    base_url: str,
    manager_exe: Path,
    output_root: Path,
    build_installer: bool,
) -> BuiltCustomerPackage:
    manager = manager_exe.expanduser().resolve()
    payload = package_root / "payload.wrb"
    if not manager.is_file() or not payload.is_file():
        raise ValueError("标准安装器缺少管理器或完整载荷")
    if not build_native_installer._offline_manager_matches(payload, manager):
        raise ValueError("标准安装器引导管理器与在线载荷中的管理器不一致")
    with zipfile.ZipFile(payload) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
    if manifest.get("release_version") != version:
        raise ValueError("标准安装器载荷版本与项目版本不一致")

    digest = sha256_file(payload)
    named_payload = output_root / "payloads" / f"WeChatReBot-{version}-payload-{digest[:12].lower()}.wrb"
    _copy_or_link(payload, named_payload)
    payload_url = f"{base_url.rstrip('/')}/releases/{named_payload.name}"
    setup_root = output_root / "native-installer"
    setup = setup_root / f"WeChatReBot-Setup-{version}.exe"
    if build_installer or not setup.is_file():
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "build_native_installer.py"),
                "--manager-exe",
                str(manager),
                "--payload-url",
                payload_url,
                "--payload-sha256",
                digest,
                "--output",
                str(setup_root),
            ]
        )
    if not setup.is_file():
        raise ValueError(f"标准安装器未生成：{setup}")
    archive, checksum = _wrap_native_installer(
        setup,
        output_root,
        version,
        named_payload,
    )
    package_digest = sha256_file(archive)
    return BuiltCustomerPackage(
        version=version,
        archive_path=archive,
        checksum_path=checksum,
        package_url=f"{base_url.rstrip('/')}/downloads/{archive.name}",
        checksum_url=f"{base_url.rstrip('/')}/downloads/{checksum.name}",
        package_size=archive.stat().st_size,
        package_sha256=package_digest,
        installer={
            "kind": "inno-online",
            "entrypoint": setup.name,
            "payload_url": payload_url,
            "payload_size": named_payload.stat().st_size,
            "payload_sha256": digest,
            "windows_code_signed": False,
        },
        payload_path=named_payload,
        payload_url=payload_url,
    )


def _write_download_metadata(
    target: Path,
    *,
    package: BuiltCustomerPackage,
    published_at: str,
    title: str,
    notes: tuple[str, ...],
) -> Path:
    payload = {
        "schema_version": 1,
        "product": "小妹",
        "platform": "Windows 10/11 x64",
        "version": package.version,
        "published_at": published_at,
        "title": title.strip()[:100] or f"版本 {package.version}",
        "notes": [item.strip()[:500] for item in notes if item.strip()][:20],
        "download": {
            "url": package.package_url,
            "filename": package.archive_path.name,
            "size": package.package_size,
            "sha256": package.package_sha256,
            "checksum_url": package.checksum_url,
        },
        "requirements": {
            "windows": "Windows 10/11 x64",
            "wechat": "4.1.8.27",
            "webview": "Microsoft Edge WebView2",
        },
    }
    if package.installer is not None:
        payload["installer"] = package.installer
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def _public_head(url: str, *, expected_size: int, timeout: int) -> None:
    request = urllib.request.Request(
        url,
        method="HEAD",
        headers={"Cache-Control": "no-cache", "User-Agent": "WeChatReBot-Publisher/1"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        length = response.headers.get("Content-Length")
        if response.geturl() != url or response.status != 200 or int(length or 0) != expected_size:
            raise ValueError("公网客户安装包状态或大小与发布信息不一致")


def _verify_public_download(
    url: str, *, expected_size: int, expected_sha256: str, timeout: int
) -> None:
    """Read the CDN object end to end before switching the stable release."""
    _public_head(url, expected_size=expected_size, timeout=timeout)
    request = urllib.request.Request(
        url,
        headers={"Cache-Control": "no-cache", "User-Agent": "WeChatReBot-Publisher/1"},
    )
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(request, timeout=timeout) as response:
        if response.geturl() != url or response.status != 200:
            raise ValueError(f"CDN 公网下载不可访问：{url}")
        while chunk := response.read(4 * 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    if size != expected_size or digest.hexdigest().lower() != expected_sha256.lower():
        raise ValueError(f"CDN 公网下载大小或 SHA-256 不一致：{url}")


def _public_download_page_is_hidden(base_url: str, *, timeout: int) -> None:
    root = f"{base_url.rstrip('/')}/"
    urls = (
        root,
        f"{root}index.html",
        f"{root}site",
        f"{root}site/",
        f"{root}site/index.html",
    )
    for url in urls:
        request = urllib.request.Request(
            url,
            headers={
                "Cache-Control": "no-cache",
                "User-Agent": "WeChatReBot-Publisher/1",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout):
                pass
        except urllib.error.HTTPError as exc:
            if exc.code == 404 and exc.geturl() == url:
                continue
            raise ValueError(f"公网下载页面未正确隐藏：{url}") from exc
        raise ValueError(f"公网下载页面仍可访问：{url}")


def _stage_download_site(
    package: BuiltCustomerPackage,
    metadata_path: Path,
    *,
    host: str,
    remote_root: str,
    base_url: str,
    work_root: Path,
    timeout: int,
    upload_package_to_server: bool = True,
) -> StagedDownloadSite:
    publisher._safe_remote_inputs(host, remote_root)
    nonce = uuid.uuid4().hex
    downloads_root = f"{remote_root}/downloads"
    releases_root = f"{remote_root}/releases"
    site_root = f"{remote_root}/site"
    manifests_root = f"{remote_root}/manifests"
    remote_archive = f"{downloads_root}/{package.archive_path.name}"
    remote_checksum = f"{downloads_root}/{package.checksum_path.name}"
    remote_payload = (
        f"{releases_root}/{package.payload_path.name}"
        if package.payload_path is not None
        else None
    )
    archive_upload = f"{remote_archive}.upload-{nonce}"
    checksum_upload = f"{remote_checksum}.upload-{nonce}"
    payload_upload = f"{remote_payload}.upload-{nonce}" if remote_payload else None
    index_upload = f"{site_root}/index.html.upload-{nonce}"
    metadata_upload = f"{remote_root}/download.json.upload-{nonce}"
    rollback_metadata = f"{manifests_root}/.download-previous-{nonce}.json"

    rendered_nginx = work_root / "wechatrebot-updates.conf"
    nginx_source = COMMERCIAL_ROOT / "deploy" / "nginx" / "wechatrebot-updates.conf"
    rendered_nginx.write_text(
        nginx_source.read_text(encoding="utf-8").replace(
            publisher.DEFAULT_REMOTE_ROOT,
            remote_root,
        ),
        encoding="utf-8",
    )
    nginx_upload = f"/tmp/wechatrebot-updates-{nonce}.conf"
    index_source = COMMERCIAL_ROOT / "deploy" / "download-site" / "index.html"

    publisher._ssh(
        host,
        "set -eu; "
        f"mkdir -p {shlex.quote(downloads_root)} {shlex.quote(releases_root)} {shlex.quote(site_root)} {shlex.quote(manifests_root)}; "
        f"test \"$(readlink -f {shlex.quote(remote_root)})\" = {shlex.quote(remote_root)}",
    )
    archive_candidate = archive_upload
    archive_move = (
        f"mv -f -- {shlex.quote(archive_upload)} {shlex.quote(remote_archive)}; "
    )
    payload_candidate = payload_upload
    payload_move = (
        f"mv -f -- {shlex.quote(payload_upload)} {shlex.quote(remote_payload)}; "
        if payload_upload and remote_payload
        else ""
    )
    if upload_package_to_server:
        try:
            publisher._ssh(
                host,
                "set -eu; "
                f"test \"$(stat -c%s {shlex.quote(remote_archive)})\" -eq {package.package_size}; "
                f"printf '%s  %s\\n' {package.package_sha256} {shlex.quote(remote_archive)} | sha256sum -c -",
            )
        except subprocess.CalledProcessError:
            publisher._scp(package.archive_path, host, archive_upload)
        else:
            archive_candidate = remote_archive
            archive_move = ""
            print("服务器已存在摘要一致的客户安装包，跳过重复上传。", flush=True)
        publisher._scp(package.checksum_path, host, checksum_upload)
        if package.payload_path is not None and remote_payload and payload_upload:
            try:
                publisher._ssh(
                    host,
                    "set -eu; "
                    f"test \"$(stat -c%s {shlex.quote(remote_payload)})\" -eq {package.payload_path.stat().st_size}; "
                    f"printf '%s  %s\\n' {sha256_file(package.payload_path)} {shlex.quote(remote_payload)} | sha256sum -c -",
                )
            except subprocess.CalledProcessError:
                publisher._scp(package.payload_path, host, payload_upload)
            else:
                payload_candidate = remote_payload
                payload_move = ""
                print("服务器已存在摘要一致的在线安装载荷，跳过重复上传。", flush=True)
    else:
        print("客户安装包使用 OSS/CDN，服务器仅发布元数据并保持下载页隐藏。", flush=True)
    publisher._scp(index_source, host, index_upload)
    publisher._scp(metadata_path, host, metadata_upload)
    publisher._scp(rendered_nginx, host, nginx_upload)

    archive_size = package.archive_path.stat().st_size
    checksum_hash = sha256_file(package.checksum_path)
    index_hash = sha256_file(index_source)
    metadata_hash = sha256_file(metadata_path)
    verification = "set -eu; "
    if upload_package_to_server:
        verification += (
            f"test \"$(stat -c%s {shlex.quote(archive_candidate)})\" -eq {archive_size}; "
            f"printf '%s  %s\\n' {package.package_sha256} {shlex.quote(archive_candidate)} | sha256sum -c -; "
            f"printf '%s  %s\\n' {checksum_hash} {shlex.quote(checksum_upload)} | sha256sum -c -; "
            f"{archive_move}"
            f"mv -f -- {shlex.quote(checksum_upload)} {shlex.quote(remote_checksum)}; "
        )
        if package.payload_path is not None and remote_payload and payload_candidate:
            verification += (
                f"test \"$(stat -c%s {shlex.quote(payload_candidate)})\" -eq {package.payload_path.stat().st_size}; "
                f"printf '%s  %s\\n' {sha256_file(package.payload_path)} {shlex.quote(payload_candidate)} | sha256sum -c -; "
                f"{payload_move}"
            )
    verification += (
        f"printf '%s  %s\\n' {index_hash} {shlex.quote(index_upload)} | sha256sum -c -; "
        f"printf '%s  %s\\n' {metadata_hash} {shlex.quote(metadata_upload)} | sha256sum -c -; "
        f"mv -f -- {shlex.quote(index_upload)} {shlex.quote(site_root + '/index.html')}"
    )
    publisher._ssh(host, verification)

    nginx_config = "/etc/nginx/snippets/wechatrebot-updates.conf"
    nginx_backup = f"/etc/nginx/snippets/.wechatrebot-updates-{nonce}.conf"
    publisher._ssh(
        host,
        "set -eu; "
        f"cp -f -- {shlex.quote(nginx_config)} {shlex.quote(nginx_backup)}; "
        f"if mv -f -- {shlex.quote(nginx_upload)} {shlex.quote(nginx_config)} "
        "&& nginx -t && systemctl reload nginx; then "
        f"rm -f -- {shlex.quote(nginx_backup)}; "
        "else "
        f"cp -f -- {shlex.quote(nginx_backup)} {shlex.quote(nginx_config)}; "
        "nginx -t; systemctl reload nginx; "
        f"rm -f -- {shlex.quote(nginx_backup)}; exit 1; fi",
    )
    _public_download_page_is_hidden(base_url, timeout=timeout)
    _public_head(package.package_url, expected_size=package.package_size, timeout=timeout)
    if package.payload_path is not None and package.payload_url:
        _public_head(
            package.payload_url,
            expected_size=package.payload_path.stat().st_size,
            timeout=timeout,
        )
    return StagedDownloadSite(
        metadata_upload=metadata_upload,
        rollback_metadata=rollback_metadata,
        nonce=nonce,
    )


def _activate_download_metadata(
    staged: StagedDownloadSite,
    package: BuiltCustomerPackage,
    *,
    host: str,
    remote_root: str,
    base_url: str,
    timeout: int,
) -> None:
    current = f"{remote_root}/download.json"
    archived = f"{remote_root}/manifests/download-{package.version}.json"
    publisher._ssh(
        host,
        "set -eu; "
        f"if test -f {shlex.quote(current)}; then cp -f -- {shlex.quote(current)} {shlex.quote(staged.rollback_metadata)}; "
        f"else rm -f -- {shlex.quote(staged.rollback_metadata)}; fi; "
        f"mv -f -- {shlex.quote(staged.metadata_upload)} {shlex.quote(current)}",
    )
    try:
        request = urllib.request.Request(
            f"{base_url.rstrip('/')}/download.json",
            headers={"Accept": "application/json", "Cache-Control": "no-cache", "User-Agent": "WeChatReBot-Publisher/1"},
        )
        with urllib.request.urlopen(request, timeout=timeout) as response:
            live = json.loads(response.read(1024 * 1024).decode("utf-8-sig"))
        download = live.get("download") if isinstance(live, dict) else None
        if (
            live.get("version") != package.version
            or not isinstance(download, dict)
            or download.get("url") != package.package_url
            or int(download.get("size") or 0) != package.package_size
            or str(download.get("sha256") or "").upper() != package.package_sha256
        ):
            raise ValueError("公网下载信息与客户安装包不一致")
        if package.installer is not None and live.get("installer") != package.installer:
            raise ValueError("公网下载信息与标准安装器不一致")
        _public_head(package.package_url, expected_size=package.package_size, timeout=timeout)
        if package.payload_path is not None and package.payload_url:
            _public_head(
                package.payload_url,
                expected_size=package.payload_path.stat().st_size,
                timeout=timeout,
            )
    except Exception:
        publisher._ssh(
            host,
            "set -eu; "
            f"if test -f {shlex.quote(staged.rollback_metadata)}; then mv -f -- {shlex.quote(staged.rollback_metadata)} {shlex.quote(current)}; "
            f"else rm -f -- {shlex.quote(current)}; fi",
        )
        raise
    publisher._ssh(
        host,
        "set -eu; "
        f"cp -f -- {shlex.quote(current)} {shlex.quote(archived)}; "
        f"rm -f -- {shlex.quote(staged.rollback_metadata)}",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="一键测试、构建、签名并发布完整载荷更新。"
    )
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--title", default="完整载荷更新")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--minimum-supported-version", default="0.7.0")
    parser.add_argument("--build", type=int)
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument(
        "--private-key",
        type=Path,
        default=COMMERCIAL_ROOT / "config" / "local" / "update-signing-private.pem",
    )
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--host", default=publisher.DEFAULT_HOST)
    parser.add_argument("--remote-root", default=publisher.DEFAULT_REMOTE_ROOT)
    parser.add_argument("--base-url", default=publisher.DEFAULT_BASE_URL)
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
    parser.add_argument(
        "--resume",
        action="store_true",
        help="复用指定 --output 中已完成的构建产物，从本地校验和发布阶段继续。",
    )
    parser.add_argument(
        "--refresh-manager",
        action="store_true",
        help=(
            "与 --resume 一起使用：复用运行时，但重新构建管理器、暂存载荷、"
            "在线更新包和客户完整包。"
        ),
    )
    parser.add_argument("--yes", action="store_true")
    args = parser.parse_args(argv)
    if args.refresh_manager and not args.resume:
        parser.error("--refresh-manager 必须与 --resume 一起使用")

    if sys.version_info[:2] != (3, 13) or platform.architecture()[0] != "64bit":
        parser.error("必须使用 Python 3.13 x64 执行发布脚本")
    if not args.dry_run and (
        not shutil.which("ssh.exe") or not shutil.which("scp.exe")
    ):
        parser.error("找不到 Windows OpenSSH 的 ssh.exe 或 scp.exe")

    source_root = args.source_root.expanduser().resolve()
    if not source_root.is_dir():
        parser.error(f"项目源码目录不存在：{source_root}")
    private_key = args.private_key.expanduser().resolve()
    if not private_key.is_file():
        parser.error(f"更新签名私钥不存在：{private_key}")

    version = _project_version()
    channel = load_update_channel()
    publisher._signing_key(private_key, channel)
    oss_config = oss_storage.load_oss_release_storage(args.oss_config)
    oss_required = Version(version) >= Version("0.9.8")
    artifact_storage = args.artifact_storage or (
        "oss" if oss_required or oss_config.enabled else "server"
    )
    if oss_required and artifact_storage != "oss":
        parser.error("0.9.8 及以后版本必须使用 OSS/CDN，禁止回退为服务器直链")
    if oss_required and not oss_config.enabled and not args.dry_run:
        parser.error("0.9.8 及以后版本发布前必须启用 OSS 配置并完成凭证校验")
    asset_base_url = (
        args.asset_base_url
        or (oss_config.public_base_url if artifact_storage == "oss" else args.base_url)
    ).rstrip("/")
    live_manifest = publisher._fetch_live_manifest(channel)
    live_version = str(Version(str(live_manifest["version"])))
    download_version = publisher._fetch_download_version(args.base_url)
    published_floor = publisher._published_version_floor(
        live_version, download_version
    )
    if Version(version) < Version(published_floor) or (
        Version(version) == Version(published_floor) and not args.dry_run
    ):
        parser.error(
            f"项目版本 {version} 必须高于线上已发布版本 {published_floor}"
            f"（更新通道 {live_version}，下载页 {download_version}）；请先提升版本号"
        )

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    output = (
        args.output
        or COMMERCIAL_ROOT / "artifacts" / f"publish-full-{version}-{timestamp}"
    ).expanduser().resolve()
    artifacts_root = (COMMERCIAL_ROOT / "artifacts").resolve()
    if output == artifacts_root or artifacts_root not in output.parents:
        parser.error("输出目录必须位于本项目 artifacts 目录内")
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error(f"输出目录不是空目录：{output}")
    output.mkdir(parents=True, exist_ok=True)

    build_env = os.environ.copy()
    build_env["PYTHONPATH"] = str(SOURCE_ROOT)
    if not args.skip_tests:
        _run([sys.executable, "-m", "pytest", "-q"], env=build_env)
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "validate_release_metadata.py"),
                "--root",
                str(COMMERCIAL_ROOT),
            ],
            env=build_env,
        )

    manager_build = output / "manager-build"
    runtime_build = output / "runtime-build"
    staging = output / "staging"
    update_output = output / "update"
    distribution_root = output / "distribution"
    if args.refresh_manager:
        for candidate in (manager_build, staging, update_output, distribution_root):
            try:
                candidate.relative_to(output)
            except ValueError as exc:
                raise RuntimeError(f"拒绝清理输出目录之外的路径：{candidate}") from exc
            if candidate.is_dir():
                shutil.rmtree(candidate)
            elif candidate.exists():
                candidate.unlink()
    distribution_root.mkdir(parents=True, exist_ok=True)
    customer_root = distribution_root / f"WeChatReBot-{version}-Windows-x64"
    if not args.resume or args.refresh_manager:
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "build_manager.py"),
                "--output",
                str(manager_build),
                "--host-python",
                sys.executable,
            ]
        )
    runtime_command = [
        sys.executable,
        str(SCRIPTS_ROOT / "build_runtime.py"),
        "--output",
        str(runtime_build),
        "--host-python",
        sys.executable,
    ]
    if args.wheelhouse:
        runtime_command.extend(
            ["--wheelhouse", str(args.wheelhouse.expanduser().resolve())]
        )
    if args.offline:
        runtime_command.append("--offline")
    if not args.resume:
        _run(runtime_command)
    if not args.resume or args.refresh_manager:
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "build_legal_inventory.py"),
                "--runtime-root",
                str(runtime_build),
                "--manager-site-packages",
                str(manager_build / "build-venv" / "Lib" / "site-packages"),
                "--project-root",
                str(source_root),
                "--sbom",
                "release/sbom.cdx.json",
                "--notices",
                "legal/THIRD-PARTY-NOTICES.md",
                "--licenses",
                "legal/THIRD-PARTY-LICENSES.txt",
                "--review",
                "release/legal-review.json",
                "--corresponding-source",
                "legal/CORRESPONDING-SOURCE.md",
            ]
        )
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "validate_release_metadata.py"),
                "--root",
                str(COMMERCIAL_ROOT),
            ],
            env=build_env,
        )
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "build_staging.py"),
                "--source-root",
                str(source_root),
                "--runtime-root",
                str(runtime_build),
                "--manager-exe",
                str(manager_build / "dist" / "WeChatReBot.exe"),
                "--output",
                str(staging),
            ]
        )
    package_url = (
        f"{asset_base_url}/releases/"
        f"wechatrebot-{version}-update.zip"
    )
    release_command = [
        sys.executable,
        str(SCRIPTS_ROOT / "build_update_release.py"),
        "--payload",
        str(staging),
        "--output",
        str(update_output),
        "--private-key",
        str(private_key),
        "--package-url",
        package_url,
        "--title",
        args.title,
        "--minimum-supported-version",
        args.minimum_supported_version,
    ]
    if args.mandatory:
        release_command.append("--mandatory")
    if args.build is not None:
        release_command.extend(["--build", str(args.build)])
    if not args.resume or args.refresh_manager:
        for note in args.note:
            release_command.extend(["--note", note])
        _run(release_command)
        _run(
            [
                sys.executable,
                str(SCRIPTS_ROOT / "build_distribution.py"),
                "--payload",
                str(staging),
                "--output",
                str(customer_root),
            ]
        )
    else:
        print(f"正在复用已完成的完整构建产物：{output}", flush=True)

    release = _release_from_output(
        update_output,
        version=version,
        channel=channel,
        previous_version=(
            live_version if Version(version) > Version(live_version) else "0.0.0"
        ),
    )
    if release.package_url != package_url:
        raise ValueError(
            "复用产物中的更新包地址与当前存储后端不一致；"
            "请使用新的输出目录重新构建，或搭配 --refresh-manager 重建交付包。"
        )
    customer_package = _customer_package_from_output(
        customer_root,
        version=version,
        base_url=asset_base_url,
        native=True,
        manager_exe=manager_build / "dist" / "WeChatReBot.exe",
        output_root=output,
        build_installer=(not args.resume or args.refresh_manager),
    )
    release_manifest = json.loads(
        release.manifest_path.read_text(encoding="utf-8-sig")
    )
    download_metadata = _write_download_metadata(
        output / "download-site" / "download.json",
        package=customer_package,
        published_at=str(release_manifest["published_at"]),
        title=args.title,
        notes=tuple(args.note),
    )
    summary = {
        "version": release.version,
        "online_update": {
            "archive": str(release.archive_path),
            "package_url": release.package_url,
            "size": release.package_size,
            "sha256": release.package_sha256,
            "manifest": str(release.manifest_path),
        },
        "customer_download": {
            "archive": str(customer_package.archive_path),
            "package_url": customer_package.package_url,
            "size": customer_package.package_size,
            "sha256": customer_package.package_sha256,
            "metadata": str(download_metadata),
            "installer": customer_package.installer,
            "payload": (
                {
                    "path": str(customer_package.payload_path),
                    "url": customer_package.payload_url,
                    "size": customer_package.payload_path.stat().st_size,
                    "sha256": sha256_file(customer_package.payload_path),
                }
                if customer_package.payload_path is not None
                else None
            ),
        },
        "artifact_storage": artifact_storage,
        "oss_upload_enabled": artifact_storage == "oss" and not args.dry_run,
        "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        print("完整载荷本地构建完成；--dry-run 未上传服务器。")
        return 0
    if not args.yes and not publisher._prompt_publish(version, release.package_size):
        print("未确认版本号，已取消上传。")
        return 2

    if artifact_storage == "oss":
        oss_objects = (
            oss_storage.OssReleaseObject(
                release.archive_path,
                "releases",
                release.package_sha256,
                "application/zip",
                "public,max-age=3600",
            ),
            oss_storage.OssReleaseObject(
                customer_package.archive_path,
                "downloads",
                customer_package.package_sha256,
                "application/zip",
                "public,max-age=31536000,immutable",
            ),
            oss_storage.OssReleaseObject(
                customer_package.checksum_path,
                "downloads",
                sha256_file(customer_package.checksum_path),
                "text/plain; charset=utf-8",
                "public,max-age=31536000,immutable",
            ),
            *(
                (
                    oss_storage.OssReleaseObject(
                        customer_package.payload_path,
                        "releases",
                        sha256_file(customer_package.payload_path),
                        "application/octet-stream",
                        "public,max-age=31536000,immutable",
                    ),
                )
                if customer_package.payload_path is not None
                else ()
            ),
        )
        oss_storage.upload_release_objects(oss_config, oss_objects)
        for obj in oss_objects:
            url = oss_config.public_url(obj.category, obj.local_path.name)
            _verify_public_download(
                url,
                expected_size=obj.local_path.stat().st_size,
                expected_sha256=obj.sha256,
                timeout=channel.request_timeout_seconds,
            )
            print(f"CDN 公网下载校验通过：{url}", flush=True)

    remote_root = args.remote_root.rstrip("/")
    staged_site = _stage_download_site(
        customer_package,
        download_metadata,
        host=args.host,
        remote_root=remote_root,
        base_url=args.base_url,
        work_root=download_metadata.parent,
        timeout=channel.request_timeout_seconds,
        upload_package_to_server=artifact_storage == "server",
    )
    publisher._publish(
        release,
        channel=channel,
        previous_version=live_version,
        host=args.host,
        remote_root=remote_root,
        smoke_root=artifacts_root / f"smoke-{version.replace('.', '')}",
        skip_remote_smoke=args.skip_remote_smoke,
        upload_archive_to_server=artifact_storage == "server",
        after_switch=lambda: _activate_download_metadata(
            staged_site,
            customer_package,
            host=args.host,
            remote_root=remote_root,
            base_url=args.base_url,
            timeout=channel.request_timeout_seconds,
        ),
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
