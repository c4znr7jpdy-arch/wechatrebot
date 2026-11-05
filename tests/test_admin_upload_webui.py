"""Preview the real admin upload forms over local HTTPS; GitHub is always fake."""
from __future__ import annotations

import asyncio
import base64
import datetime
import hashlib
import ipaddress
import json
from pathlib import Path
import ssl
import threading
from types import SimpleNamespace

from aiohttp import ClientSession, web
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from playwright.sync_api import expect
import pytest

from services.activation_service.admin.app import create_admin_app
from services.activation_service.admin.auth import initialize, totp
from services.activation_service.admin.github import GitHubError
from services.activation_service.store import LicenseStore
from test_admin_app import FakeGitHub as PageGitHub, NOW, PASSWORD
from test_admin_github import BASE, FakeGitHub, signed
from test_manager_startup import browser  # noqa: F401
from wechatrebot_manager.updates import canonical_manifest_bytes


class UploadGitHub(FakeGitHub):
    def __init__(self, directory):
        super().__init__(directory)
        self.uploads = []
        self.upload_error = False
        self.version = "1.0.0"

    async def stable(self):
        return {"version": self.version, "verified": True, "digest": "a" * 64}

    async def listing(self):
        return await PageGitHub.listing(self)

    async def switch(self, version, digest):
        result = await PageGitHub.switch(self, version, digest)
        self.version = version
        return result

    async def upload_candidate(self, candidate, manifest_raw, package_paths):
        if self.upload_error:
            raise GitHubError("GitHub 暂时无法连接，请保留本地文件后重试上传。")
        self.uploads.append({"candidate": candidate, "manifest": manifest_raw,
                             "packages": {name: path.read_bytes() for name, path in package_paths.items()}})
        return {"version": "1.0.1", "source_commit": "b" * 40, "package_count": len(package_paths), "stable_changed": False}


def _tls_context(root: Path) -> ssl.SSLContext:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    now = datetime.datetime.now(datetime.timezone.utc)
    certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
                   .public_key(key.public_key()).serial_number(x509.random_serial_number())
                   .not_valid_before(now - datetime.timedelta(minutes=1))
                   .not_valid_after(now + datetime.timedelta(hours=1))
                   .add_extension(x509.SubjectAlternativeName([
                       x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), False)
                   .sign(key, hashes.SHA256()))
    (root / "cert.pem").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    (root / "key.pem").write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(root / "cert.pem", root / "key.pem")
    return context


@pytest.fixture
def upload_server(tmp_path):
    data = tmp_path / "server"
    data.mkdir()
    secret = initialize(data, PASSWORD)["secret"]
    clock, github = [NOW], UploadGitHub(data)
    tls = _tls_context(data)
    ready, stopped = threading.Event(), threading.Event()
    addresses, failures = [], []

    async def serve():
        app = create_admin_app(LicenseStore(data / "activation.sqlite3"), data,
                               clock=lambda: clock[0], github=github)
        inner = web.AppRunner(app, access_log=None)
        outer = None
        try:
            await inner.setup()
            site = web.TCPSite(inner, "127.0.0.1", 0)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            async with ClientSession() as client:
                async def proxy(request):
                    suffix = request.path.removeprefix("/wechatrebot-admin") or "/"
                    headers = {key: value for key, value in request.headers.items()
                               if key.lower() not in {"host", "content-length", "transfer-encoding"}}
                    async with client.request(request.method, f"http://127.0.0.1:{port}{suffix}",
                                              headers=headers, data=request.content,
                                              allow_redirects=False) as response:
                        return web.Response(status=response.status, body=await response.read(),
                            headers={key: value for key, value in response.headers.items()
                                     if key.lower() not in {"content-length", "transfer-encoding", "content-encoding"}})

                proxy_app = web.Application()
                proxy_app.router.add_route("*", "/{tail:.*}", proxy)
                outer = web.AppRunner(proxy_app, access_log=None)
                await outer.setup()
                secure = web.TCPSite(outer, "127.0.0.1", 0, ssl_context=tls)
                await secure.start()
                addresses.append(f"https://127.0.0.1:{secure._server.sockets[0].getsockname()[1]}/wechatrebot-admin/")
                ready.set()
                while not stopped.is_set():
                    await asyncio.sleep(0.05)
        except Exception as exc:
            failures.append(exc)
            ready.set()
        finally:
            if outer is not None:
                await outer.cleanup()
            await inner.cleanup()

    thread = threading.Thread(target=lambda: asyncio.run(serve()), daemon=True)
    thread.start()
    try:
        assert ready.wait(10), "Local admin preview did not become ready"
        assert not failures, failures
        yield SimpleNamespace(url=addresses[0], github=github, secret=secret, clock=clock, root=data)
    finally:
        stopped.set()
        thread.join(timeout=10)
        assert not thread.is_alive()
        assert not failures, failures


