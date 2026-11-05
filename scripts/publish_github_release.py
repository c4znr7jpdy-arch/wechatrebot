"""Publish customer packages, payloads, update ZIPs and the signed stable manifest to GitHub Releases."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import re
import shutil
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from packaging.version import InvalidVersion, Version

COMMERCIAL_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = COMMERCIAL_ROOT / "scripts"
SOURCE_ROOT = COMMERCIAL_ROOT / "src"
for search_root in (SCRIPTS_ROOT, SOURCE_ROOT):
    if str(search_root) not in sys.path:
        sys.path.insert(0, str(search_root))

import publish_full_update as full
from source_inputs import verify_source_inputs
import publish_manager_update as publisher
from build_update_release import attach_incremental_payloads
from wechatrebot_manager.payload import sha256_file
from wechatrebot_manager.updates import (
    UpdateChannel,
    UpdateError,
    canonical_manifest_bytes,
    load_update_channel,
    manifest_package_releases,
    parse_update_manifest,
    prepare_update,
)

STABLE_TAG = "stable"
STABLE_MANIFEST_NAME = "stable.json"
_REPO_PATTERN = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")


def release_tag(version: str) -> str:
    try:
        return f"v{Version(version)}"
    except InvalidVersion as exc:
        raise ValueError(f"版本号无效：{version}") from exc


def _repo(repo: str) -> str:
    if not _REPO_PATTERN.fullmatch(repo):
        raise ValueError(f"GitHub 仓库名无效：{repo}")
    return repo


def asset_prefix(repo: str, tag: str) -> str:
    return f"https://github.com/{_repo(repo)}/releases/download/{tag}"


def asset_url(repo: str, tag: str, name: str) -> str:
    return f"{asset_prefix(repo, tag)}/{name}"


def stable_manifest_url(repo: str) -> str:
    return asset_url(repo, STABLE_TAG, STABLE_MANIFEST_NAME)


_USER_AGENT = "WeChatReBot-Publisher/2"


def _safe_error(error: object) -> str:
    text = str(error)
    for name in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(name):
            text = text.replace(os.environ[name], "[redacted]")
    text = re.sub(r"(?:github_pat_|gh[pousr]_)[A-Za-z0-9_]+", "[redacted]", text)
    text = re.sub(r"(https://[^\s?]+)\?[^\s]+", r"\1?[redacted]", text)
    return text[-2000:]


def _transient(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {403, 404, 408, 429, 500, 502, 503, 504}
    if isinstance(error, urllib.error.URLError):
        return _transient(error.reason) if isinstance(error.reason, Exception) else False
    return isinstance(error, (TimeoutError, ConnectionError, ssl.SSLEOFError,
                              http.client.IncompleteRead, http.client.RemoteDisconnected))


def _final_https_url(response, request_url: str) -> str:
    final = response.geturl()
    parsed = urllib.parse.urlsplit(final)
    if parsed.scheme.casefold() != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError(f"公网资产最终地址不是 HTTPS：{request_url}")
    if response.status != 200:
        raise ValueError(f"公网资产返回 HTTP {response.status}：{request_url}")
    return final


def _request(url: str, method: str = "GET") -> urllib.request.Request:
    return urllib.request.Request(
        url, method=method,
        headers={"Cache-Control": "no-cache", "User-Agent": _USER_AGENT, "Accept": "*/*"},
    )


def verify_public_asset(
    url: str, *, expected_size: int, expected_sha256: str, timeout: int,
    attempts: int = 3, delay: float = 5.0, sleep=time.sleep,
) -> str:
    """HEAD then GET the asset through GitHub's 302, comparing size and SHA-256. Returns the final URL."""
    failure: Exception | None = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(_request(url, "HEAD"), timeout=timeout) as response:
                final = _final_https_url(response, url)
                length = response.headers.get("Content-Length")
                if int(length or 0) != expected_size:
                    raise ValueError(f"公网资产大小与本地产物不一致：{url}")
            digest = hashlib.sha256()
            size = 0
            with urllib.request.urlopen(_request(url), timeout=timeout) as response:
                _final_https_url(response, url)
                while chunk := response.read(4 * 1024 * 1024):
                    size += len(chunk)
                    digest.update(chunk)
            if size != expected_size:
                raise ValueError(f"公网资产下载大小与本地产物不一致：{url}")
            if digest.hexdigest().lower() != expected_sha256.lower():
                raise ValueError(f"公网资产 SHA-256 与本地产物不一致：{url}")
            return final
        except (OSError, http.client.HTTPException) as exc:
            failure = ValueError(f"公网资产访问失败：{url}；{_safe_error(exc)}")
            if not _transient(exc) or attempt + 1 >= attempts:
                raise failure from exc
            sleep(delay)
    raise failure or ValueError(f"公网资产校验失败：{url}")


