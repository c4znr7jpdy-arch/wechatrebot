"""Publish customer packages, payloads, update ZIPs and the signed stable manifest to GitHub Releases."""
from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
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
        except urllib.error.HTTPError as exc:
            # A freshly uploaded asset can lag behind the release API for a few seconds.
            failure = ValueError(f"公网资产暂不可访问（HTTP {exc.code}）：{url}")
            if exc.code not in {403, 404} or attempt + 1 >= attempts:
                raise failure from exc
            sleep(delay)
    raise failure or ValueError(f"公网资产校验失败：{url}")


def _gh(argv: list[str]) -> str:
    command = ["gh", *argv]
    print("+", subprocess.list2cmdline(command), flush=True)
    completed = subprocess.run(command, cwd=COMMERCIAL_ROOT, check=True, capture_output=True,
                               text=True, encoding="utf-8", errors="replace")
    return completed.stdout


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
    except subprocess.CalledProcessError:
        return False
    return True


def create_release(repo: str, tag: str, *, title: str, notes_file: Path, target: str, latest: bool) -> None:
    argv = ["release", "create", tag, "-R", repo, "--draft", "--title", title,
            "--notes-file", str(notes_file), "--target", target]
    if not latest:
        argv.append("--latest=false")
    _gh(argv)


def upload_assets(repo: str, tag: str, paths: list[Path]) -> None:
    if not paths:
        return
    _gh(["release", "upload", tag, *(str(path) for path in paths), "-R", repo, "--clobber"])


def publish_release(repo: str, tag: str) -> None:
    _gh(["release", "edit", tag, "-R", repo, "--draft=false"])


def download_asset(repo: str, tag: str, name: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    _gh(["release", "download", tag, "-R", repo, "--pattern", name, "--dir", str(directory), "--clobber"])
    return directory / name
