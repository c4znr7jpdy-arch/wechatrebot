"""Bounded multipart intake and immutable GitHub upload of locally signed candidates."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile

from aiohttp import BodyPartReader, web

from wechatrebot_manager.updates import MAX_MANIFEST_BYTES
from .github import GitHubError

MAX_UPLOAD_BYTES = 4 * 1024**3
MAX_METADATA_BYTES = 1024 * 1024
UPLOAD_CHUNK_BYTES = 64 * 1024
UPLOAD_SECONDS = 3600
DISK_RESERVE_BYTES = 64 * 1024**2
UPLOAD_ROUTE = "/releases/upload"
_HASH = re.compile(r"[0-9a-fA-F]{64}")
_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def _artifact(value: object) -> dict:
    if not isinstance(value, dict):
        raise GitHubError("发布候选记录缺少文件信息。")
    raw = value.get("path", "")
    if not isinstance(raw, str):
        raise GitHubError("发布候选记录文件路径无效。")
    # Windows publisher paths are metadata only, never server filesystem targets.
    path = PurePosixPath(raw.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} or not _NAME.fullmatch(part) for part in path.parts):
        raise GitHubError("发布候选记录包含越界文件路径。")
    if not path.parts or type(value.get("size")) is not int or value["size"] <= 0 or not _HASH.fullmatch(str(value.get("sha256", ""))):
        raise GitHubError("发布候选记录文件大小或摘要无效。")
    return {"name": path.name, "size": value["size"], "sha256": value["sha256"].lower()}


def validate_candidate(github, candidate: dict, manifest_raw: bytes) -> tuple[object, dict[str, object]]:
    if (not isinstance(candidate, dict) or candidate.get("schema_version") != 1
            or candidate.get("repo") != github.repo or not re.fullmatch(r"[0-9a-f]{40}", str(candidate.get("target", "")))):
        raise GitHubError("发布候选记录的仓库、格式或源提交无效。请使用本地发布入口生成的 publish-candidate.json。")
    manifest_info = _artifact(candidate.get("manifest"))
    if manifest_info["size"] != len(manifest_raw) or manifest_info["sha256"] != hashlib.sha256(manifest_raw).hexdigest():
        raise GitHubError("签名清单与发布候选记录的大小或 SHA-256 不一致。")
    value, release = github._manifest(manifest_raw, str(candidate.get("version", "")))
    if candidate.get("version") != release.version:
        raise GitHubError("候选记录版本与签名清单不一致。")
    packages = {}
    for package in github.manifest_packages(value):
        name = package.package_url.rsplit("/", 1)[1]
        if name in packages and packages[name] != package:
            raise GitHubError("签名清单包含冲突的同名更新包。")
        packages[name] = package
    assets = candidate.get("assets")
    if not isinstance(assets, list) or not 1 <= len(assets) <= 200:
        raise GitHubError("发布候选记录资产列表无效。")
    declared = {}
    for asset in assets:
        item = _artifact(asset)
        if item["name"] in declared:
            raise GitHubError("发布候选记录包含重复文件名。")
        declared[item["name"]] = item
    for name, package in packages.items():
        if declared.get(name) != {"name": name, "size": package.package_size, "sha256": package.package_sha256.lower()}:
            raise GitHubError("更新包与发布候选记录不一致，不能上传。")
    if sum(package.package_size for package in packages.values()) > MAX_UPLOAD_BYTES:
        raise GitHubError("本次更新包总大小超过 4 GiB 网页上传上限，请使用开发机发布入口。")
    if any(name in packages for name in (f"stable-{release.version}.json", "publish-candidate.json")):
        raise GitHubError("更新包文件名与发布元数据冲突。")
    return release, packages


async def _part(reader, name: str):
    part = await asyncio.wait_for(reader.next(), timeout=30)
    if not isinstance(part, BodyPartReader) or part.name != name:
        raise GitHubError("上传字段顺序或文件缺失，请刷新页面重新选择文件。")
    if part.headers.get("Content-Encoding") or part.headers.get("Content-Transfer-Encoding"):
        raise GitHubError("上传字段不允许额外编码。")
    return part


async def _small(part, maximum: int) -> bytes:
    chunks, size = [], 0
    while chunk := await asyncio.wait_for(part.read_chunk(UPLOAD_CHUNK_BYTES), timeout=30):
        size += len(chunk)
        if size > maximum:
            raise GitHubError("上传字段或元数据超过允许大小。")
        chunks.append(chunk)
    return b"".join(chunks)


async def authenticate_upload(request, auth, clock, audit):
    if request.content_type != "multipart/form-data":
        raise web.HTTPBadRequest(text="请选择发布文件。")
    if request.content_length is not None and request.content_length > MAX_UPLOAD_BYTES + 3 * MAX_METADATA_BYTES:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_UPLOAD_BYTES, actual_size=request.content_length)
    reader = await request.multipart()
    csrf_part = await _part(reader, "csrf")
    submitted = (await _small(csrf_part, 256)).decode("utf-8", errors="replace")
    session = request["admin_session"]
    if not submitted.isascii() or not hmac.compare_digest(submitted, session["csrf"]):
        raise web.HTTPForbidden(text="请求已过期，请刷新页面后重试。")
    code = (await _small(await _part(reader, "totp"), 64)).decode("utf-8", errors="replace")
    result = await asyncio.to_thread(auth.authenticate, "", code, int(clock()), step_up=True)
    if result.status != "ok" or result.revision != session["revision"]:
        await audit("upload_totp_locked" if result.status == "locked" else "upload_totp_failed")
        raise GitHubError("动态码无效、已使用或账号已锁定。等待新动态码后重试上传。")
    return reader


async def _write_package(part, path: Path, package) -> None:
    digest, size = hashlib.sha256(), 0
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as handle:
        while chunk := await asyncio.wait_for(part.read_chunk(UPLOAD_CHUNK_BYTES), timeout=30):
            size += len(chunk)
            if size > package.package_size:
                raise GitHubError("上传包超过签名清单约定大小。")
            digest.update(chunk)
            await asyncio.to_thread(handle.write, chunk)
        await asyncio.to_thread(handle.flush)
        await asyncio.to_thread(os.fsync, handle.fileno())
    if size != package.package_size or digest.hexdigest() != package.package_sha256.lower():
        raise GitHubError("上传包大小或 SHA-256 与签名清单不一致。")


@asynccontextmanager
async def receive_candidate(request, reader, github, data_dir: Path, auth, clock):
    """No request.post/read here: metadata bounded in memory, packages bounded on disk."""
    temporary = None
    try:
        async with asyncio.timeout(UPLOAD_SECONDS):
            candidate_raw = await _small(await _part(reader, "candidate"), MAX_METADATA_BYTES)
            try:
                candidate = json.loads(candidate_raw)
            except (ValueError, UnicodeError):
                raise GitHubError("发布候选记录不是有效 JSON。") from None
            manifest_raw = await _small(await _part(reader, "manifest"), MAX_MANIFEST_BYTES)
            release, packages = validate_candidate(github, candidate, manifest_raw)
            needed = sum(item.package_size for item in packages.values()) + DISK_RESERVE_BYTES
            if shutil.disk_usage(data_dir).free < needed:
                raise GitHubError("服务器临时磁盘空间不足，尚未接收更新包。")
            temporary = Path(tempfile.mkdtemp(prefix=".admin-upload-", dir=data_dir))
            os.chmod(temporary, 0o700)
            paths = {}
            for _ in range(len(packages)):
                part = await _part(reader, "packages")
                name = part.filename or ""
                if name not in packages or name in paths:
                    raise GitHubError("更新包文件名不在签名清单中，或被重复选择。")
                path = temporary / name
                await _write_package(part, path, packages[name])
                paths[name] = path
            # Do not let MultipartReader auto-drain an arbitrary unrecognized trailing file.
            if await asyncio.wait_for(reader.next(), timeout=30) is not None:
                raise GitHubError("选择了清单之外的文件，请仅选择要求的全部 ZIP。")
            if await asyncio.to_thread(auth.session, request["admin_token"], int(clock())) is None:
                raise GitHubError("上传期间登录已失效，请重新登录。未向 GitHub 写入。")
        yield candidate, manifest_raw, paths, release
    except TimeoutError:
        raise GitHubError("上传超时，已清理临时文件。请检查网络后重新选择文件上传。") from None
    finally:
        if temporary is not None:
            await asyncio.to_thread(shutil.rmtree, temporary)


async def _source_ready(github, target: str) -> None:
    # The repository may be private; source and CI checks use the configured
    # repository token. Public, unauthenticated reads are reserved for assets.
    comparison = await github._api("GET", f"/compare/{target}...master")
    if comparison.get("status") not in {"ahead", "identical"}:
        raise GitHubError("候选源提交尚未合入 master，不能发布。")
    checks = await github._api("GET", f"/actions/workflows/quality.yml/runs?head_sha={target}&per_page=10")
    runs = [item for item in checks.get("workflow_runs", [])
            if item.get("head_sha") == target and item.get("event") in {"push", "workflow_dispatch"}]
    if not runs:
        raise GitHubError("候选源提交缺少 quality CI 记录，不能发布。")
    latest = max(runs, key=lambda item: (item.get("run_number", 0), item.get("run_attempt", 0)))
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise GitHubError("候选源提交的最新 quality CI 未通过，不能发布。")


async def _tag_matches(github, version: str, target: str, *, missing_ok: bool):
    commit = await github._api("GET", f"/commits/v{version}", missing_ok=missing_ok)
    if (commit is None and not missing_ok) or (commit is not None and commit.get("sha") != target):
        raise GitHubError("GitHub 同版本标签已指向不同源提交，禁止覆盖。")


def _same_asset(remote: dict, size: int, digest: str) -> bool:
    return remote.get("state", "uploaded") == "uploaded" and remote.get("size") == size and remote.get("digest", "").lower() == "sha256:" + digest.lower()


async def publish_candidate(github, candidate: dict, manifest_raw: bytes, package_paths: dict[str, Path]) -> dict:
    release, packages = validate_candidate(github, candidate, manifest_raw)
    if set(package_paths) != set(packages):
        raise GitHubError("尚未选择签名清单要求的全部更新包。")
    version, target = release.version, candidate["target"]
    # Recheck the private temporary files immediately before any remote write.
    for name, package in packages.items():
        digest = hashlib.sha256()
        with package_paths[name].open("rb") as handle:
            while chunk := await asyncio.to_thread(handle.read, UPLOAD_CHUNK_BYTES):
                digest.update(chunk)
        if package_paths[name].stat().st_size != package.package_size or digest.hexdigest() != package.package_sha256.lower():
            raise GitHubError("服务器暂存包发生变化，未向 GitHub 写入。")
    async with github.lock:
        if (github.directory / "admin-release-recovery.json").exists():
            raise GitHubError("存在未完成的稳定清单恢复，请先在发布页完成恢复后再上传。")
        await _source_ready(github, target)
        await _tag_matches(github, version, target, missing_ok=True)
        tag_path = f"/releases/tags/v{version}"
        existing = await github._api("GET", tag_path, missing_ok=True)
        if existing is None:
            existing = await github._api("POST", "/releases", payload={"tag_name": f"v{version}",
                "target_commitish": target, "name": f"{version} 更新包", "draft": True, "prerelease": False,
                "make_latest": "false", "body": "已签名客户端更新包。维护者须在网页后台另行确认切换稳定版本。"})
        if existing.get("tag_name") != f"v{version}" or existing.get("prerelease"):
            raise GitHubError("同版本 Release 状态不兼容，禁止覆盖。")
        if existing.get("draft") and existing.get("target_commitish") != target:
            raise GitHubError("同版本草稿的源提交不同，禁止覆盖。")
        if not existing.get("draft"):
            await _tag_matches(github, version, target, missing_ok=False)
        release_id = int(existing["id"])
        manifest_name = f"stable-{version}.json"
        marker = json.dumps({"schema_version": 1, "version": version, "repo": github.repo, "target": target,
            "manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(), "packages": [
                {"name": name, "size": item.package_size, "sha256": item.package_sha256.lower()}
                for name, item in sorted(packages.items())]}, sort_keys=True, separators=(",", ":")).encode("utf-8")
        metadata = {"publish-candidate.json": marker, manifest_name: manifest_raw}
        # Check every collision before uploading anything; immutable public assets are never clobbered.
        expected = {name: (len(raw), hashlib.sha256(raw).hexdigest()) for name, raw in metadata.items()}
        expected.update({name: (item.package_size, item.package_sha256) for name, item in packages.items()})
        remote = {item["name"]: item for item in existing.get("assets", [])}
        if len(remote) != len(existing.get("assets", [])):
            raise GitHubError("Release 存在重复资产名，无法安全恢复。")
        interrupted = set()
        marker_matches = ("publish-candidate.json" in remote and _same_asset(
            remote["publish-candidate.json"], len(marker), hashlib.sha256(marker).hexdigest()))
        for name, (size, digest) in expected.items():
            if name in remote and not _same_asset(remote[name], size, digest):
                if existing.get("draft") and marker_matches and remote[name].get("state") == "starter" and remote[name].get("size") == 0:
                    interrupted.add(name)
                    continue
                raise GitHubError(f"GitHub 已有同名但内容不同的资产：{name}；禁止覆盖，请核对原候选。")
        for name, (size, digest) in expected.items():
            if name in interrupted:
                await github._api("DELETE", f"/releases/assets/{int(remote[name]['id'])}")
            elif name in remote:
                continue
            args = {"file_path": package_paths[name]} if name in packages else {"content": metadata[name]}
            uploaded = await github._api("POST", f"/releases/{release_id}/assets?name={name}", **args)
            if not _same_asset(uploaded, size, digest):
                raise GitHubError("GitHub 返回的上传资产摘要不符，尚未切换稳定清单。")
        if existing.get("draft"):
            await github._api("PATCH", f"/releases/{release_id}", payload={"draft": False, "make_latest": "false"})
        await _tag_matches(github, version, target, missing_ok=False)
        # Both new uploads and reused assets must pass public, unauthenticated full verification.
        for name, package in packages.items():
            await github._download(package.package_url, maximum=package.package_size,
                expected_size=package.package_size, expected_sha256=package.package_sha256)
        public_manifest = await github._download(
            f"https://github.com/{github.repo}/releases/download/v{version}/{manifest_name}", maximum=MAX_MANIFEST_BYTES)
        github._manifest(public_manifest, version)
        if public_manifest != manifest_raw:
            raise GitHubError("公网版本清单与上传内容不一致，尚未切换稳定清单。")
        return {"version": version, "source_commit": target, "package_count": len(packages), "stable_changed": False}