class GitHubCommandError(RuntimeError):
    pass


def _gh(argv: list[str]) -> str:
    command = ["gh", *argv]
    print("+", _safe_error(subprocess.list2cmdline(command)), flush=True)
    # Only retry reads. A timed-out write may already have succeeded remotely.
    readonly = argv[:2] in (["release", "view"], ["release", "download"], ["repo", "view"])
    for attempt in range(3):
        try:
            completed = subprocess.run(command, cwd=COMMERCIAL_ROOT, check=True, capture_output=True,
                                       text=True, encoding="utf-8", errors="replace")
            return completed.stdout
        except subprocess.CalledProcessError as exc:
            detail = _safe_error(exc.stderr or exc.stdout or f"退出码 {exc.returncode}")
            retryable = re.search(r"timeout|timed out|TLS|EOF|connection reset|HTTP (?:429|50[0234])", detail, re.I)
            if readonly and retryable and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            raise GitHubCommandError(f"GitHub {argv[0]} {argv[1] if len(argv) > 1 else ''} 失败：{detail}") from None
    raise AssertionError("unreachable")


def repo_name_with_owner() -> str:
    payload = json.loads(_gh(["repo", "view", "--json", "nameWithOwner"]))
    return _repo(str(payload["nameWithOwner"]))


def ensure_gh_ready(repo: str) -> None:
    _gh(["auth", "status"])
    payload = json.loads(_gh(["repo", "view", repo, "--json", "viewerPermission"]))
    if payload.get("viewerPermission") not in {"ADMIN", "MAINTAIN", "WRITE"}:
        raise ValueError(f"当前 gh 账号对 {repo} 没有发布权限")


def release_exists(repo: str, tag: str) -> bool:
    try:
        _gh(["release", "view", tag, "-R", repo, "--json", "tagName"])
    except (GitHubCommandError, subprocess.CalledProcessError) as exc:
        detail = str(getattr(exc, "stderr", "") or exc)
        if re.search(r"release not found|HTTP 404|404 Not Found", detail, re.I):
            return False
        raise
    return True


def create_release(repo: str, tag: str, *, title: str, notes_file: Path, target: str, latest: bool) -> None:
    argv = ["release", "create", tag, "-R", repo, "--draft", "--title", title,
            "--notes-file", str(notes_file), "--target", target]
    if not latest:
        argv.append("--latest=false")
    _gh(argv)


def upload_assets(repo: str, tag: str, paths: list[Path], *, clobber: bool = True) -> None:
    if not paths:
        return
    _gh(["release", "upload", tag, *(str(path) for path in paths), "-R", repo,
         *(["--clobber"] if clobber else [])])


def publish_release(repo: str, tag: str) -> None:
    _gh(["release", "edit", tag, "-R", repo, "--draft=false"])


def download_asset(repo: str, tag: str, name: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _gh(["release", "download", tag, "-R", repo, "--pattern", name, "--dir", str(directory), "--clobber"])
    return directory / name


def fetch_stable_manifest(channel: UpdateChannel) -> dict | None:
    try:
        return publisher._fetch_live_manifest(channel)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return None
        raise


def _load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict):
        raise ValueError(f"清单根节点不是对象：{path}")
    return payload


def _verify_signed_manifest(payload: dict, channel: UpdateChannel, version: str) -> None:
    try:
        parsed = parse_update_manifest(payload, channel, current_version="0.0.0")
    except (UpdateError, ValueError) as exc:
        raise ValueError(f"清单签名校验失败：{exc}") from exc
    if parsed is None or parsed.version != str(Version(version)):
        raise ValueError(f"清单签名校验失败或版本不是 {version}")


