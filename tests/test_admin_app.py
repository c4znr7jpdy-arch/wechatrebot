import asyncio
import base64
import json
import re
from types import SimpleNamespace

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
import pytest
import pytest_asyncio

from services.activation_service.admin.app import create_admin_app
from services.activation_service.admin.auth import COOKIE, PREFIX, AuthStore, initialize, totp
from services.activation_service.admin.github import GitHubError
from services.activation_service.contact import CONTACT_FILENAME
from services.activation_service.store import LicenseStore
from test_activation_service import env, issue, redeem

PASSWORD = "test-only-long-admin-password"
NOW = 1_790_000_000
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jY9kAAAAASUVORK5CYII=")


class FakeGitHub:
    def __init__(self):
        self.calls, self.failed = [], False

    async def stable(self):
        if self.failed:
            raise GitHubError("GitHub 请求失败（HTTP 401），请检查网络和令牌权限。")
        return {"version": "1.0.0", "verified": True, "digest": "a" * 64}

    async def listing(self):
        return [{"version": "1.0.1", "published_at": "2026-11-05", "assets": [{"name": "update.zip", "size": 123}],
                 "verified": True, "error": ""},
                {"version": "0.9.9", "published_at": "2026-10-01", "assets": [], "verified": False, "error": "清单验签失败"}]

    async def switch(self, version, digest):
        self.calls.append((version, digest))
        return {"version": version, "previous_version": "1.0.0", "changed": True}


def csrf(html):
    return re.search(r'name="csrf" value="([^"]+)"', html)[1]


@pytest_asyncio.fixture
async def admin(tmp_path):
    secret = initialize(tmp_path, PASSWORD)["secret"]
    now, github = [NOW], FakeGitHub()
    store = LicenseStore(tmp_path / "activation.sqlite3")
    app = create_admin_app(store, tmp_path, clock=lambda: now[0], github=github)
    async with TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar()) as client:
        result = SimpleNamespace(client=client, secret=secret, now=now, github=github, store=store,
                                 root=tmp_path, cookies={}, csrf="")
        yield result


