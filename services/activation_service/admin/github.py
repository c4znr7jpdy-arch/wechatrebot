"""Read public, signed releases and switch stable without access to signing keys."""
from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import re
import secrets
import urllib.parse

import aiohttp
from packaging.version import InvalidVersion, Version

from wechatrebot_manager.updates import (
    MAX_MANIFEST_BYTES, UpdateChannel, UpdateError, load_update_channel, parse_update_manifest,
)
from .auth import TOKEN_FILE, private_json, read_private_json

_PUBLIC_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}
_RECOVERY_FILE = "admin-release-recovery.json"


class GitHubError(RuntimeError):
    """Safe operator-facing errors; never include tokens, response bodies or URLs with queries."""


class GitHubReleases:
    def __init__(self, data_dir: Path, *, channel: UpdateChannel | None = None):
        self.directory = data_dir
        self.channel = channel or load_update_channel()
        parsed = urllib.parse.urlsplit(self.channel.manifest_url)
        match = re.fullmatch(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/download/stable/stable.json", parsed.path)
        if parsed.scheme != "https" or parsed.netloc != "github.com" or not match or parsed.query:
            raise ValueError("The embedded update channel must point to GitHub stable")
        self.repo = match.group(1)
        self.api = f"https://api.github.com/repos/{self.repo}"
        self.lock = asyncio.Lock()

    def _token(self) -> str:
        try:
            token = read_private_json(self.directory / TOKEN_FILE).get("token", "")
        except FileNotFoundError:
            return ""
        if not isinstance(token, str) or not re.fullmatch(r"github_pat_[A-Za-z0-9_]{20,250}", token):
            raise GitHubError("GitHub 令牌配置无效，请在服务器本机重新设置。")
        return token

    async def _api(self, method: str, path: str, *, payload=None, content: bytes | None = None):
        token = self._token()
        if method != "GET" and not token:
            raise GitHubError("尚未配置 GitHub 写入令牌，请在服务器本机设置。")
        url = self.api + path
        if content is not None:
            url = url.replace("https://api.github.com/", "https://uploads.github.com/", 1)
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
                   "User-Agent": "WeChatReBot-Admin"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if content is not None:
            headers["Content-Type"] = "application/json"
        try:
            async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=45)) as session:
                async with session.request(method, url, headers=headers, json=payload, data=content,
                                           allow_redirects=False) as response:
                    if response.status not in {200, 201, 204}:
                        raise GitHubError(f"GitHub 请求失败（HTTP {response.status}），请检查网络和令牌权限。")
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(64 * 1024):
                        size += len(chunk)
                        if size > 4 * 1024 * 1024:
                            raise GitHubError("GitHub 返回的数据过大。")
                        chunks.append(chunk)
                    raw = b"".join(chunks)
                    return json.loads(raw) if raw else None
        except (aiohttp.ClientError, TimeoutError, ValueError):
            raise GitHubError("GitHub 暂不可用，请稍后重试。") from None

    @staticmethod
    def _public_url(url: str) -> None:
        parsed = urllib.parse.urlsplit(url)
        if (parsed.scheme != "https" or parsed.hostname not in _PUBLIC_HOSTS or parsed.port not in {None, 443}
                or parsed.username or parsed.password or parsed.fragment):
            raise GitHubError("下载地址不在 GitHub 的 HTTPS 资产范围内。")

    async def _download(self, url: str, *, maximum: int, expected_size: int = 0,
                        expected_sha256: str = "") -> bytes:
        """Stream large packages without retaining them in the service's memory."""
        try:
            timeout = aiohttp.ClientTimeout(total=1800 if expected_size else 45, sock_read=30)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                for _ in range(6):
                    self._public_url(url)
                    async with session.get(url, allow_redirects=False, headers={
                        "User-Agent": "WeChatReBot-Admin", "Cache-Control": "no-cache",
                    }) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            url = urllib.parse.urljoin(url, response.headers.get("Location", ""))
                            continue
                        if response.status != 200:
                            raise GitHubError(f"GitHub 公网资产读取失败（HTTP {response.status}）。")
                        digest, count, chunks = hashlib.sha256(), 0, []
                        async for chunk in response.content.iter_chunked(1024 * 1024):
                            count += len(chunk)
                            if count > maximum:
                                raise GitHubError("GitHub 资产超过约定大小，已停止读取。")
                            digest.update(chunk)
                            if not expected_size:
                                chunks.append(chunk)
                        if expected_size and (count != expected_size or digest.hexdigest().lower() != expected_sha256.lower()):
                            raise GitHubError("更新包大小或 SHA-256 与签名清单不一致。")
                        return b"".join(chunks)
                raise GitHubError("GitHub 下载重定向次数过多。")
        except (aiohttp.ClientError, TimeoutError):
            raise GitHubError("GitHub 下载中断，请稍后重试。") from None

    def _manifest(self, raw: bytes, version: str = ""):
        try:
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError("Manifest is not an object")
            release = parse_update_manifest(value, self.channel, current_version="0.0.0")
            if release is None or (version and release.version != str(Version(version))):
                raise ValueError("Version mismatch")
            prefix = f"https://github.com/{self.repo}/releases/download/v{release.version}/"
            if not release.package_url.startswith(prefix):
                raise ValueError("Package belongs to another release")
            suffix = release.package_url[len(prefix):]
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", suffix):
                raise ValueError("Unsafe asset name")
            return value, release
        except (UpdateError, ValueError, TypeError, KeyError):
            raise GitHubError("清单验签失败、版本不符或更新包不属于本仓库。") from None

    async def stable(self) -> dict:
        raw = await self._download(self.channel.manifest_url, maximum=MAX_MANIFEST_BYTES)
        _, release = self._manifest(raw)
        return {"version": release.version, "verified": True, "digest": hashlib.sha256(raw).hexdigest()}

    async def candidate(self, version: str) -> tuple[bytes, object]:
        try:
            version = str(Version(version))
        except InvalidVersion:
            raise GitHubError("版本号无效。") from None
        item = await self._api("GET", f"/releases/tags/v{version}")
        if not isinstance(item, dict) or item.get("draft") or item.get("prerelease") or item.get("tag_name") != f"v{version}":
            raise GitHubError("只能选择已经公开发布的正式版本。")
        name = f"stable-{version}.json"
        if not any(asset.get("name") == name for asset in item.get("assets", [])):
            raise GitHubError("该版本缺少签名清单。")
        raw = await self._download(f"https://github.com/{self.repo}/releases/download/v{version}/{name}", maximum=MAX_MANIFEST_BYTES)
        _, release = self._manifest(raw, version)
        return raw, release

    async def listing(self) -> list[dict]:
        values = await self._api("GET", "/releases?per_page=100")
        if not isinstance(values, list):
            raise GitHubError("GitHub 发布列表格式无效。")
        result = []
        for item in values:
            tag = item.get("tag_name", "")
            if not tag.startswith("v") or item.get("draft") or item.get("prerelease"):
                continue
            entry = {"version": tag[1:], "published_at": item.get("published_at", ""),
                     "assets": [{"name": a.get("name", ""), "size": a.get("size", 0)} for a in item.get("assets", [])],
                     "verified": False, "error": ""}
            try:
                await self.candidate(tag[1:])
                entry["verified"] = True
            except GitHubError as exc:
                entry["error"] = str(exc)
            result.append(entry)
        return result

    async def _readback(self, expected: bytes, *, attempts: int = 10) -> None:
        failure = None
        for attempt in range(attempts):
            try:
                raw = await self._download(self.channel.manifest_url, maximum=MAX_MANIFEST_BYTES)
                self._manifest(raw)
                if raw == expected:
                    return
                failure = GitHubError("公网稳定清单尚未与目标一致。")
            except GitHubError as exc:
                failure = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(3)
        raise failure or GitHubError("公网稳定清单校验失败。")

    async def _restore(self, recovery: dict) -> None:
        current = await self._api("GET", "/releases/tags/stable")
        old_id, new_id = recovery["old_id"], recovery["new_id"]
        assets = {item["name"]: item for item in current["assets"]}
        stable = assets.get("stable.json")
        if stable and stable["id"] not in {old_id, new_id}:
            raise GitHubError("稳定清单已被其他发布者修改，已停止自动恢复，请人工核对。")
        if stable and stable["id"] == new_id:
            await self._api("PATCH", f"/releases/assets/{new_id}", payload={"name": recovery["staged_name"]})
        await self._api("PATCH", f"/releases/assets/{old_id}", payload={"name": "stable.json"})
        await self._readback(recovery["previous"].encode("utf-8"))
        if any(item["id"] == new_id for item in current["assets"]):
            await self._api("DELETE", f"/releases/assets/{new_id}")
        (self.directory / _RECOVERY_FILE).unlink(missing_ok=True)

    async def switch(self, version: str, expected_digest: str) -> dict:
        """One operator at a time; retain old asset until public verification succeeds."""
        async with self.lock:
            recovery_path = self.directory / _RECOVERY_FILE
            if recovery_path.exists():
                # An interrupted previous operation is recovered before any new promotion.
                await self._restore(read_private_json(recovery_path, maximum=2 * MAX_MANIFEST_BYTES + 4096))
            raw, release = await self.candidate(version)
            await self._download(release.package_url, maximum=release.package_size,
                                 expected_size=release.package_size, expected_sha256=release.package_sha256)
            previous = await self._download(self.channel.manifest_url, maximum=MAX_MANIFEST_BYTES)
            _, old_release = self._manifest(previous)
            if hashlib.sha256(previous).hexdigest() != expected_digest:
                raise GitHubError("稳定版本已变化，请刷新发布页后重试。")
            if previous == raw:
                return {"version": release.version, "previous_version": old_release.version, "changed": False}
            stable = await self._api("GET", "/releases/tags/stable")
            if stable.get("draft") or stable.get("prerelease"):
                raise GitHubError("stable Release 尚未公开，请先用发布入口修复。")
            old = next((a for a in stable["assets"] if a["name"] == "stable.json"), None)
            if old is None:
                raise GitHubError("stable Release 缺少当前清单。")
            if old.get("digest") != "sha256:" + hashlib.sha256(previous).hexdigest():
                raise GitHubError("GitHub 资产摘要与公网稳定清单不符，请等待缓存刷新后重试。")
            staged_name = f"stable-pending-{secrets.token_hex(12)}.json"
            new = await self._api("POST", f"/releases/{int(stable['id'])}/assets?name={staged_name}", content=raw)
            if new.get("digest") != "sha256:" + hashlib.sha256(raw).hexdigest() or new.get("size") != len(raw):
                raise GitHubError("暂存清单的 GitHub 摘要不符，稳定版本未改变。")
            recovery = {"old_id": int(old["id"]), "new_id": int(new["id"]), "previous": previous.decode("utf-8"),
                        "staged_name": staged_name}
            private_json(recovery_path, recovery, replace=True)
            try:
                # Check again after uploading, before changing the public pointer.
                current = await self._api("GET", "/releases/tags/stable")
                current_id = next((a["id"] for a in current["assets"] if a["name"] == "stable.json"), None)
                if current_id != old["id"]:
                    raise GitHubError("稳定清单已被其他发布者修改。")
                await self._api("PATCH", f"/releases/assets/{old['id']}", payload={"name": f"stable-previous-{old['id']}.json"})
                await self._api("PATCH", f"/releases/assets/{new['id']}", payload={"name": "stable.json"})
                await self._readback(raw)
            except Exception:
                try:
                    await self._restore(recovery)
                except Exception:
                    raise GitHubError("切换失败且自动恢复未完成。已保留恢复记录，请修复网络或令牌后重试；暂停其他发布。") from None
                raise GitHubError("切换未通过公网校验，已恢复原稳定清单。") from None
            # Keep recovery information until success is known; old asset is harmless evidence.
            recovery_path.unlink(missing_ok=True)
            return {"version": release.version, "previous_version": old_release.version, "changed": True}
