"""Search-key replacement keeps the user's final intent, without a live robot."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from wechatrebot_manager.windows_tray import _BEFORE_UNLOAD_SCRIPT


@pytest.mark.parametrize("replace,width", [(False, 1380), (True, 760)])
def test_search_key_clear_and_replace(browser, manager_url, tmp_path, replace, width):
    page = browser.new_page(viewport={"width": width, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="search"]').click()
        page.locator('#webSearchForm [name="enabled"]').check(force=True)
        page.evaluate("""() => {
          const save = window.pywebview.api.save_web_search_settings;
          window.pywebview.api.save_web_search_settings = async values => {
            window.searchSaveValues = structuredClone(values);
            return await save(values);
          };
        }""")
        page.locator("#clearWebSearchKey").click()
        expect(page.locator("#webSearchNote")).to_have_text("当前服务商密钥将在保存时清除。")
        if replace:
            page.locator("#webSearchKey").fill("preview-only-replacement")
            expect(page.locator("#webSearchNote")).to_have_text("新密钥将在保存时替换当前服务商密钥。")
            page.locator("#revealWebSearchKey").click()
            expect(page.locator("#webSearchKey")).to_have_attribute("type", "text")
        page.locator('#webSearchForm button[type="submit"]').click()
        expect(page.locator("#webSearchNote")).to_contain_text("已热更新")
        assert page.evaluate("window.searchSaveValues.clear_key") is (not replace)
        assert page.evaluate("Boolean(window.searchSaveValues.key)") is replace
        expect(page.locator("#webSearchProviderSummary")).to_contain_text("已配置 1 个密钥" if replace else "尚未配置")
        expect(page.locator("#webSearchKey")).to_have_value("")
        expect(page.locator("#webSearchKey")).to_have_attribute("type", "password")
        expect(page.locator("#revealWebSearchKey")).to_have_attribute("aria-pressed", "false")
        assert page.url == manager_url
        expect(page).to_have_title(re.compile("机器人总设置"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"search-key-{replace}.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("change", ["key", "provider"])
def test_search_save_preserves_newer_draft(browser, manager_url, tmp_path, change):
    page = browser.new_page(viewport={"width": 760, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="search"]').click()
        page.locator('#webSearchForm [name="enabled"]').check(force=True)
        page.locator("#webSearchKey").fill("preview-only-submitted")
        page.evaluate("""() => {
          const save = window.pywebview.api.save_web_search_settings;
          window.searchSaveCalls = [];
          window.pywebview.api.save_web_search_settings = async values => {
            window.searchSaveCalls.push(structuredClone(values));
            const result = await save(values);
            if (window.searchSaveCalls.length === 1) return new Promise(resolve => {
              window.finishPendingSearchSave = () => resolve(result);
            });
            return result;
          };
        }""")
        button = page.locator('#webSearchForm button[type="submit"]')
        button.click()
        expect(button).to_be_disabled()
        if change == "provider":
            options = page.locator("#webSearchProvider option").evaluate_all("items => items.map(item => item.value)")
            current = page.locator("#webSearchProvider").input_value()
            page.locator("#webSearchProvider").select_option(next(item for item in options if item != current))
        provider = page.locator("#webSearchProvider").input_value()
        page.locator("#webSearchKey").fill("preview-only-newer-draft")
        page.locator('#webSearchForm [name="link_enabled"]').select_option("true")
        page.evaluate("window.finishPendingSearchSave()")
        expect(button).to_be_enabled()
        expect(page.locator("#webSearchKey")).to_have_value("preview-only-newer-draft")
        expect(page.locator("#webSearchProvider")).to_have_value(provider)
        expect(page.locator('#webSearchForm [name="link_enabled"]')).to_have_value("true")
        expect(page.locator("#webSearchNote")).to_contain_text("之后的修改仍未保存")
        # Returning to the section must keep the dirty draft instead of reloading it.
        page.locator('[data-settings-tab="skin"]').click()
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchKey")).to_have_value("preview-only-newer-draft")
        page.screenshot(path=str(tmp_path / f"search-save-{change}.png"), animations="disabled")
        button.click()
        expect(page.locator("#webSearchNote")).to_contain_text("已热更新")
        expect(page.locator("#webSearchKey")).to_have_value("")
        assert page.evaluate("window.searchSaveCalls[1].key") == "preview-only-newer-draft"
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("change", ["enabled", "key", "clear", "provider", "links"])
def test_search_draft_protects_browser_and_native_exit(browser, manager_url, tmp_path, change):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchProvider option")).not_to_have_count(0)
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is False
        enabled = page.locator('#webSearchForm [name="enabled"]')
        if change == "enabled":
            enabled.set_checked(not enabled.is_checked(), force=True)
        else:
            enabled.check(force=True)
            if change == "key":
                page.locator("#webSearchKey").fill("preview-only-draft")
            elif change == "clear":
                page.locator("#clearWebSearchKey").click()
            elif change == "provider":
                page.locator("#webSearchProvider").select_option("tavily")
            else:
                page.locator('#webSearchForm [name="link_enabled"]').select_option("true")
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is True
        page.locator('[data-settings-tab="skin"]').click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is True
        page.locator('[data-settings-tab="search"]').click()
        if change == "key":
            expect(page.locator("#webSearchKey")).to_have_value("preview-only-draft")
            with page.expect_event("dialog") as pending:
                page.evaluate("setTimeout(() => location.reload(), 0)")
            dialog = pending.value
            assert dialog.type == "beforeunload"
            dialog.dismiss()
            expect(page.locator("#webSearchKey")).to_have_value("preview-only-draft")
        page.locator('#webSearchForm button[type="submit"]').click()
        expect(page.locator("#webSearchNote")).to_contain_text("已热更新")
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is False
        page.screenshot(path=str(tmp_path / f"search-exit-{change}.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()


def test_search_save_in_progress_remains_protected(browser, manager_url):
    page = browser.new_page()
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchProvider option")).not_to_have_count(0)
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is False
        page.evaluate("""() => {
          const save = window.pywebview.api.save_web_search_settings;
          window.pywebview.api.save_web_search_settings = async values => {
            const result = await save(values);
            return new Promise(resolve => { window.finishSearchSave = () => resolve(result); });
          };
        }""")
        button = page.locator('#webSearchForm button[type="submit"]')
        button.click()
        expect(button).to_be_disabled()
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is True
        page.evaluate("window.finishSearchSave()")
        expect(button).to_be_enabled()
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT) is False
    finally:
        page.close()
