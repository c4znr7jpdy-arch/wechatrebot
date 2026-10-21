"""Conversation metadata stays aligned when names and list widths vary."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("skin", ["default", "soft"])
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [1440, 600])
def test_conversation_times_share_right_edge_without_squeezing_names(
    browser, manager_url, tmp_path, skin, theme, width
):
    page = browser.new_page(viewport={"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url + f"&style={skin}")
        expect(page).to_have_title(re.compile("机器人管理$"))
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#themeToggle").click()
        page.evaluate("""() => {
          const original = window.pywebview.api.get_conversations;
          window.pywebview.api.get_conversations = async (...args) => {
            const result = await original(...args);
            const names = ['短', 'We are Family', '这是一个非常长的群聊名称用于验证时间完整显示与名称省略', '未产生消息', '周末'];
            result.data.items = result.data.items.map((item, index) => ({...item, display_name:names[index]}));
            return result;
          };
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        rows = page.locator("#conversationList .conversation-item")
        expect(rows).to_have_count(5)
        page.evaluate("document.fonts.ready")

        # The desktop list can be narrowed through the existing resize control.
        for narrow in ([False, True] if width > 1100 else [False]):
            if narrow:
                handle = page.locator('#conversationList > [data-resize-axis="width"]')
                expect(handle).to_be_attached()
                handle.press("Shift+ArrowLeft")
                assert page.locator("#conversationList").bounding_box()["width"] <= 221
            geometry = rows.evaluate_all("""items => items.map(row => {
              const time = row.querySelector('time'), name = row.querySelector('.conversation-line strong');
              const box = row.getBoundingClientRect(), css = getComputedStyle(row);
              const clock = time.getBoundingClientRect(), title = name.getBoundingClientRect();
              return {rowRight:box.right, timeRight:clock.right,
                expectedRight:box.right - parseFloat(css.paddingRight) - parseFloat(css.borderRightWidth),
                gap:clock.left-title.right, timeFits:time.scrollWidth <= time.clientWidth + 1,
                nameTruncated:name.scrollWidth > name.clientWidth,
                ellipsis:getComputedStyle(name).textOverflow};
            })""")
            assert max(item["timeRight"] for item in geometry) - min(item["timeRight"] for item in geometry) <= 1, geometry
            assert all(abs(item["timeRight"] - item["expectedRight"]) <= 1 for item in geometry), geometry
            assert all(item["gap"] >= 7 and item["timeFits"] for item in geometry), geometry
            assert geometry[2]["nameTruncated"] and geometry[2]["ellipsis"] == "ellipsis", geometry[2]
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            page.screenshot(path=str(tmp_path / f"conversations-{skin}-{theme}-{width}-{narrow}.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()
