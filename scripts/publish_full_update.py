"""Build helpers for full-payload releases. Publishing lives in publish_github_release.py."""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
import zipfile

from packaging.version import Version


COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = COMMERCIAL_ROOT / "scripts"
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
for search_root in (SCRIPTS_ROOT, SOURCE_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

import build_native_installer
from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.updates import UpdateChannel, parse_update_manifest


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
    asset_prefix: str,
    native: bool = False,
    manager_exe: Path | None = None,
    output_root: Path | None = None,
    build_installer: bool = True,
) -> BuiltCustomerPackage:
    prefix = asset_prefix.rstrip("/")
    if native:
        if manager_exe is None or output_root is None:
            raise ValueError("标准安装器构建缺少管理器或输出目录")
        return _native_customer_package_from_output(
            package_root,
            version=version,
            asset_prefix=prefix,
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
        package_url=f"{prefix}/{archive_path.name}",
        checksum_url=f"{prefix}/{checksum_path.name}",
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
    asset_prefix: str,
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
    payload_url = f"{asset_prefix}/{named_payload.name}"
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
        package_url=f"{asset_prefix}/{archive.name}",
        checksum_url=f"{asset_prefix}/{checksum.name}",
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


@dataclass(frozen=True, slots=True)
class FullBuildOptions:
    source_root: Path
    private_key: Path
    output: Path
    title: str
    notes: tuple[str, ...]
    minimum_supported_version: str
    build: int | None
    mandatory: bool
    wheelhouse: Path | None
    offline: bool
    skip_tests: bool
    resume: bool
    refresh_manager: bool


def build_full_update_artifacts(
    options: FullBuildOptions,
    *,
    version: str,
    channel: UpdateChannel,
    previous_version: str,
    package_url: str,
    asset_prefix: str,
) -> tuple[BuiltFullUpdate, BuiltCustomerPackage]:
    """Tests, metadata, manager, runtime, legal inventory, staging, signed update ZIP and customer package."""
    output = options.output
    output.mkdir(parents=True, exist_ok=True)
    build_env = os.environ.copy()
    build_env["PYTHONPATH"] = str(SOURCE_ROOT)
    if not options.skip_tests:
        _run([sys.executable, "-m", "pytest", "-q"], env=build_env)
        _run([sys.executable, str(SCRIPTS_ROOT / "validate_release_metadata.py"), "--root", str(COMMERCIAL_ROOT)],
             env=build_env)

    manager_build = output / "manager-build"
    runtime_build = output / "runtime-build"
    staging = output / "staging"
    update_output = output / "update"
    distribution_root = output / "distribution"
    if options.refresh_manager:
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
    rebuild = not options.resume or options.refresh_manager
    if rebuild:
        _run([sys.executable, str(SCRIPTS_ROOT / "build_manager.py"), "--output", str(manager_build),
              "--host-python", sys.executable])
    runtime_command = [sys.executable, str(SCRIPTS_ROOT / "build_runtime.py"), "--output", str(runtime_build),
                       "--host-python", sys.executable]
    if options.wheelhouse:
        runtime_command.extend(["--wheelhouse", str(options.wheelhouse.expanduser().resolve())])
    if options.offline:
        runtime_command.append("--offline")
    if not options.resume:
        _run(runtime_command)
    if rebuild:
        _run([sys.executable, str(SCRIPTS_ROOT / "build_legal_inventory.py"),
              "--runtime-root", str(runtime_build),
              "--manager-site-packages", str(manager_build / "build-venv" / "Lib" / "site-packages"),
              "--project-root", str(options.source_root),
              "--sbom", "release/sbom.cdx.json", "--notices", "legal/THIRD-PARTY-NOTICES.md",
              "--licenses", "legal/THIRD-PARTY-LICENSES.txt", "--review", "release/legal-review.json",
              "--corresponding-source", "legal/CORRESPONDING-SOURCE.md"])
        _run([sys.executable, str(SCRIPTS_ROOT / "validate_release_metadata.py"), "--root", str(COMMERCIAL_ROOT)],
             env=build_env)
        _run([sys.executable, str(SCRIPTS_ROOT / "build_staging.py"), "--source-root", str(options.source_root),
              "--runtime-root", str(runtime_build), "--manager-exe", str(manager_build / "dist" / "WeChatReBot.exe"),
              "--output", str(staging)])
    release_command = [sys.executable, str(SCRIPTS_ROOT / "build_update_release.py"), "--payload", str(staging),
                       "--output", str(update_output), "--private-key", str(options.private_key),
                       "--package-url", package_url, "--title", options.title,
                       "--minimum-supported-version", options.minimum_supported_version]
    if options.mandatory:
        release_command.append("--mandatory")
    if options.build is not None:
        release_command.extend(["--build", str(options.build)])
    if rebuild:
        for note in options.notes:
            release_command.extend(["--note", note])
        _run(release_command)
        _run([sys.executable, str(SCRIPTS_ROOT / "build_distribution.py"), "--payload", str(staging),
              "--output", str(customer_root)])
    else:
        print(f"正在复用已完成的完整构建产物：{output}", flush=True)

    release = _release_from_output(update_output, version=version, channel=channel,
                                   previous_version=previous_version)
    if release.package_url != package_url:
        raise ValueError("复用产物中的更新包地址与当前发布目标不一致；请使用新的输出目录重新构建，"
                         "或搭配 --refresh-manager 重建交付包。")
    customer_package = _customer_package_from_output(
        customer_root, version=version, asset_prefix=asset_prefix, native=True,
        manager_exe=manager_build / "dist" / "WeChatReBot.exe", output_root=output, build_installer=rebuild,
    )
    return release, customer_package