def verify_stable_manifest(channel: UpdateChannel, expected: dict, *, attempts: int = 10,
                           delay: float = 6.0, sleep=time.sleep) -> None:
    for attempt in range(attempts):
        try:
            live = fetch_stable_manifest(channel)
        except (OSError, http.client.HTTPException) as exc:
            if not _transient(exc) or attempt + 1 == attempts:
                raise
            live = None
        if (live is not None and canonical_manifest_bytes(live) == canonical_manifest_bytes(expected)
                and live.get("signature") == expected.get("signature")):
            return
        if attempt + 1 < attempts:
            sleep(delay)
    raise ValueError("公网稳定清单与刚上传的清单不一致")


def ensure_stable_release(repo: str, *, target: str, work_root: Path) -> None:
    if release_exists(repo, STABLE_TAG):
        state = json.loads(_gh(["release", "view", STABLE_TAG, "-R", repo, "--json", "isDraft"]))
        if state.get("isDraft"):
            publish_release(repo, STABLE_TAG)
        return
    work_root.mkdir(parents=True, exist_ok=True)
    notes = work_root / "stable-notes.md"
    notes.write_text("客户端读取的签名稳定清单。请勿手工修改此 Release 的资产。\n", encoding="utf-8")
    create_release(repo, STABLE_TAG, title="稳定清单", notes_file=notes, target=target, latest=False)
    publish_release(repo, STABLE_TAG)


def promote_manifest(repo: str, version: str, manifest_path: Path, *, channel: UpdateChannel, target: str,
                     work_root: Path, attempts: int = 10, delay: float = 6.0, sleep=time.sleep) -> None:
    """Keep a per-version signed copy, then overwrite stable/stable.json and read it back."""
    manifest = _load_manifest(manifest_path)
    _verify_signed_manifest(manifest, channel, version)
    work_root.mkdir(parents=True, exist_ok=True)
    tag = release_tag(version)
    versioned = work_root / f"stable-{Version(version)}.json"
    shutil.copyfile(manifest_path, versioned)
    # The immutable per-version copy was uploaded and checked with the other candidate assets.
    verify_public_asset(asset_url(repo, tag, versioned.name), expected_size=versioned.stat().st_size,
                        expected_sha256=sha256_file(versioned), timeout=channel.request_timeout_seconds)
    ensure_stable_release(repo, target=target, work_root=work_root)
    stable_copy = work_root / STABLE_MANIFEST_NAME
    shutil.copyfile(manifest_path, stable_copy)
    upload_assets(repo, STABLE_TAG, [stable_copy])
    verify_stable_manifest(channel, manifest, attempts=attempts, delay=delay, sleep=sleep)


def rollback_stable(repo: str, version: str, *, channel: UpdateChannel, work_root: Path,
                    attempts: int = 10, delay: float = 6.0, sleep=time.sleep) -> None:
    tag = release_tag(version)
    name = f"stable-{Version(version)}.json"
    downloaded = download_asset(repo, tag, name, work_root)
    manifest = _load_manifest(downloaded)
    _verify_signed_manifest(manifest, channel, version)
    for package in manifest_package_releases(manifest, channel):
        verify_public_asset(package.package_url, expected_size=package.package_size,
                            expected_sha256=package.package_sha256, timeout=channel.request_timeout_seconds)
    stable_copy = work_root / STABLE_MANIFEST_NAME
    shutil.copyfile(downloaded, stable_copy)
    upload_assets(repo, STABLE_TAG, [stable_copy])
    verify_stable_manifest(channel, manifest, attempts=attempts, delay=delay, sleep=sleep)


def _project_version() -> str:
    return full._project_version()


def _run(argv: list[str], *, env: dict[str, str] | None = None) -> None:
    full._run(argv, env=env)


def _git_head() -> str:
    completed = subprocess.run(["git", "rev-parse", "HEAD"], cwd=COMMERCIAL_ROOT, check=True,
                               capture_output=True, text=True, encoding="utf-8")
    return completed.stdout.strip()


def _write_checksum(path: Path) -> Path:
    checksum = path.with_name(path.name + ".sha256")
    checksum.write_text(f"{sha256_file(path)}  {path.name}\n", encoding="ascii")
    return checksum


