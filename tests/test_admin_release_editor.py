"""Release text drafts stay private until a fresh, bound second confirmation."""
import hashlib
import re
from types import SimpleNamespace

import pytest

from services.activation_service.admin.auth import totp
from services.activation_service.admin.github import GitHubError
from test_admin_app import admin, login, post, release_status  # noqa: F401


@pytest.fixture
def candidate(admin, monkeypatch):
    raw = b"signed candidate identity"
    state = SimpleNamespace(raw=raw, calls=[], fail=False)

    async def load(version):
        if version != "1.0.1":
            raise GitHubError("版本号无效。")
        return state.raw, SimpleNamespace(version=version, title="默认标题", notes=("默认说明",))

    async def switch(version, digest, **kwargs):
        state.calls.append((version, digest, kwargs))
        if state.fail:
            raise GitHubError("公网校验失败，稳定清单未切换。")
        return {"version": version, "previous_version": "1.0.0", "changed": True}

    monkeypatch.setattr(admin.github, "candidate", load, raising=False)
    monkeypatch.setattr(admin.github, "switch", switch)
    state.fields = {"version": "1.0.1", "manifest_digest": hashlib.sha256(raw).hexdigest(),
                    "title": "✨ 新版本 <script>", "notes": "✨ 新增功能\r\n\r\n2. 修复问题 👨‍👩‍👧‍👦\n<script>alert(1)</script>"}
    return state


async def preview(admin, candidate):
    response = await post(admin, "/releases/preview", **candidate.fields)
    assert response.status == 200
    return await response.text()


def confirmation(html):
    return re.search(r'name="confirmation" value="([^"]+)"', html)[1]


@pytest.mark.asyncio
async def test_draft_requires_login_csrf_and_restores_multiline_unicode(admin, candidate):
    response = await admin.client.get("/releases/editor?version=1.0.1", allow_redirects=False)
    assert response.status == 303
    await login(admin)
    denied = await admin.client.post("/releases/draft", data=candidate.fields, cookies=admin.cookies)
    assert denied.status == 403
    assert not (admin.root / "release-drafts").exists()
    saved = await post(admin, "/releases/draft", **candidate.fields)
    assert saved.status == 303 and not candidate.calls
    data = await (await admin.client.get("/releases/editor?version=1.0.1", cookies=admin.cookies)).json()
    assert data["title"] == candidate.fields["title"]
    assert data["notes"] == candidate.fields["notes"].replace("\r\n", "\n")


@pytest.mark.asyncio
async def test_preview_escapes_text_and_publish_uses_exact_preview_once(admin, candidate):
    await login(admin)
    html = await preview(admin, candidate)
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>alert(1)</script>" not in html and "👨‍👩‍👧‍👦" in html
    assert "确认一键发布线上" in html and not candidate.calls
    # A later draft edit cannot silently change the already-previewed release text.
    await post(admin, "/releases/draft", **{**candidate.fields, "notes": "新草稿，未预览"})
    admin.now[0] += 30
    fields = {"confirmation": confirmation(html), "confirmed": "yes",
              "totp": totp(admin.secret, admin.now[0] // 30)}
    result = await post(admin, "/releases/publish", **fields)
    assert result.status == 303 and len(candidate.calls) == 1
    version, digest, extra = candidate.calls[0]
    assert version == "1.0.1" and digest == "a" * 64
    assert extra["expected_candidate_digest"] == candidate.fields["manifest_digest"]
    assert extra["message"]["notes"] == candidate.fields["notes"].replace("\r\n", "\n")
    assert (await post(admin, "/releases/publish", **fields)).status == 409
    assert len(candidate.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["expired", "token", "unchecked", "reused_totp"])
async def test_final_confirmation_cannot_be_bypassed(admin, candidate, change):
    await login(admin)
    html = await preview(admin, candidate)
    used = totp(admin.secret, admin.now[0] // 30)
    admin.now[0] += 630 if change == "expired" else 30
    fields = {"confirmation": confirmation(html), "confirmed": "yes",
              "totp": totp(admin.secret, admin.now[0] // 30)}
    if change == "token":
        fields["confirmation"] = "无效确认"
    elif change == "unchecked":
        fields.pop("confirmed")
    elif change == "reused_totp":
        fields["totp"] = used
    assert (await post(admin, "/releases/publish", **fields)).status == 409
    assert not candidate.calls


@pytest.mark.asyncio
async def test_candidate_change_and_invalid_text_do_not_save(admin, candidate):
    await login(admin)
    candidate.raw = b"different signed candidate"
    assert (await post(admin, "/releases/draft", **candidate.fields)).status == 409
    candidate.fields["manifest_digest"] = hashlib.sha256(candidate.raw).hexdigest()
    for notes in ("", "x" * 10001, "invalid\x00text"):
        response = await post(admin, "/releases/draft", **{**candidate.fields, "notes": notes})
        assert response.status == 409
    assert not (admin.root / "release-drafts").exists()


@pytest.mark.asyncio
async def test_failed_publication_retains_draft_and_does_not_report_success(admin, candidate):
    await login(admin)
    html = await preview(admin, candidate)
    candidate.fail = True
    admin.now[0] += 30
    response = await post(admin, "/releases/publish", confirmation=confirmation(html), confirmed="yes",
                          totp=totp(admin.secret, admin.now[0] // 30))
    assert response.status == 303
    html = await (await admin.client.get("/releases", cookies=admin.cookies)).text()
    assert "公网校验失败" in html and "已发布 1.0.1" not in html
    assert (admin.root / "release-drafts" / "1.0.1.json").is_file()


@pytest.mark.asyncio
async def test_editor_loads_before_status_and_old_versions_are_not_pending(admin, monkeypatch):
    async def listing():
        return [{"version": version, "verified": False, "available": True}
                for version in ("1.0.1", "1.0.0", "0.9.9")]

    monkeypatch.setattr(admin.github, "listing", listing)
    await login(admin)
    html = await (await admin.client.get("/releases", cookies=admin.cookies)).text()
    assert html.index('src="/wechatrebot-admin/release-editor.js"') < html.index('src="/wechatrebot-admin/releases.js"')
    status = await release_status(admin)
    assert {item["version"]: item["pending"] for item in status["listing"]["value"]} == {
        "1.0.1": True, "1.0.0": False, "0.9.9": False}