@pytest.fixture
def upload_page(browser, upload_server):
    page = browser.new_page(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(upload_server.url + "login")
        expect(page.get_by_role("heading", name="维护者登录")).to_be_visible()
        page.locator('input[name="password"]').fill(PASSWORD)
        page.locator('input[name="totp"]').fill(totp(upload_server.secret, upload_server.clock[0] // 30))
        page.get_by_role("button", name="登录", exact=True).click()
        expect(page.get_by_text("授权数据库连接正常")).to_be_visible()
        page.get_by_role("link", name="发布版本", exact=True).click()
        expect(page.locator('form[action$="/releases/upload"]')).to_be_visible()
        yield page
        assert page.url.startswith(upload_server.url)
        assert page.title()
        assert not errors, errors
    finally:
        page.close()


def _candidate_files(tmp_path, github):
    files = {}
    packages = {
        "wechatrebot-1.0.1.zip": b"PK\x03\x04browser-preview-update",
        "wechatrebot-manager-1.0.1.zip": b"PK\x03\x04browser-preview-manager",
    }
    manifest = json.loads(signed(github.signer, "1.0.1", packages["wechatrebot-1.0.1.zip"],
        package_url=BASE + "v1.0.1/wechatrebot-1.0.1.zip"))
    incremental = packages["wechatrebot-manager-1.0.1.zip"]
    manifest["incremental_packages"] = [{"base_fingerprint": "c" * 64, "package": {
        "url": BASE + "v1.0.1/wechatrebot-manager-1.0.1.zip", "format": "zip", "size": len(incremental),
        "sha256": hashlib.sha256(incremental).hexdigest(),
    }}]
    manifest["signature"]["value"] = base64.b64encode(github.signer.sign(canonical_manifest_bytes(manifest))).decode()
    raw = json.dumps(manifest).encode()
    candidate = {"schema_version": 1, "repo": github.repo, "target": "b" * 40, "version": "1.0.1",
        "manifest": {"path": "stable.json", "size": len(raw), "sha256": hashlib.sha256(raw).hexdigest()},
        "assets": [{"path": name, "size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
                   for name, content in packages.items()]}
    for name, content in {"publish-candidate.json": json.dumps(candidate).encode(), "stable.json": raw, **packages}.items():
        path = tmp_path / name
        path.write_bytes(content)
        files[name] = str(path)
    return files


def _fill_upload(page, server, files):
    form = page.locator('form[action$="/releases/upload"]')
    server.clock[0] += 30
    form.locator('input[name="totp"]').fill(totp(server.secret, server.clock[0] // 30))
    form.locator('input[name="candidate"]').set_input_files(files["publish-candidate.json"])
    form.locator('input[name="manifest"]').set_input_files(files["stable.json"])
    form.locator('input[name="packages"]').set_input_files([
        files["wechatrebot-1.0.1.zip"], files["wechatrebot-manager-1.0.1.zip"],
    ])
    return form


@pytest.mark.parametrize("width,height", [(1366, 900), (390, 844)])
def test_admin_upload_is_separate_from_stable_notification(upload_page, upload_server, tmp_path, width, height):
    page, server = upload_page, upload_server
    page.set_viewport_size({"width": width, "height": height})
    expect(page.get_by_role("heading", name="上传已签名更新包")).to_be_visible()
    expect(page.locator("body")).to_contain_text("本地发布入口生成")
    expect(page.locator("body")).not_to_contain_text("此处不接收安装包上传")
    expect(page.get_by_text("当前稳定版本：")).to_contain_text("1.0.0")
    form = _fill_upload(page, server, _candidate_files(tmp_path, server.github))
    expect(form.locator('input[name="packages"]')).to_have_attribute("multiple", "")
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / f"admin-upload-{width}.png"), full_page=True)
    form.get_by_role("button", name="上传并验证", exact=True).click()
    expect(page.locator(".message")).to_contain_text("尚未通知客户端")
    expect(page.get_by_text("当前稳定版本：")).to_contain_text("1.0.0")
    assert len(server.github.uploads) == 1
    assert len(server.github.uploads[0]["packages"]) == 2
    assert not list(server.root.glob(".admin-upload-*"))
    assert not server.github.calls
    page.screenshot(path=str(tmp_path / f"admin-upload-verified-{width}.png"), full_page=True)
    server.clock[0] += 30
    switch = page.locator('form[action$="/releases"]')
    switch.locator('select[name="version"]').select_option("1.0.1")
    switch.locator('input[name="totp"]').fill(totp(server.secret, server.clock[0] // 30))
    switch.get_by_role("button").click()
    expect(page.get_by_text("当前稳定版本：")).to_contain_text("1.0.1")
    assert server.github.calls == [("1.0.1", "a" * 64)]


def test_admin_upload_missing_files_stays_in_form_and_failed_upload_can_retry(upload_page, upload_server, tmp_path):
    page, server = upload_page, upload_server
    form = page.locator('form[action$="/releases/upload"]')
    form.get_by_role("button", name="上传并验证", exact=True).click()
    assert form.evaluate("form => !form.checkValidity()")
    assert not server.github.uploads
    files = _candidate_files(tmp_path, server.github)
    server.github.upload_error = True
    _fill_upload(page, server, files).get_by_role("button", name="上传并验证", exact=True).click()
    expect(page.locator(".message")).to_contain_text("请保留本地文件后重试上传")
    expect(page.locator('form[action$="/releases/upload"]')).to_be_visible()
    expect(page.get_by_text("当前稳定版本：")).to_contain_text("1.0.0")
    assert not server.github.calls
    server.github.upload_error = False
    _fill_upload(page, server, files).get_by_role("button", name="上传并验证", exact=True).click()
    expect(page.locator(".message")).to_contain_text("尚未通知客户端")
    assert len(server.github.uploads) == 1 and not server.github.calls


@pytest.mark.parametrize("invalid", ["signature", "missing_package"])
def test_admin_upload_invalid_candidate_explains_and_allows_retry(upload_page, upload_server, tmp_path, invalid):
    page, server = upload_page, upload_server
    files = _candidate_files(tmp_path, server.github)
    if invalid == "signature":
        manifest_path = Path(files["stable.json"])
        manifest = json.loads(manifest_path.read_bytes())
        manifest["notes"] = ["modified after signing"]
        raw = json.dumps(manifest).encode()
        manifest_path.write_bytes(raw)
        candidate_path = Path(files["publish-candidate.json"])
        candidate = json.loads(candidate_path.read_bytes())
        candidate["manifest"].update(size=len(raw), sha256=hashlib.sha256(raw).hexdigest())
        candidate_path.write_text(json.dumps(candidate), encoding="utf-8")
    form = _fill_upload(page, server, files)
    if invalid == "missing_package":
        form.locator('input[name="packages"]').set_input_files(files["wechatrebot-1.0.1.zip"])
    form.get_by_role("button", name="上传并验证", exact=True).click()
    expect(page.locator(".message")).to_contain_text("清单验签失败" if invalid == "signature" else "文件缺失")
    expect(page.locator('form[action$="/releases/upload"]')).to_be_visible()
    expect(page.get_by_text("当前稳定版本：")).to_contain_text("1.0.0")
    assert not server.github.uploads and not server.github.calls
    assert not list(server.root.glob(".admin-upload-*"))
    _fill_upload(page, server, _candidate_files(tmp_path, server.github)).get_by_role("button", name="上传并验证", exact=True).click()
    expect(page.locator(".message")).to_contain_text("尚未通知客户端")
    assert len(server.github.uploads) == 1 and not server.github.calls