def _write_notes(path: Path, *, title: str, notes: tuple[str, ...], version: str) -> Path:
    lines = [f"# {title.strip() or f'版本 {version}'}", ""]
    lines.extend(f"- {note.strip()}" for note in notes if note.strip())
    lines.extend([
        "",
        "安装：下载 `WeChatReBot-*-online-*.zip`，解压后运行其中的安装程序；"
        "安装时会从本 Release 下载 `payload.wrb` 并校验 SHA-256。",
        "已安装的管理器会通过签名清单自动获得本次更新。",
    ])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def smoke_update_from_github(channel: UpdateChannel, release, smoke_root: Path) -> None:
    """Act like an installed manager: read the manifest, download the package, check the digest."""
    live = fetch_stable_manifest(channel)
    parsed = parse_update_manifest(live or {}, channel, current_version="0.0.0")
    if parsed is None or parsed.version != release.version or parsed.package_sha256 != release.package_sha256:
        raise ValueError("公网稳定清单未指向刚发布的更新包")
    prepared = prepare_update(parsed, channel, smoke_root)
    if sha256_file(prepared.archive_path) != release.package_sha256:
        raise ValueError("从 GitHub 下载的更新包摘要不一致")
    if not (prepared.payload_root / "manager" / "WeChatReBot.exe").is_file():
        raise ValueError("从 GitHub 下载的更新包缺少管理器")


def _verify_release_target(repo: str, tag: str, target: str, state: dict | None = None) -> None:
    state = state or json.loads(_gh(["release", "view", tag, "-R", repo,
                                    "--json", "isDraft,targetCommitish"]))
    ref = str(state.get("targetCommitish", "")) if state.get("isDraft") else tag
    if not ref:
        raise ValueError("Release 缺少目标提交")
    actual = _gh(["api", f"repos/{_repo(repo)}/commits/{urllib.parse.quote(ref, safe='')}",
                  "--jq", ".sha"]).strip()
    if actual != target:
        raise ValueError(f"Release 标签/目标提交与原候选不一致：{tag}；拒绝发布")


def _verify_web_candidate(marker: Path, *, assets: list[Path], repo: str,
                          version: str, target: str, channel: UpdateChannel) -> None:
    """Permit web-to-local resume only for the same signed package set and commit."""
    manifest_path = next((p for p in assets if p.name == f"stable-{version}.json"), None)
    if manifest_path is None or marker.stat().st_size > 1024 * 1024:
        raise ValueError("网页候选缺少本地签名清单或标记文件过大")
    manifest = _load_manifest(manifest_path)
    _verify_signed_manifest(manifest, channel, version)
    packages = {}
    prefix = asset_prefix(repo, release_tag(version)) + "/"
    for package in manifest_package_releases(manifest, channel):
        name = package.package_url.removeprefix(prefix)
        if not package.package_url.startswith(prefix) or not re.fullmatch(r"[A-Za-z0-9_.-]+\.zip", name):
            raise ValueError("网页候选包地址不属于该版本 Release")
        if name in packages and packages[name] != package:
            raise ValueError("网页候选包文件名冲突")
        packages[name] = package
    expected = {"schema_version": 1, "version": version, "repo": repo, "target": target,
                "manifest_sha256": sha256_file(manifest_path).lower(), "packages": [
                    {"name": name, "size": package.package_size, "sha256": package.package_sha256.lower()}
                    for name, package in sorted(packages.items())]}
    if _load_manifest(marker) != expected:
        raise ValueError("网页候选与本地签名包或目标提交不同，拒绝混合恢复")


