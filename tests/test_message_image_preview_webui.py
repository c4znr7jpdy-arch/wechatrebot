"""Message image viewing against delivered assets, without starting a robot."""
import base64
import io

import pytest
from PIL import Image, ImageDraw
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def image_source(size, image_format="WEBP", color="#d5e4ec"):
    image = Image.new("RGB", size, color)
    draw = ImageDraw.Draw(image)
    width, height = size
    for row in range(8):
        top = int(height * (.08 + row * .105))
        draw.rounded_rectangle((width * .09, top, width * .91, top + height * .07),
                               radius=max(2, int(width * .02)), fill="#819eb0" if row % 2 else "#f8fbff")
    output = io.BytesIO()
    image.save(output, format=image_format)
    return f"data:image/{image_format.lower()};base64," + base64.b64encode(output.getvalue()).decode()


def open_images(page, manager_url, thumbnail):
    page.goto(manager_url)
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""thumbnail => {
      document.documentElement.dataset.skin = 'soft';
      const api = window.pywebview.api, read = api.get_messages;
      api.get_messages = async (...args) => {
        const result = await read(...args), items = result.data.items || result.data;
        for (const message of items.slice(0, 2)) {
          message.message_kind = ''; message.content = '[图片]'; message.media_previews = [thumbnail];
        }
        return result;
      };
      window.imageCalls = [];
      window.pendingImages = [];
      api.get_message_image = (...args) => {
        window.imageCalls.push(args);
        return new Promise(resolve => window.pendingImages.push(resolve));
      };
    }""", thumbnail)
    page.locator('.nav-item[data-page="history"]').click()
    images = page.locator(".message-image-preview")
    expect(images).to_have_count(2)
    return images


@pytest.mark.parametrize("width,theme", [(1440, "light"), (1440, "dark"), (430, "light"), (430, "dark")])
def test_click_fetches_original_and_shows_blurred_fitted_view(browser, manager_url, tmp_path, width, theme):
    page = browser.new_page(viewport={"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    thumbnail, original = image_source((120, 240)), image_source((800, 1600), "PNG")
    try:
        images = open_images(page, manager_url, thumbnail)
        if theme == "dark":
            page.locator("#themeToggle").click()
        assert page.evaluate("imageCalls.length") == 0
        trigger = images.first
        trigger.click()
        dialog = page.locator(".report-preview-dialog")
        shown = dialog.locator("img")
        expect(dialog).to_be_visible()
        expect(shown).to_be_visible()
        expect(shown).to_have_attribute("src", thumbnail)
        expect(dialog.locator('[role="status"]')).to_have_text("正在获取原图…")
        assert page.evaluate("imageCalls.length") == 1
        call = page.evaluate("imageCalls[0]")
        assert call[0] and call[1] and call[2] == 0 and call[3]
        for surface in [dialog, dialog.locator(".report-preview-viewport")]:
            assert surface.evaluate("e => getComputedStyle(e).backgroundColor") == "rgba(0, 0, 0, 0)"
        backdrop = dialog.evaluate("e => ({blur:getComputedStyle(e, '::backdrop').backdropFilter, color:getComputedStyle(e, '::backdrop').backgroundColor})")
        assert backdrop["blur"] == "blur(6px)"
        assert "rgba(" in backdrop["color"]
        assert shown.bounding_box()["height"] > 600
        page.evaluate("src => pendingImages.shift()({ok:true, data:{src, quality:'original', account_id:imageCalls[0][3]}})", original)
        expect(shown).to_have_attribute("src", original)
        expect(dialog.locator('[role="status"]')).not_to_be_visible()
        box = shown.bounding_box()
        assert box["height"] > 600 and box["width"] <= width - 32
        assert box["width"] / box["height"] == pytest.approx(.5)
        dialog.locator('[data-action="in"]').click()
        assert shown.bounding_box()["width"] == pytest.approx(box["width"] * 1.25, abs=1)
        dialog.locator('[data-action="out"]').click()
        assert shown.bounding_box()["width"] == pytest.approx(box["width"], abs=1)
        assert images.first.locator("img").get_attribute("src") == thumbnail
        page.screenshot(path=str(tmp_path / f"message-image-{theme}-{width}.png"), animations="disabled")
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(shown).not_to_have_attribute("src", original)
        expect(shown).not_to_have_attribute("data-thumbnail-source", thumbnail)
        expect(trigger).to_be_focused()
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert not errors, errors
        print(f"Message image evidence: {tmp_path / f'message-image-{theme}-{width}.png'}")
    finally:
        page.close()


@pytest.mark.parametrize("result", ["unavailable", "cached", "invalid", "broken", "wrong_account"])
def test_original_failure_keeps_thumbnail_and_reports_status(browser, manager_url, result):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    thumbnail = image_source((120, 240))
    try:
        images = open_images(page, manager_url, thumbnail)
        images.first.click()
        dialog = page.locator(".report-preview-dialog")
        shown = dialog.locator("img")
        expect(shown).to_be_visible()
        if result == "cached":
            response = {"ok": True, "data": {"src": image_source((400, 800), "PNG"), "quality": "cached"}}
            expected = "当前为已缓存图片，原图不可用。"
        elif result == "unavailable":
            response = {"ok": True, "data": {"src": "", "message": "原图已过期，已显示缩略图。"}}
            expected = response["data"]["message"]
        elif result == "wrong_account":
            response = {"ok": True, "data": {"src": image_source((800, 1600), "PNG"), "account_id": "wxid_other"}}
            expected = "微信账号已切换，请重新打开图片。"
        else:
            response = {"ok": True, "data": {"src": "data:image/png;base64,YmFk" if result == "broken" else "https://invalid.test/image.png"}}
            expected = "原图加载失败，已显示缩略图。" if result == "broken" else "原图暂不可用，已显示缩略图。"
        page.evaluate("response => pendingImages.shift()(response)", response)
        expect(dialog.locator('[role="status"]')).to_have_text(expected)
        expect(dialog.locator('[role="status"]')).to_be_visible()
        expect(shown).to_be_visible()
        expect(shown).to_have_attribute("src", response["data"]["src"] if result == "cached" else thumbnail)
        dialog.locator(".report-preview-viewport").click(position={"x": 5, "y": 5})
        expect(dialog).not_to_be_visible()
    finally:
        page.close()


def test_original_failure_status_auto_dismisses(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    thumbnail = image_source((120, 240))
    try:
        images = open_images(page, manager_url, thumbnail)
        images.first.click()
        dialog = page.locator(".report-preview-dialog")
        page.evaluate("pendingImages.shift()({ok:true, data:{src:'', message:'原图未能获取，已显示缩略图。'}})")
        status = dialog.locator('[role="status"]')
        expect(status).to_have_text("原图未能获取，已显示缩略图。")
        expect(status).to_be_visible()
        expect(status).to_be_hidden(timeout=5000)
    finally:
        page.close()


def test_account_change_closes_view_and_discards_pending_original(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    thumbnail = image_source((120, 240))
    original = image_source((800, 1600), "PNG")
    try:
        images = open_images(page, manager_url, thumbnail)
        images.first.click()
        dialog = page.locator(".report-preview-dialog")
        expect(dialog.locator("img")).to_be_visible()
        page.evaluate("""() => {
          const read = window.pywebview.api.get_status;
          window.pywebview.api.get_status = async () => {
            const result = await read(); result.data.runtime.wechat_identity = 'wxid_changed';
            return result;
          };
        }""")
        expect(dialog).not_to_be_visible()
        page.evaluate("src => pendingImages.shift()({ok:true, data:{src, account_id:imageCalls[0][3]}})", original)
        expect(dialog.locator("img")).not_to_have_attribute("src", original)
        expect(dialog.locator("img")).not_to_have_attribute("data-thumbnail-source", thumbnail)
    finally:
        page.close()


def test_closed_or_replaced_view_ignores_late_original(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    thumbnail = image_source((120, 240))
    original = image_source((800, 1600), "PNG")
    try:
        images = open_images(page, manager_url, thumbnail)
        images.first.click()
        dialog = page.locator(".report-preview-dialog")
        expect(dialog.locator("img")).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        page.evaluate("src => pendingImages.shift()({ok:true, data:{src}})", original)
        expect(dialog.locator("img")).not_to_have_attribute("src", original)
        images.first.click()
        expect(dialog.locator("img")).to_be_visible()
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        images.nth(1).click()
        expect(dialog.locator("img")).to_be_visible()
        page.evaluate("src => pendingImages.shift()({ok:true, data:{src}})", original)
        expect(dialog.locator("img")).to_have_attribute("src", thumbnail)
        page.evaluate("src => pendingImages.shift()({ok:true, data:{src}})", original)
        expect(dialog.locator("img")).to_have_attribute("src", original)
        assert page.evaluate("imageCalls.length") == 3
    finally:
        page.close()