async def login(admin):
    response = await admin.client.get("/login")
    token = response.cookies[COOKIE].value
    assert response.cookies[COOKIE]["secure"] and response.cookies[COOKIE]["httponly"]
    assert response.cookies[COOKIE]["samesite"] == "Strict"
    assert response.cookies[COOKIE]["path"] == PREFIX
    response = await admin.client.post("/login", data={"csrf": csrf(await response.text()), "password": PASSWORD,
        "totp": totp(admin.secret, admin.now[0] // 30)}, cookies={COOKIE: token}, allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == PREFIX
    admin.cookies = {COOKIE: response.cookies[COOKIE].value}
    response = await admin.client.get("/codes", cookies=admin.cookies)
    admin.csrf = csrf(await response.text())


async def post(admin, path, **fields):
    if fields.get("action") == "issue" and "reference" not in fields:
        fields.update(await issuance_fields(admin))
    return await admin.client.post(path, data={"csrf": admin.csrf, **fields}, cookies=admin.cookies, allow_redirects=False)


async def issuance_fields(admin):
    html = await (await admin.client.get("/codes", cookies=admin.cookies)).text()
    return {name: re.search(fr'name="{name}" value="([^"]+)"', html)[1]
            for name in ("reference", "reference_signature")}


async def release_status(admin, *, scope="releases", complete=True):
    async with asyncio.timeout(3):
        while True:
            response = await admin.client.get("/releases/status", params={"scope": scope}, cookies=admin.cookies)
            assert response.status == 200
            value = await response.json()
            if not complete or not any(item["loading"] for item in value.values()):
                return value
            await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_uninitialized_all_routes_have_no_login_form(tmp_path):
    app = create_admin_app(LicenseStore(tmp_path / "license.sqlite3"), tmp_path, github=FakeGitHub())
    async with TestClient(TestServer(app)) as client:
        for path in ("/", "/login", "/codes", "/contact", "/releases", "/releases/status", "/releases.js", "/style.css", "/missing"):
            response = await client.get(path)
            html = await response.text()
            assert response.status == 503 and "未初始化" in html and "<form" not in html


@pytest.mark.asyncio
async def test_login_security_headers_csrf_logout_and_session_invalidation(admin):
    response = await admin.client.post("/login", data={"password": PASSWORD, "totp": "123456"})
    assert response.status == 403
    await login(admin)
    response = await admin.client.get("/", cookies=admin.cookies)
    assert response.status == 200
    assert response.headers["Cache-Control"] == "no-store" and response.headers["X-Frame-Options"] == "DENY"
    assert "form-action 'self'" in response.headers["Content-Security-Policy"]
    response = await admin.client.post("/codes", data={"action": "issue"}, cookies=admin.cookies)
    assert response.status == 403 and admin.store.inspect()["codes"] == 0
    response = await admin.client.post("/codes", data={"csrf": "非ASCII", "action": "issue"}, cookies=admin.cookies)
    assert response.status == 403
    response = await post(admin, "/logout")
    assert response.status == 303
    response = await admin.client.get("/codes", cookies=admin.cookies, allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == PREFIX + "login"
    with admin.store.connection() as db:
        assert {row[0] for row in db.execute("SELECT event FROM audit WHERE actor='admin-web'")} >= {"login_success", "logout"}


@pytest.mark.asyncio
async def test_lockout_display_and_audit_are_generic(admin):
    response = await admin.client.get("/login")
    cookie = {COOKIE: response.cookies[COOKIE].value}
    token = csrf(await response.text())
    for attempt in range(5):
        response = await admin.client.post("/login", data={"csrf": token, "password": "wrong", "totp": "000000"}, cookies=cookie)
        assert response.status == (429 if attempt == 4 else 200)
    assert "900 秒" in await response.text()
    with admin.store.connection() as db:
        events = [r[0] for r in db.execute("SELECT event FROM audit")]
    assert events.count("login_failed") == 4 and events[-1] == "login_locked"


@pytest.mark.asyncio
async def test_issue_automatic_reference_history_reveal_and_duplicate_rejected(admin):
    await login(admin)
    automatic = await issuance_fields(admin)
    assert re.fullmatch(r"WRB-\d{14}-[A-F0-9]{16}", automatic["reference"])
    response = await post(admin, "/codes", action="issue", mode="new", plan="month", owner_ref="customer-one",
                          valid_days="30", **automatic)
    code_id = admin.store.inspect(owner_ref="customer-one")["codes"][0]["id"]
    assert response.status == 303 and response.headers["Location"] == PREFIX + "codes/" + code_id
    first = await (await admin.client.get("/codes/" + code_id, cookies=admin.cookies)).text()
    code = re.search(r"WRB-(?:[A-Z2-7]{8}-){3}[A-Z2-7]{8}", first)[0]
    assert "复制激活码" in first and automatic["reference"] in first and "customer-one" in first
    assert code in await (await admin.client.get("/codes/" + code_id, cookies=admin.cookies)).text()
    second = await (await admin.client.get("/codes", cookies=admin.cookies)).text()
    assert code not in second and automatic["reference"] in second and "customer-one" in second
    await post(admin, "/codes", action="inspect", owner_ref="customer-one")
    queried = await (await admin.client.get("/codes", cookies=admin.cookies)).text()
    assert code not in queried and "查询结果" in queried
    await post(admin, "/codes", action="issue", mode="new", plan="month", owner_ref="customer-one",
               valid_days="30", **automatic)
    assert "已经发过码" in await (await admin.client.get("/codes", cookies=admin.cookies)).text()
    assert admin.store.inspect()["codes"] == 1
    await post(admin, "/codes", action="revoke", code_id=code_id)
    assert admin.store.inspect(owner_ref="customer-one")["codes"][0]["revoked_at"] == NOW
    with admin.store.connection() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM audit")]
    assert all(row["actor"] == "admin-web" for row in rows)
    assert code not in json.dumps(rows)
    with AuthStore(admin.root).connection() as db:
        assert code not in " ".join(row[0] for row in db.execute("SELECT flash FROM sessions"))


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["renewal", "recovery", "migration"])
async def test_issue_existing_license_modes_reuse_store_validation(admin, env, mode):
    await login(admin)
    # Populate the UI's actual store with a legitimate, redeemed grant.
    env.store = admin.store
    env.now = NOW
    original = redeem(env, issue(env, owner="customer-one"))
    fields = {"action": "issue", "mode": mode, "owner_ref": "customer-one",
              "valid_days": "30", "license_id": original["id"], "plan": "month" if mode == "renewal" else ""}
    if mode == "migration":
        fields.update(device="a" * 64, reset_account="on")
    response = await post(admin, "/codes", **fields)
    assert response.status == 303
    detail = response.headers["Location"].removeprefix(PREFIX.rstrip("/"))
    assert "复制激活码" in await (await admin.client.get(detail, cookies=admin.cookies)).text()
    rows = admin.store.inspect(owner_ref="customer-one")["codes"]
    assert len(rows) == 2 and any(row["mode"] == mode for row in rows)


@pytest.mark.asyncio
async def test_issue_requires_signed_system_reference_and_csrf(admin):
    await login(admin)
    original = await issuance_fields(admin)
    fields = {"action": "issue", "mode": "new", "plan": "year", "owner_ref": "customer-one", "valid_days": "30"}
    for reference in ({"reference": "manual-order"}, {**original, "reference": original["reference"][:-1] + "Z"},
                      {**original, "reference_signature": "a" * 64}):
        response = await post(admin, "/codes", **fields, **reference)
        assert response.status == 303
        assert "系统生成的业务编号" in await (await admin.client.get("/codes", cookies=admin.cookies)).text()
    response = await admin.client.post("/codes", data={**fields, **original}, cookies=admin.cookies)
    assert response.status == 403 and admin.store.inspect()["codes"] == 0


@pytest.mark.asyncio
async def test_history_filters_escapes_details_and_requires_login(admin):
    await login(admin)
    response = await post(admin, "/codes", action="issue", mode="new", plan="year",
                          owner_ref="<script>customer</script>", valid_days="30")
    detail = response.headers["Location"].removeprefix(PREFIX.rstrip("/"))
    code_id = detail.rsplit("/", 1)[1]
    for path in (detail, "/codes.js"):
        denied = await admin.client.get(path, allow_redirects=False)
        assert denied.status == 303 and denied.headers["Location"] == PREFIX + "login"
    html = await (await admin.client.get("/codes", cookies=admin.cookies)).text()
    assert "&lt;script&gt;customer&lt;/script&gt;" in html and "<script>customer" not in html
    html = await (await admin.client.get("/codes", params={"owner_ref": "other"}, cookies=admin.cookies)).text()
    assert "共 0 条记录" in html and code_id not in html
    html = await (await admin.client.get("/codes", params={"code_id": code_id}, cookies=admin.cookies)).text()
    assert "共 1 条记录" in html
    await post(admin, "/codes", action="revoke", code_id=code_id)
    html = await (await admin.client.get("/codes", params={"status": "revoked"}, cookies=admin.cookies)).text()
    assert "共 1 条记录" in html and "已撤销" in html
    response = await admin.client.get(detail, cookies=admin.cookies)
    assert response.headers["Cache-Control"] == "no-store" and "script-src 'self'" in response.headers["Content-Security-Policy"]
    html = await response.text()
    assert "&lt;script&gt;customer&lt;/script&gt;" in html and "<script>customer" not in html
    for params in ({"page": "-1"}, {"page": "bad"}, {"status": "unknown"}, {"owner_ref": "x" * 129}):
        assert (await admin.client.get("/codes", params=params, cookies=admin.cookies)).status == 409


@pytest.mark.asyncio
async def test_legacy_code_detail_preserves_metadata_without_inventing_secrets(admin):
    await login(admin)
    record = admin.store.issue_code(owner_ref="old-customer", reference="old-order", now=NOW, plan="month")
    with admin.store.connection(write=True) as db:
        db.execute("UPDATE codes SET encrypted_payload='' WHERE id=?", (record["code_id"],))
    html = await (await admin.client.get("/codes/" + record["code_id"], cookies=admin.cookies)).text()
    assert "旧记录未保存" in html and "无法恢复" in html
    assert record["code"] not in html and "old-customer" not in html and "old-order" not in html
    assert "月卡" in html and record["code_id"] in html


@pytest.mark.asyncio
async def test_password_reset_invalidates_session_but_preserves_encrypted_codes(admin):
    await login(admin)
    response = await post(admin, "/codes", action="issue", mode="new", plan="month", owner_ref="customer-one", valid_days="30")
    detail = response.headers["Location"].removeprefix(PREFIX.rstrip("/"))
    original = await (await admin.client.get(detail, cookies=admin.cookies)).text()
    code = re.search(r"WRB-(?:[A-Z2-7]{8}-){3}[A-Z2-7]{8}", original)[0]
    admin.secret = initialize(admin.root, PASSWORD, reset=True)["secret"]
    denied = await admin.client.get(detail, cookies=admin.cookies, allow_redirects=False)
    assert denied.status == 303 and denied.headers["Location"] == PREFIX + "login"
    admin.cookies = {}
    await login(admin)
    assert code in await (await admin.client.get(detail, cookies=admin.cookies)).text()


@pytest.mark.asyncio
async def test_contact_upload_rejects_oversize_and_gif_then_updates_and_clears(admin):
    await login(admin)
    for image, content_type in ((b"GIF89a" + b"x" * 20, "image/gif"), (PNG + b"x" * 600 * 1024, "image/png"), (PNG, "image/png")):
        form = aiohttp.FormData()
        for key, value in {"csrf": admin.csrf, "group_name": "客户群 <script>", "expires_at": "2030-12-31"}.items():
            form.add_field(key, value)
        form.add_field("image", image, filename="qr.png", content_type=content_type)
        response = await admin.client.post("/contact", data=form, cookies=admin.cookies, allow_redirects=False)
        assert response.status == 303
        if image != PNG:
            assert not (admin.root / CONTACT_FILENAME).exists()
    record = json.loads((admin.root / CONTACT_FILENAME).read_text("utf-8"))
    assert record["group_name"] == "客户群 <script>"
    html = await (await admin.client.get("/contact", cookies=admin.cookies)).text()
    assert "客户群 &lt;script&gt;" in html and "<script>" not in html
    response = await admin.client.get("/contact/image", cookies=admin.cookies)
    assert await response.read() == PNG and response.content_type == "image/png"
    await post(admin, "/contact", action="clear")
    assert not (admin.root / CONTACT_FILENAME).exists()


@pytest.mark.asyncio
async def test_release_switch_requires_fresh_totp_csrf_and_audit(admin):
    await login(admin)
    response = await admin.client.get("/releases", cookies=admin.cookies)
    html = await response.text()
    assert 'action="' + PREFIX + 'releases/upload"' in html and 'releases.js' in html
    status = await release_status(admin)
    assert status["stable"]["value"]["version"] == "1.0.0"
    assert [item["version"] for item in status["listing"]["value"] if item["verified"]] == ["1.0.1"]
    denied = await admin.client.post("/releases", data={"version": "1.0.1", "stable_digest": "a" * 64,
        "totp": totp(admin.secret, NOW // 30)}, cookies=admin.cookies, allow_redirects=False)
    assert denied.status == 403 and not admin.github.calls
    await post(admin, "/releases", version="1.0.1", stable_digest="a" * 64, totp=totp(admin.secret, NOW // 30))
    assert not admin.github.calls
    admin.now[0] += 30
    await post(admin, "/releases", version="1.0.1", stable_digest="a" * 64, totp=totp(admin.secret, admin.now[0] // 30))
    assert admin.github.calls == [("1.0.1", "a" * 64)]
    with admin.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='stable_switch' AND actor='admin-web'").fetchone()[0] == 1
    admin.github.failed = True
    await admin.client.get("/releases", cookies=admin.cookies)
    status = await release_status(admin)
    assert status["stable"]["value"] is None and "GitHub 请求失败" in status["stable"]["error"]
    assert (await admin.client.get("/codes", cookies=admin.cookies)).status == 200


@pytest.mark.asyncio
async def test_release_pages_render_and_remain_navigable_while_github_is_blocked(admin, monkeypatch):
    await login(admin)
    gates = {name: asyncio.Event() for name in ("stable", "listing")}
    started = {name: asyncio.Event() for name in gates}
    calls = dict.fromkeys(gates, 0)
    originals = {name: getattr(admin.github, name) for name in gates}

    def blocked(name):
        async def read():
            calls[name] += 1
            started[name].set()
            await gates[name].wait()
            return await originals[name]()
        return read

    for name in gates:
        monkeypatch.setattr(admin.github, name, blocked(name))
    try:
        async with asyncio.timeout(2):
            response = await admin.client.get("/", cookies=admin.cookies)
            html = await response.text()
            assert response.status == 200 and "授权数据库连接正常" in html and "授权数" in html
            assert "releases.js" in html
            await started["stable"].wait()
            status = await release_status(admin, scope="overview", complete=False)
            assert set(status) == {"stable"} and status["stable"]["loading"]
            assert status["stable"]["value"] is None and calls["listing"] == 0
            response = await admin.client.get("/releases", cookies=admin.cookies)
            html = await response.text()
            assert response.status == 200 and "上传已签名更新包" in html
            assert 'action="' + PREFIX + 'releases/upload"' in html
            assert "<script src=" in html and "releases.js" in html
            await started["listing"].wait()
            status = await release_status(admin, complete=False)
            assert all(item["loading"] and item["value"] is None for item in status.values())
            assert (await admin.client.get("/codes", cookies=admin.cookies)).status == 200
            assert calls == {"stable": 1, "listing": 1}
        gates["stable"].set()
        gates["listing"].set()
        status = await release_status(admin)
        assert status["stable"]["value"]["version"] == "1.0.0"
        assert status["listing"]["value"][0]["version"] == "1.0.1"
        assert calls == {"stable": 1, "listing": 1}
    finally:
        for gate in gates.values():
            gate.set()


@pytest.mark.asyncio
async def test_release_status_and_script_require_login_and_preserve_response_boundaries(admin):
    for path in ("/releases/status?scope=overview", "/releases.js"):
        response = await admin.client.get(path, allow_redirects=False)
        assert response.status == 303 and response.headers["Location"] == PREFIX + "login"
    response = await admin.client.post("/releases/refresh", data={"scope": "releases"}, allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == PREFIX + "login"
    await login(admin)
    response = await admin.client.get("/releases/status", params={"scope": "overview"}, cookies=admin.cookies)
    assert response.content_type == "application/json" and set(await response.json()) == {"stable"}
    assert response.headers["Cache-Control"] == "no-store"
    assert "connect-src 'self'" in response.headers["Content-Security-Policy"]
    response = await admin.client.get("/releases.js", cookies=admin.cookies)
    assert response.status == 200 and response.content_type == "application/javascript"
    assert response.headers["Cache-Control"] == "no-store"
    response = await admin.client.get("/releases/status", params={"scope": "unknown"}, cookies=admin.cookies)
    assert response.status == 400
    await post(admin, "/logout")
    response = await admin.client.get("/releases/status", cookies=admin.cookies, allow_redirects=False)
    assert response.status == 303 and response.headers["Location"] == PREFIX + "login"


@pytest.mark.asyncio
async def test_cached_release_display_never_substitutes_for_switch_preflight(admin, monkeypatch):
    await login(admin)
    status = await release_status(admin)
    assert status["stable"]["value"]["digest"] == "a" * 64
    submitted = []

    async def reject(version, digest):
        submitted.append((version, digest))
        raise GitHubError("稳定清单已变更，请刷新页面重新核对。")

    monkeypatch.setattr(admin.github, "switch", reject)
    admin.now[0] += 30
    response = await post(admin, "/releases", version="1.0.1", stable_digest="b" * 64,
                          totp=totp(admin.secret, admin.now[0] // 30))
    assert response.status == 303 and submitted == [("1.0.1", "b" * 64)]
    with admin.store.connection() as db:
        assert db.execute("SELECT COUNT(*) FROM audit WHERE event='stable_switch_failed'").fetchone()[0] == 1
    html = await (await admin.client.get("/releases", cookies=admin.cookies)).text()
    assert "稳定清单已变更" in html


@pytest.mark.asyncio
async def test_refresh_requires_csrf_and_switch_invalidates_both_display_caches(admin, monkeypatch):
    await login(admin)
    assert (await release_status(admin))["stable"]["value"]["version"] == "1.0.0"
    denied = await admin.client.post("/releases/refresh", data={"scope": "releases"}, cookies=admin.cookies)
    assert denied.status == 403
    assert (await release_status(admin))["listing"]["value"]
    gates = {name: asyncio.Event() for name in ("stable", "listing")}

    def blocked(name):
        async def read():
            await gates[name].wait()
            return {"version": "1.0.1", "verified": True, "digest": "b" * 64} if name == "stable" else []
        return read

    for name in gates:
        monkeypatch.setattr(admin.github, name, blocked(name))
    try:
        response = await post(admin, "/releases/refresh", scope="overview")
        assert response.status == 200
        status = await response.json()
        assert set(status) == {"stable"} and status["stable"]["value"] is None and status["stable"]["loading"]
        assert (await release_status(admin, complete=False))["listing"]["value"]
        admin.now[0] += 30
        async with asyncio.timeout(2):
            response = await post(admin, "/releases", version="1.0.1", stable_digest="a" * 64,
                                  totp=totp(admin.secret, admin.now[0] // 30))
            assert response.status == 303 and admin.github.calls == [("1.0.1", "a" * 64)]
            status = await release_status(admin, complete=False)
            assert all(item["value"] is None and item["loading"] for item in status.values())
    finally:
        for gate in gates.values():
            gate.set()
    status = await release_status(admin)
    assert status["stable"]["value"]["version"] == "1.0.1" and status["listing"]["value"] == []


@pytest.mark.asyncio
async def test_admin_cleanup_cancels_pending_release_reads(tmp_path):
    secret = initialize(tmp_path, PASSWORD)["secret"]
    started, cancelled = asyncio.Event(), asyncio.Event()
    github = FakeGitHub()

    async def blocked():
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    github.stable = blocked
    store = LicenseStore(tmp_path / "activation.sqlite3")
    app = create_admin_app(store, tmp_path, clock=lambda: NOW, github=github)
    async with TestClient(TestServer(app), cookie_jar=aiohttp.DummyCookieJar()) as client:
        admin = SimpleNamespace(client=client, secret=secret, now=[NOW], github=github, store=store,
                                root=tmp_path, cookies={}, csrf="")
        await login(admin)
        async with asyncio.timeout(2):
            assert (await client.get("/", cookies=admin.cookies)).status == 200
            await started.wait()
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_public_and_admin_listeners_share_store_but_not_routes(tmp_path, unused_tcp_port):
    from services.activation_service.cli import _attach_admin
    from services.activation_service.app import create_app
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    store = LicenseStore(tmp_path / "activation.sqlite3")
    public = create_app(store, Ed25519PrivateKey.generate(), "test")
    _attach_admin(public, store, tmp_path, "127.0.0.1", unused_tcp_port)
    async with TestClient(TestServer(public)) as client:
        assert (await client.get("/health")).status == 200
        for path in ("/login", "/codes", "/codes/example", "/codes.js", "/releases/status", "/releases.js"):
            assert (await client.get(path)).status == 404
        async with aiohttp.ClientSession() as session:
            async with session.get(f"http://127.0.0.1:{unused_tcp_port}/login") as response:
                assert response.status == 503 and "未初始化" in await response.text()

