"""Group QR interaction against delivered assets, without a live robot."""
import base64
import io

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def qr_image():
    image = Image.new("RGB", (256, 256), "white")
    draw = ImageDraw.Draw(image)
    for x, y in [(20, 20), (172, 20), (20, 172)]:
        draw.rectangle((x, y, x + 64, y + 64), fill="black")
        draw.rectangle((x + 10, y + 10, x + 54, y + 54), fill="white")
        draw.rectangle((x + 20, y + 20, x + 44, y + 44), fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode()


def open_history(page, manager_url):
    page.goto(manager_url)
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()


@pytest.mark.parametrize("width", [1440, 1920])
def test_group_qrcode_layout_preview_copy_and_failure(browser, manager_url, tmp_path, width):
    page = browser.new_page(viewport={"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        open_history(page, manager_url)
        page.evaluate("""src => {
          window.qrCalls = [];
          window.pywebview.api.get_group_qrcode = async (account, group) => {
            window.qrCalls.push([account, group]);
            return {ok:true, data:{account_id:account, group_id:group, image_data_url:src}};
          };
          Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{write: async items => {
            if (window.copyDenied) throw new Error('permission denied');
            const blob = await items[0].getType('image/png');
            const bitmap = await createImageBitmap(blob);
            window.copiedImage = {type:blob.type, width:bitmap.width, height:bitmap.height};
          }}});
        }""", qr_image())
        page.locator('.nav-item[data-page="history"]').click()
        qr = page.get_by_role("button", name="放大查看群二维码")
        expect(qr).to_be_visible()
        header = page.locator(".inspector-conversation-title")
        geometry = header.evaluate("""header => {
          const text = header.querySelector('div').getBoundingClientRect();
          const qr = header.querySelector('.group-qrcode').getBoundingClientRect();
          return {textHeight:text.height, width:qr.width, height:qr.height, rightOfText:qr.x >= text.right};
        }""")
        assert geometry["width"] == geometry["height"] == geometry["textHeight"]
        assert geometry["rightOfText"]
        assert page.evaluate("window.qrCalls.length") == 1
        page.locator(".message-avatar-button").first.click()
        expect(qr.locator("img")).to_be_visible()
        assert page.evaluate("window.qrCalls.length") == 1
        page.screenshot(path=str(tmp_path / f"group-qrcode-{width}.png"))
        qr.click()
        dialog = page.locator(".report-preview-dialog")
        expect(dialog).to_be_visible()
        expect(dialog.locator("h2")).not_to_be_visible()
        expect(dialog.locator("img")).to_have_attribute("src", qr_image())
        expect(dialog.locator("button:visible")).to_have_count(3)
        dialog.locator('[data-action="in"]').click()
        expect(dialog.locator("output")).to_have_text("125%")
        dialog.locator("img").click(button="right")
        copy = dialog.get_by_role("button", name="复制图片")
        expect(copy).to_be_visible()
        page.screenshot(path=str(tmp_path / f"group-qrcode-copy-{width}.png"))
        copy.click()
        expect(dialog.get_by_role("status")).to_have_text("图片已复制")
        assert page.evaluate("window.copiedImage") == {"type": "image/png", "width": 256, "height": 256}
        page.evaluate("window.copyDenied = true")
        dialog.locator("img").click(button="right")
        copy.click()
        expect(dialog.get_by_role("status")).to_contain_text("复制失败")
        dialog.get_by_role("button", name="关闭预览").click()
        expect(qr).to_be_focused()
        # Directory polling replaces the inspector; keyboard access survives.
        qr.evaluate("button => button.dataset.focusProbe = 'old'")
        expect(page.locator('[data-focus-probe="old"]')).to_have_count(0)
        expect(qr).to_be_focused()
        page.evaluate("""() => { window.pywebview.api.get_group_qrcode = async () => ({ok:false,
          error:{code:'19000', title:'群二维码暂不可用', message:'请重试', recorded:true}}); }""")
        page.locator("#refreshHistory").click()
        retry = page.get_by_role("button", name="重新获取群二维码")
        expect(retry).to_be_visible()
        expect(retry).to_have_attribute("title", "请重试")
        page.evaluate("""src => {
          window.pywebview.api.get_group_qrcode = async (account, group) => ({ok:true,
            data:{account_id:account, group_id:group, image_data_url:src}});
        }""", qr_image())
        retry.click()
        expect(qr.locator("img")).to_be_visible()
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        print(f"QR UI evidence: {tmp_path}")
    finally:
        page.close()


def test_group_qrcode_discards_late_response_after_switch(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    try:
        open_history(page, manager_url)
        page.evaluate("""src => {
          window.qrCalls = [];
          window.pywebview.api.get_group_qrcode = (account, group) => new Promise(resolve => {
            window.qrCalls.push(group);
            window.resolveQr = () => resolve({ok:true, data:{account_id:account, group_id:group, image_data_url:src}});
          });
        }""", qr_image())
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator(".group-qrcode")).to_have_text("加载中")
        page.get_by_role("button", name="个人聊天", exact=True).click()
        expect(page.locator(".group-qrcode")).to_have_count(0)
        page.evaluate("window.resolveQr()")
        expect(page.locator(".group-qrcode")).to_have_count(0)
        expect(page.locator(".inspector-conversation-title")).not_to_have_class("has-group-qrcode")
    finally:
        page.close()


def test_group_qrcode_closes_on_account_change_and_preserves_report_preview(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    try:
        open_history(page, manager_url)
        page.evaluate("""src => {
          window.pywebview.api.get_group_qrcode = async (account, group) => ({ok:true,
            data:{account_id:account, group_id:group, image_data_url:src}});
        }""", qr_image())
        page.locator('.nav-item[data-page="history"]').click()
        qr = page.get_by_role("button", name="放大查看群二维码")
        qr.click()
        dialog = page.locator(".report-preview-dialog")
        expect(dialog).to_be_visible()
        page.evaluate("""() => {
          const read = window.pywebview.api.get_status;
          window.pywebview.api.get_status = async () => {
            const result = await read(); result.data.runtime.wechat_identity = 'wxid_changed';
            return result;
          };
        }""")
        expect(dialog).not_to_be_visible()
        expect(dialog.locator("img")).not_to_have_attribute("src", qr_image())
        page.evaluate("window.openReportPreview('/assets/report-preview-simple.webp', '经典')")
        expect(dialog).to_be_visible()
        expect(dialog.locator("h2")).to_be_visible()
        expect(dialog.locator("h2")).to_have_text("经典 · 完整预览")
        assert dialog.evaluate("e => getComputedStyle(e).backgroundColor") != "rgba(0, 0, 0, 0)"
        dialog.get_by_role("button", name="关闭预览").click()
    finally:
        page.close()
