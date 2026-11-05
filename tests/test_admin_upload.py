"""Signed browser uploads use bounded files, immutable assets and separate stable promotion."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest
import pytest_asyncio

from services.activation_service.admin import upload as module
from services.activation_service.admin.app import create_admin_app
from services.activation_service.admin.auth import COOKIE, initialize, totp
from services.activation_service.admin.github import GitHubError, GitHubReleases
from services.activation_service.store import LicenseStore
from wechatrebot_manager.updates import UpdateChannel, canonical_manifest_bytes
from test_admin_app import PASSWORD, NOW, login

REPO = "test-owner/test-repo"
TARGET = "a" * 40


def candidate_files(github, *, size=1024, multi=False):
    files = {"update.zip": b"x" * size}
    if multi:
        files["delta.zip"] = b"smaller signed incremental bytes"
    def package(name):
        raw = files[name]
        return {"url": f"https://github.com/{REPO}/releases/download/v1.0.1/{name}", "format": "zip",
                "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest(), "uncompressed_size": len(raw)}
    manifest = {"schema_version": 1, "product": "wechatrebot-desktop", "channel": "stable", "version": "1.0.1",
                "notes": [], "package": package("update.zip")}
    if multi:
        manifest["incremental_packages"] = [{"base_fingerprint": "b" * 64, "package": package("delta.zip")}]
    manifest["signature"] = {"algorithm": "ed25519", "key_id": github.channel.key_id,
        "value": base64.b64encode(github.key.sign(canonical_manifest_bytes(manifest))).decode("ascii")}
    raw = json.dumps(manifest).encode("utf-8")
    def artifact(path, content):
        return {"path": path, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    candidate = {"schema_version": 1, "version": "1.0.1", "repo": REPO, "target": TARGET,
                 "manifest": artifact("update\\stable.json", raw),
                 "assets": [artifact("update\\" + name, value) for name, value in files.items()]}
    return candidate, raw, files


class UploadGitHub(GitHubReleases):
    def __init__(self, directory):
        self.key = Ed25519PrivateKey.generate()
        public = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        super().__init__(directory, channel=UpdateChannel(
            f"https://github.com/{REPO}/releases/download/stable/stable.json", "stable",
            hashlib.sha256(public).hexdigest().upper()[:16], public))
        self.release = None
        self.assets, self.contents, self.calls, self.public_reads = {}, {}, [], []
        self.comparison, self.ci = "ahead", "success"
        self.public_fail = False
        self.fail_after_upload = False
        self.tag_target = TARGET

    async def stable(self):
        return {"version": "1.0.0", "digest": "f" * 64, "verified": True}

    async def listing(self):
        return []

    async def _api(self, method, path, *, payload=None, content=None, file_path=None, missing_ok=False, public=False):
        self.calls.append((method, path))
        if path.startswith("/compare/"):
            return {"status": self.comparison}
        if path.startswith("/actions/"):
            return {"workflow_runs": [{"head_sha": TARGET, "event": "push", "run_number": 1,
                "run_attempt": 1, "status": "completed", "conclusion": self.ci}]}
        if path.startswith("/commits/"):
            return {"sha": self.tag_target} if self.release and not self.release["draft"] else None
        if method == "GET" and path == "/releases/tags/v1.0.1":
            return {**self.release, "assets": list(self.assets.values())} if self.release else None
        if method == "POST" and path == "/releases":
            self.release = {"id": 123, **payload}
            return {**self.release, "assets": []}
        if method == "PATCH" and path == "/releases/123":
            self.release.update(payload)
            return dict(self.release)
        if method == "POST" and path.startswith("/releases/123/assets?"):
            name = parse_qs(urlsplit(path).query)["name"][0]
            assert name not in self.assets, "Must never overwrite an existing asset"
            raw = file_path.read_bytes() if file_path is not None else content
            identifier = len(self.contents) + 1
            asset = {"id": identifier, "name": name, "size": len(raw), "state": "uploaded",
                     "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}
            self.contents[name], self.assets[name] = raw, asset
            if self.fail_after_upload and name.endswith(".zip"):
                self.fail_after_upload = False
                raise GitHubError("模拟响应丢失")
            return dict(asset)
        if method == "DELETE":
            identifier = int(path.rsplit("/", 1)[1])
            name = next(name for name, item in self.assets.items() if item["id"] == identifier)
            del self.assets[name]
            return None
        raise AssertionError((method, path))

    async def _download(self, url, *, maximum, expected_size=0, expected_sha256=""):
        self.public_reads.append(url)
        if self.public_fail:
            raise GitHubError("公网下载失败")
        name = url.rsplit("/", 1)[1]
        raw = self.contents[name]
        if expected_size:
            assert len(raw) == expected_size and hashlib.sha256(raw).hexdigest() == expected_sha256.lower()
            return b""
        return raw


@pytest_asyncio.fixture
async def upload_admin(tmp_path):
    secret = initialize(tmp_path, PASSWORD)["secret"]
    github, now = UploadGitHub(tmp_path), [NOW]
    store = LicenseStore(tmp_path / "activation.sqlite3")
    app = create_admin_app(store, tmp_path, clock=lambda: now[0], github=github)
    async with TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar()) as client:
        admin = SimpleNamespace(client=client, secret=secret, now=now, github=github, store=store,
                                root=tmp_path, cookies={}, csrf="")
        await login(admin)
        now[0] += 30
        yield admin


def form_for(admin, candidate, manifest, files, *, csrf=None, code=None):
    form = aiohttp.FormData()
    form.add_field("csrf", admin.csrf if csrf is None else csrf)
    form.add_field("totp", totp(admin.secret, admin.now[0] // 30) if code is None else code)
    form.add_field("candidate", json.dumps(candidate).encode(), filename="publish-candidate.json", content_type="application/json")
    form.add_field("manifest", manifest, filename="stable.json", content_type="application/json")
    for name, raw in files.items():
        form.add_field("packages", raw, filename=name, content_type="application/zip")
    return form


async def upload(admin, form):
    return await admin.client.post(module.UPLOAD_ROUTE, data=form, cookies=admin.cookies, allow_redirects=False)


@pytest.mark.asyncio
async def test_streamed_multiple_packages_bypass_form_parser_cleanup_and_do_not_switch_stable(upload_admin, monkeypatch):
    admin = upload_admin
    candidate, manifest, files = candidate_files(admin.github, size=2 * 1024 * 1024, multi=True)
    original = web.Request.post
    async def guarded(request):
        assert request.path != module.UPLOAD_ROUTE, "large uploads must not call request.post"
        return await original(request)
    monkeypatch.setattr(web.Request, "post", guarded)
    response = await upload(admin, form_for(admin, candidate, manifest, files))
    assert response.status == 303
    text = await (await admin.client.get("/releases", cookies=admin.cookies)).text()
    assert "上传并公网验证完成" in text and "尚未通知客户端" in text
    assert not list(admin.root.glob(".admin-upload-*"))
    assert set(admin.github.assets) == {"update.zip", "delta.zip", "stable-1.0.1.json", "publish-candidate.json"}
    assert all("stable/stable.json" not in url for url in admin.github.public_reads)
    assert all("/tags/stable" not in path for _, path in admin.github.calls)
    with admin.store.connection() as db:
        event = db.execute("SELECT details FROM audit WHERE event='release_upload'").fetchone()
    assert json.loads(event[0])["package_count"] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("fault", ["csrf", "totp", "unsigned", "hash", "path", "missing", "extra", "disk", "ci", "source"])
async def test_reject_invalid_upload_without_stable_write_and_cleanup(upload_admin, monkeypatch, fault):
    admin = upload_admin
    candidate, manifest, files = candidate_files(admin.github, multi=True)
    options = {}
    if fault == "csrf": options["csrf"] = "错误"
    if fault == "totp": options["code"] = ""
    if fault == "unsigned":
        value = json.loads(manifest)
        value.pop("signature")
        manifest = json.dumps(value).encode()
        candidate["manifest"].update(size=len(manifest), sha256=hashlib.sha256(manifest).hexdigest())
    if fault == "hash": files["update.zip"] = b"y" * len(files["update.zip"])
    if fault == "path": candidate["assets"][0]["path"] = "../update.zip"
    if fault == "missing": files.pop("delta.zip")
    if fault == "extra": files["unlisted.zip"] = b"extra"
    if fault == "disk": monkeypatch.setattr(module.shutil, "disk_usage", lambda _: SimpleNamespace(free=0))
    if fault == "ci": admin.github.ci = "failure"
    if fault == "source": admin.github.comparison = "diverged"
    response = await upload(admin, form_for(admin, candidate, manifest, files, **options))
    assert response.status == (403 if fault == "csrf" else 303)
    assert not any(method != "GET" for method, _ in admin.github.calls)
    assert not list(admin.root.glob(".admin-upload-*"))


@pytest.mark.asyncio
async def test_retry_same_candidate_reuses_uploaded_assets_without_overwrite(upload_admin):
    admin = upload_admin
    candidate, manifest, files = candidate_files(admin.github)
    admin.github.fail_after_upload = True
    assert (await upload(admin, form_for(admin, candidate, manifest, files))).status == 303
    assert admin.github.release["draft"]
    before = [call for call in admin.github.calls if call[0] == "POST"]
    admin.now[0] += 30
    assert (await upload(admin, form_for(admin, candidate, manifest, files))).status == 303
    assert not admin.github.release["draft"]
    assert [call for call in admin.github.calls if call[0] == "POST"] == before
    assert not list(admin.root.glob(".admin-upload-*"))


@pytest.mark.asyncio
async def test_conflicting_existing_asset_refuses_and_public_failure_keeps_stable(upload_admin):
    admin = upload_admin
    candidate, manifest, files = candidate_files(admin.github)
    admin.github.public_fail = True
    await upload(admin, form_for(admin, candidate, manifest, files))
    assert not admin.github.release["draft"]
    assert "公网下载失败" in await (await admin.client.get("/releases", cookies=admin.cookies)).text()
    admin.github.public_fail = False
    admin.github.assets["update.zip"]["digest"] = "sha256:" + "0" * 64
    mutations = [call for call in admin.github.calls if call[0] != "GET"]
    admin.now[0] += 30
    await upload(admin, form_for(admin, candidate, manifest, files))
    assert "禁止覆盖" in await (await admin.client.get("/releases", cookies=admin.cookies)).text()
    assert [call for call in admin.github.calls if call[0] != "GET"] == mutations
    assert not any("stable/stable.json" in url for url in admin.github.public_reads)


@pytest.mark.asyncio
async def test_stream_bounds_stop_at_signed_size_and_use_fixed_chunks(tmp_path):
    class Part:
        def __init__(self): self.reads = 0
        async def read_chunk(self, size):
            assert size == module.UPLOAD_CHUNK_BYTES
            self.reads += 1
            return b"a" * size
    part = Part()
    package = SimpleNamespace(package_size=module.UPLOAD_CHUNK_BYTES, package_sha256="0" * 64)
    with pytest.raises(GitHubError, match="超过"):
        await module._write_package(part, tmp_path / "update.zip", package)
    assert part.reads == 2 and (tmp_path / "update.zip").stat().st_size == module.UPLOAD_CHUNK_BYTES


@pytest.mark.asyncio
async def test_declared_request_over_total_limit_is_rejected_before_reading(upload_admin):
    admin = upload_admin
    response = await admin.client.post(module.UPLOAD_ROUTE, headers={
        "Content-Length": str(module.MAX_UPLOAD_BYTES + 4 * module.MAX_METADATA_BYTES),
        "Content-Type": "multipart/form-data; boundary=test"}, cookies=admin.cookies, allow_redirects=False)
    assert response.status == 413 and not admin.github.calls


def test_metadata_total_size_and_malformed_candidate_are_rejected(tmp_path, monkeypatch):
    github = UploadGitHub(tmp_path)
    candidate, manifest, _ = candidate_files(github)
    monkeypatch.setattr(module, "MAX_UPLOAD_BYTES", 100)
    with pytest.raises(GitHubError, match="4 GiB"):
        module.validate_candidate(github, candidate, manifest)
    for bad in ([], {}, {**candidate, "target": "master"}, {**candidate, "repo": "another/repo"}):
        with pytest.raises(GitHubError):
            module.validate_candidate(github, bad, manifest)
