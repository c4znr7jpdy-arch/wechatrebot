"""Skin selection through shipped manager routes, without starting a robot."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def open_skin_settings(page, url):
    page.goto(url)
    expect(page).to_have_title(re.compile("机器人管理$"))
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.locator('.nav-item[data-page="settings"]').click()
    page.locator('[data-settings-tab="skin"]').click()
    expect(page.locator("#skinOptions")).to_be_visible()


@pytest.mark.parametrize("width,height", [(1440, 900), (1080, 680), (600, 860)])
def test_skins_switch_independently_of_mode_and_survive_reload(browser, manager_url, tmp_path, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    try:
        open_skin_settings(page, manager_url)
        for skin in ("soft", "default"):
            page.locator(f'[data-skin-option][value="{skin}"]').check()
            expect(page.locator("#skinStatus")).to_contain_text("已应用")
            expect(page.locator(f'[data-skin-option][value="{skin}"]')).to_be_checked()
            for theme in ("dark", "light"):
                if page.locator("html").get_attribute("data-theme") != theme:
                    page.locator("#themeToggle").click()
                expect(page.locator("html")).to_have_attribute("data-theme", theme)
                assert page.locator("html").get_attribute("data-skin") == ("soft" if skin == "soft" else None)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(tmp_path / f"skin-{skin}-{theme}-{width}.png"), animations="disabled")
            open_skin_settings(page, manager_url)
            expect(page.locator(f'[data-skin-option][value="{skin}"]')).to_be_checked()
        assert not errors, errors
        assert not failures, failures
    finally:
        page.close()


def test_skin_save_failure_keeps_previous_skin_and_allows_retry(browser, manager_url):
    page = browser.new_page()
    try:
        open_skin_settings(page, manager_url)
        page.evaluate("""() => {
          window.originalSkinSave = window.pywebview.api.save_skin;
          window.pywebview.api.save_skin = async () => ({ok: false,
            error: {code:'15001', title:'保存失败', message:'测试写入失败', recorded:true}});
        }""")
        page.locator('[data-skin-option][value="soft"]').click()
        expect(page.locator("#skinStatus")).to_contain_text("未能保存")
        expect(page.locator('[data-skin-option][value="default"]')).to_be_checked()
        assert page.locator("html").get_attribute("data-skin") is None
        page.evaluate("window.pywebview.api.save_skin = window.originalSkinSave")
        page.locator('[data-skin-option][value="soft"]').check()
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        expect(page.locator("html")).to_have_attribute("data-skin", "soft")
    finally:
        page.close()


@pytest.mark.parametrize("storage", ["unavailable", "invalid", "legacy"])
def test_skin_storage_fallback_and_legacy_migration(browser, manager_url, storage):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        if storage == "unavailable":
            page.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('blocked')}})")
        else:
            value = "soft" if storage == "legacy" else "unknown-skin"
            page.add_init_script(f"localStorage.setItem('wxrebot-skin', '{value}')")
        open_skin_settings(page, manager_url)
        expected = "soft" if storage == "legacy" else "default"
        expect(page.locator(f'[data-skin-option][value="{expected}"]')).to_be_checked()
        page.locator('[data-skin-option][value="soft"]').check()
        expect(page.locator("html")).to_have_attribute("data-skin", "soft")
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        assert not errors, errors
    finally:
        page.close()


def test_unavailable_skin_styles_fall_back_to_default(browser, manager_url):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/soft-theme.css", lambda route: route.abort())
    try:
        open_skin_settings(page, manager_url + "&style=soft")
        expect(page.locator('[data-skin-option][value="default"]')).to_be_checked()
        expect(page.locator('[data-skin-option][value="soft"]')).to_be_disabled()
        expect(page.locator("#skinStatus")).to_contain_text("默认皮肤")
        assert page.locator("html").get_attribute("data-skin") is None
        assert not errors, errors
    finally:
        page.close()


def test_legacy_url_does_not_override_later_selection_on_reload(browser, manager_url):
    page = browser.new_page()
    try:
        open_skin_settings(page, manager_url + "&style=soft")
        expect(page.locator('[data-skin-option][value="soft"]')).to_be_checked()
        page.locator('[data-skin-option][value="default"]').check()
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        assert "style=" not in page.url
        assert "preview=1" in page.url
        open_skin_settings(page, page.url)
        expect(page.locator('[data-skin-option][value="default"]')).to_be_checked()
        assert page.locator("html").get_attribute("data-skin") is None
    finally:
        page.close()


def test_skin_tab_preserves_core_unsaved_changes_guard(browser, manager_url):
    page = browser.new_page()
    try:
        open_skin_settings(page, manager_url)
        page.locator('[data-settings-tab="core"]').click()
        page.locator('#coreForm [name="robot_name"]').fill("未保存名称")
        page.locator('[data-settings-tab="skin"]').click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(page.locator('#coreForm [name="robot_name"]')).to_have_value("未保存名称")
        page.locator('[data-settings-tab="skin"]').click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#skinOptions")).to_be_visible()
    finally:
        page.close()
