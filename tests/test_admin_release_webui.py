"""Navigation and typed release forms remain usable during slow remote reads."""
import asyncio
import threading

from playwright.sync_api import expect
import pytest

from services.activation_service.admin.auth import totp
from services.activation_service.admin.github import GitHubError
from test_admin_app import PASSWORD
from test_admin_upload_webui import _expand_advanced, upload_server  # noqa: F401
from test_manager_startup import browser  # noqa: F401


@pytest.mark.parametrize("width,height", [(1366, 900), (390, 844)])
def test_navigation_does_not_wait_for_github_and_refresh_preserves_inputs(
    browser, upload_server, monkeypatch, tmp_path, width, height,
):
    server = upload_server
    gates = {kind: threading.Event() for kind in ("stable", "listing")}
    originals = {kind: getattr(server.github, kind) for kind in gates}
    state = {"error": False, "changed": False}

    def slow(kind):
        async def read():
            while not gates[kind].is_set():
                await asyncio.sleep(0.01)
            if state["error"]:
                raise GitHubError("GitHub 查询暂不可用，请稍后重试。")
            value = await originals[kind]()
            if state["changed"] and kind == "stable":
                value = {"version": "1.0.2", "verified": True, "digest": "b" * 64}
            return value
        return read

    for kind in gates:
        monkeypatch.setattr(server.github, kind, slow(kind))
    page = browser.new_page(ignore_https_errors=True, viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(server.url + "login")
        page.get_by_label("管理密码", exact=True).fill(PASSWORD)
        page.get_by_label("6 位动态码").fill(totp(server.secret, server.clock[0] // 30))
        page.get_by_role("button", name="登录", exact=True).click()
        expect(page.get_by_text("授权数据库连接正常")).to_be_visible(timeout=2000)
        assert page.url == server.url
        expect(page.locator("#stable-status")).to_contain_text("正在读取")
        page.get_by_role("link", name="发布版本", exact=True).click()
        assert page.url == server.url + "releases"
        expect(page).to_have_title("发布版本 · WeChatReBot 管理后台")
        _expand_advanced(page, "高级操作：手动上传更新包")
        _expand_advanced(page, "高级操作：切换或回滚稳定清单")
        upload = page.locator('form[action$="/releases/upload"]')
        expect(upload).to_be_visible(timeout=2000)
        fields = page.locator("#release-switch-fields")
        expect(fields.locator('[name="version"]')).to_be_disabled()
        upload.locator('[name="totp"]').fill("123456")
        upload.locator('[name="candidate"]').set_input_files({
            "name": "selected-candidate.json", "mimeType": "application/json", "buffer": b"{}",
        })
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(tmp_path / f"release-loading-{width}.png"), full_page=True)
        # Returning from the back/forward cache during a read resumes polling.
        page.evaluate("""() => {
            window.dispatchEvent(new PageTransitionEvent('pagehide', {persisted: true}));
            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}));
        }""")
        for gate in gates.values():
            gate.set()
        expect(page.locator("#stable-version")).to_contain_text("1.0.0")
        expect(fields.locator('[name="version"]')).to_be_enabled()
        fields.locator('[name="version"]').select_option("1.0.1")
        fields.locator('[name="totp"]').fill("654321")
        state["changed"] = True
        page.get_by_role("button", name="刷新版本信息", exact=True).click()
        expect(page.locator("#stable-version")).to_contain_text("1.0.2")
        expect(page.locator("#release-listing-status")).to_contain_text("已保留当前选择")
        expect(fields.locator('[name="version"]')).to_have_value("1.0.1")
        expect(fields.locator('[name="totp"]')).to_have_value("654321")
        expect(fields.locator('[name="stable_digest"]')).to_have_value("a" * 64)
        expect(upload.locator('[name="totp"]')).to_have_value("123456")
        assert upload.locator('[name="candidate"]').evaluate("el => el.files[0].name") == "selected-candidate.json"
        state["error"] = True
        page.get_by_role("button", name="刷新版本信息", exact=True).click()
        expect(page.locator("#stable-status")).to_contain_text("GitHub 查询暂不可用")
        expect(fields.locator('button[type="submit"]')).to_be_disabled()
        expect(fields.locator('[name="totp"]')).to_have_value("654321")
        assert upload.locator('[name="candidate"]').evaluate("el => el.files[0].name") == "selected-candidate.json"
        assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
        page.screenshot(path=str(tmp_path / f"release-refresh-failed-{width}.png"), full_page=True)
        page.get_by_role("link", name="概览", exact=True).click()
        expect(page.get_by_text("授权数据库连接正常")).to_be_visible(timeout=2000)
        expect(page.locator("#stable-status")).to_contain_text("GitHub 查询暂不可用")
        assert not errors, errors
    finally:
        for gate in gates.values():
            gate.set()
        page.close()