def _publish_assets(repo: str, tag: str, *, title: str, notes: tuple[str, ...], version: str,
                    assets: list[Path], work_root: Path, timeout: int, resume: bool = False,
                    target: str | None = None, channel: UpdateChannel | None = None) -> None:
    exists = release_exists(repo, tag)
    if exists and not resume:
        raise ValueError(f"Release {tag} 已存在；请用 --resume --output 原产物目录恢复同一候选")
    names: set[str] = set()
    unique: list[Path] = []
    for path in assets:
        if path.name not in names:
            names.add(path.name)
            unique.append(path)
    checksums = [_write_checksum(path) for path in unique
                 if not path.name.endswith(".sha256") and f"{path.name}.sha256" not in names]
    notes_file = _write_notes(work_root / "release-notes.md", title=title, notes=notes, version=version)
    all_assets = [*unique, *checksums]
    pending = list(all_assets)
    if exists:
        remote = json.loads(_gh(["release", "view", tag, "-R", repo, "--json", "assets,isDraft,targetCommitish"]))
        _verify_release_target(repo, tag, target or _git_head(), remote)
        remote_assets = {a["name"]: a for a in remote.get("assets", [])}
        unexpected = set(remote_assets) - {p.name for p in all_assets}
        if "publish-candidate.json" in unexpected:
            if remote_assets["publish-candidate.json"].get("size", 1024 * 1024 + 1) > 1024 * 1024:
                raise ValueError("网页候选标记文件过大")
            marker = download_asset(repo, tag, "publish-candidate.json", work_root / "remote-check")
            _verify_web_candidate(marker, assets=unique, repo=repo, version=version,
                                  target=target or _git_head(), channel=channel or load_update_channel())
            unexpected.remove("publish-candidate.json")
        if unexpected:
            raise ValueError(f"远端 Release 含本候选之外的资产，拒绝混合发布：{', '.join(sorted(unexpected))}")
        for path in all_assets:
            if path.name not in remote_assets:
                continue
            if remote_assets[path.name].get("size") != path.stat().st_size:
                raise ValueError(f"远端同名资产大小不同，拒绝覆盖：{path.name}")
            downloaded = download_asset(repo, tag, path.name, work_root / "remote-check")
            if sha256_file(downloaded) != sha256_file(path):
                raise ValueError(f"远端同名资产 SHA-256 不同，拒绝覆盖：{path.name}")
            pending.remove(path)
    else:
        create_release(repo, tag, title=f"{Version(version)} {title.strip()}".strip(), notes_file=notes_file,
                       target=target or _git_head(), latest=True)
    # An interrupted upload is recovered by re-reading the remote inventory, never by blindly replacing it.
    upload_assets(repo, tag, pending, clobber=False)
    publish_release(repo, tag)
    _verify_release_target(repo, tag, target or _git_head())
    for path in unique:
        if path.name.endswith(".sha256"):
            continue
        verify_public_asset(asset_url(repo, tag, path.name), expected_size=path.stat().st_size,
                            expected_sha256=sha256_file(path), timeout=timeout)
        print(f"GitHub 公网校验通过：{asset_url(repo, tag, path.name)}", flush=True)


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _save_candidate(output: Path, release, assets: list[Path], *, repo: str, target: str,
                    title: str, notes: tuple[str, ...], previous: dict | None) -> dict:
    def artifact(path: Path) -> dict:
        return {"path": str(path.resolve().relative_to(output)), "size": path.stat().st_size,
                "sha256": sha256_file(path)}
    candidate = {"schema_version": 1, "version": release.version, "repo": repo, "target": target,
                 "title": title, "notes": list(notes), "previous_manifest": previous,
                 "manifest": artifact(release.manifest_path), "assets": [artifact(p) for p in assets]}
    _write_json(output / "publish-candidate.json", candidate)
    return candidate


