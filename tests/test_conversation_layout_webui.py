"""Conversation metadata stays aligned when names and list widths vary."""
import json
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


def history_geometry(page):
    # Wait for layout observers and the existing page-entry animation before measuring.
    page.evaluate("""async () => {
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
      await Promise.all(document.querySelector('#page-history').getAnimations({subtree:true})
        .filter(animation => animation.effect.getTiming().iterations !== Infinity)
        .map(animation => animation.finished.catch(() => {})));
    }""")
    return page.locator(".history-layout").evaluate("""layout => {
      const root = document.querySelector('.content-scroll');
      const rect = layout.getBoundingClientRect(), style = getComputedStyle(root);
      const columns = [...layout.children].map(column => column.getBoundingClientRect().width);
      return {width:layout.clientWidth, height:rect.height, columns,
        bottomGap:root.getBoundingClientRect().top + root.clientTop + root.clientHeight - parseFloat(style.paddingBottom) - rect.bottom,
        font:getComputedStyle(document.querySelector('.conversation-line strong')).fontSize};
    }""")


def open_history(page, url=None):
    if url:
        page.goto(url)
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.locator('.nav-item[data-page="history"]').click()
    expect(page.locator("#conversationList .conversation-item")).to_have_count(5)


@pytest.mark.parametrize("skin,theme", [("default", "dark"), ("soft", "light")])
@pytest.mark.parametrize("legacy", [False, True])
def test_history_fills_window_and_preserves_column_ratios(browser, manager_url, tmp_path, skin, theme, legacy):
    page = browser.new_page(viewport={"width": 1520, "height": 980})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        if legacy:
            saved = {"page-history/conversationList": [280, 540], "page-history/conversationInspector": [250, 540]}
            page.add_init_script(f"localStorage.setItem('wechatrebot-panel-layout', {json.dumps(json.dumps(saved))})")
        open_history(page, manager_url + f"&style={skin}")
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#themeToggle").click()
        before = history_geometry(page)
        for width, height in [(1920, 1200), (2048, 1433), (1520, 980)]:
            page.set_viewport_size({"width": width, "height": height})
            current = history_geometry(page)
            assert abs(current["bottomGap"]) <= 1, current
            assert current["font"] == before["font"]
            for original, resized in zip(before["columns"], current["columns"]):
                assert abs(resized - original * current["width"] / before["width"]) <= 1, current
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"history-responsive-{skin}-{legacy}.png"))
        assert not errors, errors
    finally:
        page.close()


def test_history_manual_width_stays_fluid_across_reload_hidden_page_and_breakpoints(browser, manager_url):
    page = browser.new_page(viewport={"width": 1520, "height": 1000})
    try:
        open_history(page, manager_url)
        baseline = history_geometry(page)
        # Horizontal keys on the corner handle must not accidentally lock height.
        handle = page.locator("#conversationList > .panel-resize-handle")
        handle.press("ArrowRight")
        changed = history_geometry(page)
        assert changed["columns"][0] > baseline["columns"][0] + 19
        assert abs(changed["bottomGap"]) <= 1
        saved = page.evaluate("JSON.parse(localStorage.getItem('wechatrebot-panel-layout'))")
        assert saved["page-history/history-layout"]["heightRatio"] is None
        for width in [1220, 1080, 600, 1920]:
            page.set_viewport_size({"width": width, "height": 1200})
            history_geometry(page)
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator('.nav-item[data-page="home"]').click()
        page.set_viewport_size({"width": 2048, "height": 1433})
        open_history(page)
        grown = history_geometry(page)
        assert abs(grown["bottomGap"]) <= 1, grown
        page.reload()
        open_history(page)
        reloaded = history_geometry(page)
        for original, resized in zip(changed["columns"], reloaded["columns"]):
            assert abs(resized - original * reloaded["width"] / changed["width"]) <= 1
        assert page.evaluate("JSON.parse(localStorage.getItem('wechatrebot-panel-layout'))") == saved
        page.set_viewport_size({"width": 1520, "height": 1000})
        history_geometry(page)
        handle.press("Home")
        reset = history_geometry(page)
        assert reset["columns"] == pytest.approx(baseline["columns"], abs=1)
        assert abs(reset["bottomGap"]) <= 1
        expect(page.locator("#resetPanelLayout")).to_be_hidden()
    finally:
        page.close()


def test_history_explicit_height_scales_without_changing_column_preferences(browser, manager_url):
    page = browser.new_page(viewport={"width": 1520, "height": 1000})
    try:
        open_history(page, manager_url)
        full = history_geometry(page)
        page.locator('#conversationList > [data-resize-axis="height"]').press("Shift+ArrowUp")
        shorter = history_geometry(page)
        ratio = shorter["height"] / full["height"]
        assert shorter["height"] == pytest.approx(full["height"] - 80, abs=1)
        assert shorter["columns"] == pytest.approx(full["columns"], abs=1)
        page.set_viewport_size({"width": 1920, "height": 1200})
        grown = history_geometry(page)
        assert grown["height"] / (grown["height"] + grown["bottomGap"]) == pytest.approx(ratio, abs=.002)
        page.locator("#resetPanelLayout").click()
        reset = history_geometry(page)
        assert abs(reset["bottomGap"]) <= 1
    finally:
        page.close()


def test_history_migrates_hidden_legacy_column_when_it_becomes_visible(browser, manager_url):
    page = browser.new_page(viewport={"width": 1200, "height": 1000})
    try:
        saved = {"page-history/conversationList": [260, 400], "page-history/conversationInspector": [320, 400]}
        page.add_init_script(f"localStorage.setItem('wechatrebot-panel-layout', {json.dumps(json.dumps(saved))})")
        open_history(page, manager_url)
        assert abs(history_geometry(page)["bottomGap"]) <= 1
        remaining = page.evaluate("JSON.parse(localStorage.getItem('wechatrebot-panel-layout'))")
        assert remaining["page-history/conversationInspector"] == [320, 400]
        assert "page-history/conversationList" not in remaining
        page.set_viewport_size({"width": 1500, "height": 1000})
        grown = history_geometry(page)
        assert grown["columns"][2] == pytest.approx(320, abs=1)
        assert abs(grown["bottomGap"]) <= 1
        remaining = page.evaluate("JSON.parse(localStorage.getItem('wechatrebot-panel-layout'))")
        assert "page-history/conversationInspector" not in remaining
        assert remaining["page-history/history-layout"]["heightRatio"] is None
    finally:
        page.close()
