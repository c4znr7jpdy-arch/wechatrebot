"""Exercise card issuance and tab/search navigation in the rendered admin app."""
from playwright.sync_api import expect

from test_admin_upload_webui import upload_server  # noqa: F401
from test_admin_app import PASSWORD, totp
from test_manager_startup import browser  # noqa: F401


def test_card_types_copy_search_and_mobile_layout(browser, upload_server):
    page = browser.new_page(ignore_https_errors=True, viewport={"width": 1366, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(upload_server.url + "login")
        page.get_by_label("管理密码", exact=True).fill(PASSWORD)
        page.get_by_label("6 位动态码", exact=True).fill(totp(upload_server.secret, upload_server.clock[0] // 30))
        page.get_by_role("button", name="登录", exact=True).click()
        page.get_by_role("link", name="激活码", exact=True).click()
        assert page.locator('input[name="owner_ref"], input[name="license_id"], input[name="device"]').count() == 0
        page.get_by_role("button", name="生成激活码", exact=True).click()
        expect(page.get_by_label("完整激活码", exact=True)).to_be_visible()
        # Clipboard denial must leave the complete code selected for manual copy.
        page.evaluate("Object.defineProperty(navigator, 'clipboard', {value: {writeText: async () => {throw Error('denied');}}})")
        page.get_by_role("button", name="复制激活码", exact=True).click()
        expect(page.locator("#copy-status")).to_have_text("已选中激活码，请按 Ctrl+C 或长按复制")
        page.get_by_role("link", name="再生成一个", exact=True).click()
        page.get_by_label("微信换绑次数码", exact=True).check()
        expect(page.locator("#duration-fields")).to_be_hidden()
        expect(page.locator('select[name="plan"]')).to_be_disabled()
        page.get_by_role("button", name="+5 次", exact=True).click()
        page.get_by_role("button", name="生成激活码", exact=True).click()
        expect(page.get_by_role("heading", name="微信换绑 +5 次", exact=True)).to_be_visible()
        code = page.get_by_label("完整激活码", exact=True).input_value()
        page.get_by_role("link", name="发码记录", exact=True).click()
        page.get_by_label("查找客户或激活码", exact=True).fill(code)
        page.get_by_role("button", name="查询", exact=True).click()
        expect(page.get_by_text("共 1 条记录 · 第 1 页", exact=True)).to_be_visible()
        assert code not in page.url and page.url.endswith("/codes")
        page.get_by_role("link", name="客户授权", exact=True).click()
        expect(page.get_by_text("没有找到客户授权。未兑换的码请到“发码记录”查询。", exact=True)).to_be_visible()
        page.set_viewport_size({"width": 390, "height": 844})
        page.get_by_role("link", name="发码", exact=True).click()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_label("微信换绑次数码", exact=True).check()
        expect(page.get_by_label("增加微信换绑次数", exact=True)).to_be_visible()
        assert not errors, errors
    finally:
        page.close()