def _load_candidate(output: Path, *, repo: str, version: str, channel: UpdateChannel):
    candidate = _load_manifest(output / "publish-candidate.json")
    if (candidate.get("schema_version") != 1 or candidate.get("repo") != repo
            or candidate.get("version") != version):
        raise ValueError("恢复候选与当前仓库或项目版本不一致")
    def verified_path(item: dict) -> Path:
        path = (output / item["path"]).resolve()
        if output not in path.parents or not path.is_file():
            raise ValueError("恢复候选路径越界或文件丢失")
        if path.stat().st_size != item["size"] or sha256_file(path) != item["sha256"]:
            raise ValueError(f"恢复候选文件已改变：{path.name}")
        return path
    manifest_path = verified_path(candidate["manifest"])
    manifest = _load_manifest(manifest_path)
    _verify_signed_manifest(manifest, channel, version)
    parsed = parse_update_manifest(manifest, channel, current_version="0.0.0")
    assets = [verified_path(item) for item in candidate["assets"]]
    expected_url = asset_prefix(repo, release_tag(version)) + "/"
    for package in manifest_package_releases(manifest, channel):
        if not package.package_url.startswith(expected_url):
            raise ValueError("恢复候选更新地址不属于该版本 Release")
        asset = next((p for p in assets if asset_url(repo, release_tag(version), p.name) == package.package_url), None)
        if (asset is None or asset.stat().st_size != package.package_size
                or sha256_file(asset) != package.package_sha256):
            raise ValueError("恢复候选更新包与签名清单不一致")
    archive = next((p for p in assets if asset_url(repo, release_tag(version), p.name) == parsed.package_url), None)
    if (archive is None or archive.stat().st_size != parsed.package_size
            or sha256_file(archive) != parsed.package_sha256):
        raise ValueError("恢复候选更新包与签名清单不一致")
    previous = candidate.get("previous_manifest")
    if previous:
        _verify_signed_manifest(previous, channel, previous["version"])
    release = full.BuiltFullUpdate(version=version, archive_path=archive, manifest_path=manifest_path,
                                  package_url=parsed.package_url, package_size=parsed.package_size,
                                  package_sha256=parsed.package_sha256)
    return candidate, release, assets


def _same_manifest(left: dict | None, right: dict | None) -> bool:
    return left == right


