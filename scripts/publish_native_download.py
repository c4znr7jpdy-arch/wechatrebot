"""Build an online native installer ZIP, upload to OSS, activate the download page."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import shlex
import subprocess
import sys
import urllib.request
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "scripts"), str(ROOT / "src")]

import build_native_installer
import build_staging
import oss_release_storage as oss
import publish_full_update as full
import publish_manager_update as publisher
from wechatrebot_manager.payload import sha256_file, verify_payload
from wechatrebot_manager.wechat_path import discover_wechat_executables


def run(script: str, *args: object) -> None:
    subprocess.run([sys.executable, str(ROOT / "scripts" / script), *map(str, args)], cwd=ROOT, check=True)


def record(path: Path) -> dict:
    return {"path": str(path.resolve()), "size": path.stat().st_size, "sha256": sha256_file(path)}


def verify_record(item: dict) -> Path:
    path = Path(item["path"]).resolve()
    if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
        raise ValueError(f"发布文件已改变：{path.name}")
    return path


def distribution_payload(payload: Path, manager: Path, version: str) -> None:
    verified = verify_payload(payload)
    with zipfile.ZipFile(payload) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
    if manifest.get("release_version") != version:
        raise ValueError("载荷版本与安装程序不匹配")
    if any(value for key, value in manifest.items() if "test_only" in key or "smoke" in key):
        raise ValueError("禁止将本地烟测载荷作为正式下载发布")
    if not verified.files or not build_native_installer._offline_manager_matches(payload, manager):
        raise ValueError("安装引导程序与完整载荷中的管理器不匹配")


def storage_config() -> oss.OssReleaseStorage:
    storage = oss.load_oss_release_storage()
    # The configured custom download domain is not active yet. Use the bucket's
    # fixed HTTPS endpoint for these specific public release objects.
    return replace(storage, public_base_url=f"https://{storage.bucket}.{storage.endpoint}/{storage.object_prefix}")


def validate_runtime_for_release(runtime: Path) -> None:
    manifest = build_staging._validate_runtime_root(runtime, ROOT)
    for field, path in (
        ("requirements_sha256", "release/runtime-requirements.lock"),
        ("astrbot_patch_sha256", "patches/astrbot-4.26.6-wechat.patch"),
        ("astrbot_outbound_policy_patch_sha256", "patches/astrbot-4.26.6-outbound-policy.patch"),
    ):
        if manifest.get(field) != sha256_file(ROOT / path):
            raise ValueError(f"缓存运行时不是当前源码版本，需要重新构建：{path}")


def prepare(args, output: Path) -> Path:
    version = full._project_version()
    if not args.source_root or not args.runtime_root:
        raise ValueError("首次构建需要 --source-root 与 --runtime-root")
    source = args.source_root.resolve()
    runtime = args.runtime_root.resolve()
    validate_runtime_for_release(runtime)
    output.mkdir(parents=True, exist_ok=True)
    manager_build = args.manager_build.resolve() if args.manager_build else output / "manager-build"
    if not args.manager_build:
        build_args = ["--output", manager_build]
        if args.build_python:
            build_args += ["--build-python", args.build_python]
        run("build_manager.py", *build_args)
    manager = build_staging._validate_manager_exe(manager_build / "dist" / "WeChatReBot.exe")
    build_info = json.loads((manager_build / "manager-build-manifest.json").read_text(encoding="utf-8"))
    if build_info.get("manager_version") != version or build_info.get("sha256") != sha256_file(manager):
        raise ValueError("管理器构建版本或摘要无效")
    if build_info.get("self_test", {}).get("ok") is not True:
        raise ValueError("管理器尚未通过冻结自检")
    site_packages = (args.build_python.resolve().parent.parent / "Lib" / "site-packages"
                     if args.build_python else manager_build / "build-venv" / "Lib" / "site-packages")
    run("build_legal_inventory.py", "--runtime-root", runtime,
        "--manager-site-packages", site_packages, "--project-root", source,
        "--sbom", "release/sbom.cdx.json", "--notices", "legal/THIRD-PARTY-NOTICES.md",
        "--licenses", "legal/THIRD-PARTY-LICENSES.txt", "--review", "release/legal-review.json",
        "--corresponding-source", "legal/CORRESPONDING-SOURCE.md")
    staging = output / "staging"
    run("build_staging.py", "--source-root", source, "--runtime-root", runtime,
        "--manager-exe", manager, "--output", staging)
    run("build_distribution.py", "--payload", staging, "--output", output / "distribution")
    payload = output / "distribution" / "payload.wrb"
    distribution_payload(payload, manager, version)
    wechat = args.wechat_exe
    if wechat is None:
        wechat = next((item.path for item in discover_wechat_executables() if item.supported), None)
    if wechat is None:
        raise ValueError("未发现兼容微信，请用 --wechat-exe 指定后完成隔离安装验收")
    run("smoke_install.py", "--payload", payload, "--install-root", output / "smoke-install", "--wechat-exe", wechat)
    digest = sha256_file(payload)
    named_payload = payload.with_name(f"WeChatReBot-{version}-payload-{digest[:12].lower()}.wrb")
    payload.rename(named_payload)
    storage = storage_config()
    payload_url = storage.public_url("releases", named_payload.name)
    setup_root = output / "setup"
    run("build_native_installer.py", "--manager-exe", manager, "--payload-url", payload_url,
        "--payload-sha256", digest, "--output", setup_root)
    setup = setup_root / f"WeChatReBot-Setup-{version}.exe"
    package, checksum = wrap_installer(setup, output, version, named_payload)
    metadata = output / "download.json"
    full._write_download_metadata(metadata, package=full.BuiltCustomerPackage(
        version, package, checksum, storage.public_url("downloads", package.name),
        storage.public_url("downloads", checksum.name), package.stat().st_size, sha256_file(package)),
        published_at=datetime.now(timezone.utc).isoformat(), title=args.title, notes=tuple(args.note))
    data = json.loads(metadata.read_text(encoding="utf-8"))
    data["installer"] = {"kind": "inno-online", "entrypoint": setup.name,
                         "payload_url": payload_url, "payload_size": named_payload.stat().st_size,
                         "payload_sha256": digest, "windows_code_signed": False}
    metadata.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    plan = {"schema_version": 1, "version": version, "smoke_passed": True,
            "payload": record(named_payload), "manager": record(manager), "setup": record(setup),
            "archive": record(package), "checksum": record(checksum), "metadata": record(metadata)}
    path = output / "publish-plan.json"
    path.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def wrap_installer(setup: Path, output: Path, version: str, payload: Path) -> tuple[Path, Path]:
    archive_path = output / f"WeChatReBot-{version}-Windows-x64-online.zip"
    instructions = (
        f"小妹 {version} Windows 在线安装包\n\n"
        f"1. 完整解压本 ZIP。\n2. 双击 {setup.name}，按安装向导操作。\n"
        f"3. 安装时保持联网，将另外下载约 {payload.stat().st_size / 1024**2:.1f} MB 运行组件。\n"
        "已有安装可升级或修复，无需先卸载。修改安装目录前请备份原有数据。\n"
        "支持 Windows 10/11 x64；微信版本 4.1.8.27；需要 Edge WebView2。\n"
        "本安装程序未做 Windows 代码签名；文件完整性由公开 SHA-256 校验。\n"
    )
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        archive.write(setup, setup.name)
        archive.writestr("安装说明.txt", instructions.encode("utf-8-sig"))
    digest = sha256_file(archive_path)
    named = archive_path.with_name(f"{archive_path.stem}-{digest[:12].lower()}.zip")
    archive_path.rename(named)
    checksum = named.with_suffix(".zip.sha256")
    checksum.write_text(f"{digest}  {named.name}\n", encoding="ascii")
    return named, checksum


def public_verify(url: str, path: Path) -> None:
    # Hash the actual public object, not only user-supplied OSS metadata.
    import hashlib
    digest = hashlib.sha256()
    size = 0
    with urllib.request.urlopen(url, timeout=60) as response:
        if response.status != 200:
            raise ValueError("OSS 公网下载失败")
        while block := response.read(1024 * 1024):
            digest.update(block)
            size += len(block)
    if size != path.stat().st_size or digest.hexdigest().upper() != sha256_file(path):
        raise ValueError(f"OSS 公网下载校验不一致：{path.name}")


def upload(storage, files, executable: str, credential_config: Path | None) -> None:
    for category, path, content_type in files:
        key = storage.object_key(category, path.name)
        command = [executable, "cp", str(path), storage.object_uri(key), "-e", storage.endpoint,
                   "--region", storage.region,
                   "--content-type", content_type,
                   "--cache-control", "public,max-age=31536000,immutable",
                   "--metadata", f"sha256={sha256_file(path)}",
                   "--force"]
        if category == "downloads":
            command += ["--content-disposition", f'attachment; filename="{path.name}"']
        if credential_config:
            command += ["--config-file", str(credential_config)]
        subprocess.run(command, check=True, cwd=ROOT)
        acl_command = [executable, "api", "put-object-acl",
                       "--bucket", storage.bucket, "--key", key,
                       "--object-acl", "public-read", "-e", storage.endpoint,
                       "--region", storage.region]
        if credential_config:
            acl_command += ["--config-file", str(credential_config)]
        # Public read applies only to this completed release object; the bucket
        # remains private. Keeping ACL separate also preserves a successful large
        # upload if public access configuration must be corrected afterwards.
        subprocess.run(acl_command, check=True, cwd=ROOT)
        public_verify(storage.public_url(category, path.name), path)


def deploy_page(metadata: Path, args) -> None:
    publisher._safe_remote_inputs(args.host, args.remote_root)
    nonce = uuid.uuid4().hex
    remote = args.remote_root
    previous = f"{remote}/manifests/native-download-{nonce}"
    publisher._ssh(args.host, f"set -eu; test -f {shlex.quote(remote + '/site/index.html')}; test -f {shlex.quote(remote + '/download.json')}; mkdir -p {shlex.quote(previous)}; cp {shlex.quote(remote + '/site/index.html')} {shlex.quote(previous + '/index.html')}; cp {shlex.quote(remote + '/download.json')} {shlex.quote(previous + '/download.json')}")
    page = ROOT / "deploy" / "download-site" / "index.html"
    try:
        for path, target in ((page, "site/index.html"), (metadata, "download.json")):
            temporary = f"{remote}/{target}.upload-{nonce}"
            publisher._scp(path, args.host, temporary)
            publisher._ssh(args.host, f"set -eu; printf '%s  %s\\n' {sha256_file(path)} {shlex.quote(temporary)} | sha256sum -c -; mv -f {shlex.quote(temporary)} {shlex.quote(remote + '/' + target)}")
        full._public_page(args.base_url.rstrip("/") + "/", timeout=30)
        public_verify(args.base_url.rstrip("/") + "/download.json", metadata)
    except Exception:
        publisher._ssh(args.host, f"set -eu; cp {shlex.quote(previous + '/index.html')} {shlex.quote(remote + '/site/index.html')}; cp {shlex.quote(previous + '/download.json')} {shlex.quote(remote + '/download.json')}")
        raise


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--manager-build", type=Path)
    parser.add_argument("--build-python", type=Path)
    parser.add_argument("--wechat-exe", type=Path)
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts" / f"native-download-{datetime.now():%Y%m%d-%H%M%S}")
    parser.add_argument("--prepared", type=Path, help="publish a verified plan without rebuilding")
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--title", required=True)
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--yes", action="store_true")
    parser.add_argument("--host", default="root@47.242.208.64")
    parser.add_argument("--remote-root", default="/srv/wechatrebot-updates")
    parser.add_argument("--base-url", default="https://ss.xiuxianjyj.xin/wechatrebot-updates")
    parser.add_argument("--ossutil")
    parser.add_argument("--credential-config", type=Path)
    args = parser.parse_args(argv)
    if not args.prepare_only and not args.yes:
        parser.error("上传和更新下载页需要 --yes")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "artifacts") or output == ROOT / "artifacts":
        parser.error("output must be a child of artifacts")
    path = args.prepared.resolve() if args.prepared else prepare(args, output)
    plan = json.loads(path.read_text(encoding="utf-8"))
    if plan.get("schema_version") != 1 or plan.get("smoke_passed") is not True:
        parser.error("发布计划缺少完整安装验收记录")
    files = {key: verify_record(plan[key]) for key in ("payload", "manager", "setup", "archive", "checksum", "metadata")}
    distribution_payload(files["payload"], files["manager"], plan["version"])
    metadata = json.loads(files["metadata"].read_text(encoding="utf-8"))
    if metadata["title"] != args.title or metadata["notes"] != args.note:
        parser.error("标题与说明必须匹配已准备的发布计划")
    if args.prepare_only:
        print(json.dumps({"prepared": str(path), "archive": plan["archive"], "payload": plan["payload"], "published": False}, ensure_ascii=False, indent=2))
        return 0
    storage = storage_config()
    expected_url = storage.public_url("downloads", files["archive"].name)
    if metadata["download"]["url"] != expected_url or metadata["installer"]["payload_url"] != storage.public_url("releases", files["payload"].name):
        parser.error("发布计划 OSS 地址与当前 Bucket 配置不匹配")
    upload(storage, [("releases", files["payload"], "application/octet-stream"),
                     ("downloads", files["archive"], "application/zip"),
                     ("downloads", files["checksum"], "text/plain")],
           args.ossutil or oss.find_ossutil(), args.credential_config)
    deploy_page(files["metadata"], args)
    print(json.dumps({"published": True, "page": args.base_url + "/", "download": expected_url}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