def _finish_publication(repo: str, release, *, channel: UpdateChannel, work_root: Path,
                        target: str, previous: dict | None, skip_smoke: bool, receipt: dict) -> None:
    expected = _load_manifest(release.manifest_path)
    receipt_path = work_root.parent / "publication-receipt.json"
    try:
        live = fetch_stable_manifest(channel)
        if not (_same_manifest(live, previous) or _same_manifest(live, expected)):
            raise ValueError("线上稳定清单已被其他发布改变，停止切换；请先核对线上版本")
    except Exception as exc:
        receipt.update(status="failed_before_promotion", error=_safe_error(exc))
        _write_json(receipt_path, receipt)
        raise
    already_promoted = _same_manifest(live, expected)
    try:
        if not already_promoted:
            promote_manifest(repo, release.version, release.manifest_path, channel=channel, target=target,
                             work_root=work_root)
        else:
            verify_stable_manifest(channel, expected)
        if not skip_smoke:
            smoke_update_from_github(channel, release, work_root / "smoke")
    except Exception as exc:
        receipt.update(status="failed", error=_safe_error(exc), rollback="pending")
        if already_promoted:
            receipt["rollback"] = "not_needed_existing_stable_unchanged"
            _write_json(receipt_path, receipt)
            raise ValueError("已上线候选的复核失败；本次未修改线上稳定清单。") from exc
        _write_json(receipt_path, receipt)
        try:
            current = fetch_stable_manifest(channel)
            if not (_same_manifest(current, previous) or _same_manifest(current, expected) or current is None):
                raise ValueError("其他发布已改变稳定清单，拒绝覆盖，需人工核对")
            if previous:
                _verify_signed_manifest(previous, channel, previous["version"])
                backup = work_root / "restore" / STABLE_MANIFEST_NAME
                _write_json(backup, previous)
                upload_assets(repo, STABLE_TAG, [backup])
                verify_stable_manifest(channel, previous)
            else:
                _gh(["release", "delete-asset", STABLE_TAG, STABLE_MANIFEST_NAME, "-R", repo, "--yes"])
                if fetch_stable_manifest(channel) is not None:
                    raise ValueError("首次发布失败后清单仍可访问，需人工核对")
            receipt["rollback"] = "verified"
        except Exception as restore_error:
            receipt.update(rollback="failed", rollback_error=_safe_error(restore_error))
        _write_json(receipt_path, receipt)
        raise ValueError(f"稳定清单切换/冒烟失败；回滚状态：{receipt['rollback']}。详见 {receipt_path}") from exc
    receipt.update(status="published", stable_verified=True, smoke="skipped" if skip_smoke else "passed",
                   completed_at=datetime.now(timezone.utc).isoformat())
    _write_json(receipt_path, receipt)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="构建、签名并发布到 GitHub Release；稳定清单放在 stable 标签。")
    parser.add_argument("--source-root", type=Path, default=COMMERCIAL_ROOT / "vendor")
    parser.add_argument("--title", default="完整载荷更新")
    parser.add_argument("--note", action="append", default=[])
    parser.add_argument("--minimum-supported-version", default="0.7.0")
    parser.add_argument("--build", type=int)
    parser.add_argument("--mandatory", action="store_true")
    parser.add_argument("--private-key", type=Path,
                        default=COMMERCIAL_ROOT / "config" / "local" / "update-signing-private.pem")
    parser.add_argument("--wheelhouse", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--manager-build", type=Path, help="--manager-only 时复用已完成的管理器构建目录")
    parser.add_argument("--manager-only", action="store_true", help="只发布管理器增量包")
    parser.add_argument("--incremental-payload", type=Path, action="append", default=[],
                        help="附加按安装基线生成的组件差异载荷；可重复，每个基线一份")
    parser.add_argument("--rollback", metavar="VERSION", help="把 stable 清单回滚到该版本的签名副本")
    parser.add_argument("--skip-tests", action="store_true")
    parser.add_argument("--skip-remote-smoke", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--resume", action="store_true", help="从 --output 恢复原签名候选和未完成的上传；不重新签名")
    parser.add_argument("--yes", action="store_true")
    return parser


def _build_candidate(args, *, version: str, channel: UpdateChannel, live_version: str,
                     prefix: str, output: Path):
    private_key = args.private_key.expanduser().resolve()
    if not private_key.is_file():
        raise ValueError(f"更新签名私钥不存在：{private_key}")
    publisher._signing_key(private_key, channel)
    tag = release_tag(version)
    repo = _repo(urllib.parse.urlsplit(prefix).path.strip("/").split("/releases/")[0])
    if args.manager_only:
        if not args.skip_tests:
            env = os.environ.copy()
            env["PYTHONPATH"] = str(SOURCE_ROOT)
            _run([sys.executable, "-m", "pytest", "-q"], env=env)
        manager_build = (args.manager_build.expanduser().resolve() if args.manager_build
                         else output / "manager-build")
        if not args.manager_build:
            _run([sys.executable, str(SCRIPTS_ROOT / "build_manager.py"), "--output", str(manager_build),
                  "--host-python", sys.executable])
        release = publisher.build_manager_update(
            manager_build=manager_build, output=output / "update", version=version, private_key_path=private_key,
            channel=channel, asset_prefix=prefix, title=args.title, notes=tuple(args.note),
            minimum_supported_version=args.minimum_supported_version, build=args.build, mandatory=args.mandatory,
        )
        assets = [release.archive_path]
    else:
        source_root = verify_source_inputs(COMMERCIAL_ROOT, args.source_root)
        options = full.FullBuildOptions(
            source_root=source_root, private_key=private_key, output=output, title=args.title,
            notes=tuple(args.note), minimum_supported_version=args.minimum_supported_version, build=args.build,
            mandatory=args.mandatory, wheelhouse=args.wheelhouse, offline=args.offline, skip_tests=args.skip_tests,
            resume=False, refresh_manager=False,
        )
        release, customer = full.build_full_update_artifacts(
            options, version=version, channel=channel, previous_version=live_version,
            package_url=asset_url(repo, tag, f"wechatrebot-{version}-update.zip"), asset_prefix=prefix,
        )
        assets = [release.archive_path, customer.archive_path, customer.checksum_path]
        if customer.payload_path is not None:
            assets.append(customer.payload_path)

    assets.extend(attach_incremental_payloads(
        release.manifest_path, args.incremental_payload, private_key_path=private_key,
    ))
    return release, assets


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if tuple(sys.version_info[:2]) != (3, 13) or platform.architecture()[0] != "64bit":
        parser.error("必须使用 Python 3.13 x64 执行发布脚本")
    if args.resume and args.output is None:
        parser.error("--resume 必须同时指定原 --output 产物目录")
    channel = load_update_channel()
    repo = repo_name_with_owner()
    if not args.dry_run:
        ensure_gh_ready(repo)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    artifacts_root = (COMMERCIAL_ROOT / "artifacts").resolve()

    if args.rollback:
        version = str(Version(args.rollback))
        if not args.yes and input(f"将把稳定清单回滚到 {version}，输入 {version} 确认：").strip() != version:
            print("未确认，已取消回滚。")
            return 2
        rollback_stable(repo, version, channel=channel,
                        work_root=artifacts_root / f"rollback-{version}-{timestamp}")
        print(json.dumps({"rolled_back_to": version, "manifest_url": channel.manifest_url}, ensure_ascii=False))
        return 0

    version = _project_version()
    live = fetch_stable_manifest(channel)
    live_version = str(Version(str(live["version"]))) if live else "0.0.0"
    if Version(version) < Version(live_version) or (Version(version) == Version(live_version) and not (args.dry_run or args.resume)):
        parser.error(f"项目版本 {version} 必须高于线上稳定版本 {live_version}；请先提升版本号")
    if args.manager_only:
        if args.incremental_payload:
            parser.error("--manager-only 不能与 --incremental-payload 同时使用；组件增量必须基于完整载荷发布")
        try:
            publisher._require_safe_delta_base(live_version)
        except ValueError as exc:
            parser.error(str(exc))

    tag = release_tag(version)
    prefix = asset_prefix(repo, tag)
    output = (args.output or artifacts_root / f"publish-github-{version}-{timestamp}").expanduser().resolve()
    if output == artifacts_root or artifacts_root not in output.parents:
        parser.error("输出目录必须位于本项目 artifacts 目录内")
    if output.exists() and any(output.iterdir()) and not args.resume:
        parser.error(f"输出目录不是空目录：{output}")
    output.mkdir(parents=True, exist_ok=True)
    work_root = output / "github"

    candidate_path = output / "publish-candidate.json"
    if args.resume and not candidate_path.is_file():
        parser.error("原目录缺少 publish-candidate.json，无法安全恢复；不会自动构建或签名")
    if args.resume and candidate_path.is_file():
        candidate, release, assets = _load_candidate(output, repo=repo, version=version, channel=channel)
    else:
        if Version(version) <= Version(live_version) and not args.dry_run:
            parser.error("只有原签名候选可恢复已上线的同版本")
        release, assets = _build_candidate(args, version=version, channel=channel,
                                          live_version=live_version, prefix=prefix, output=output)
        candidate = _save_candidate(output, release, assets, repo=repo, target=_git_head(),
                                    title=args.title, notes=tuple(args.note), previous=live)

    summary = {
        "version": version, "tag": tag, "repo": repo,
        "output": str(output), "candidate": str(candidate_path), "source_commit": candidate["target"],
        "assets": [{"name": p.name, "size": p.stat().st_size, "sha256": sha256_file(p)} for p in assets],
        "manifest": str(release.manifest_path), "manifest_sha256": sha256_file(release.manifest_path),
        "package_url": release.package_url,
        "stable_manifest_url": stable_manifest_url(repo), "dry_run": args.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    if args.dry_run:
        print("本地构建完成；--dry-run 未创建 Release。")
        return 0
    if not args.yes and not publisher._prompt_publish(version, release.package_size):
        print("未确认版本号，已取消发布。")
        return 2

    versioned = work_root / f"stable-{version}.json"
    work_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(release.manifest_path, versioned)
    receipt = {**summary, "target": candidate["target"], "previous_version":
               (candidate.get("previous_manifest") or {}).get("version"),
               "started_at": datetime.now(timezone.utc).isoformat(), "status": "uploading"}
    receipt_path = output / "publication-receipt.json"
    _write_json(receipt_path, receipt)
    try:
        _publish_assets(repo, tag, title=candidate["title"], notes=tuple(candidate["notes"]), version=version,
                        assets=[*assets, versioned], work_root=work_root, timeout=channel.request_timeout_seconds,
                        resume=args.resume, target=candidate["target"], channel=channel)
    except Exception as exc:
        receipt.update(status="failed_before_promotion", error=_safe_error(exc))
        _write_json(receipt_path, receipt)
        raise
    receipt["assets_verified"] = True
    _write_json(receipt_path, receipt)
    _finish_publication(repo, release, channel=channel, work_root=work_root, target=candidate["target"],
                        previous=candidate.get("previous_manifest"), skip_smoke=args.skip_remote_smoke,
                        receipt=receipt)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"发布未完成：{_safe_error(error)}", file=sys.stderr)
        raise SystemExit(1) from None
