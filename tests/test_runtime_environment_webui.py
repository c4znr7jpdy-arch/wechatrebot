"""Rendered environment installation flow, using synthetic preview responses only."""
import pytest
from playwright.sync_api import expect
from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("skin", ["default", "soft", "sea-salt", "apricot"])
def test_single_environment_install_retry_then_ready(browser, manager_url, tmp_path, skin, theme):
    page = browser.new_page(viewport={"width": 1080, "height": 680})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url + f"&runtime-environment=retry&style={skin}")
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#themeToggle").click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        assert page.locator("html").get_attribute("data-skin") == (None if skin == "default" else skin)
        assert "机器人管理" in page.title() and "preview=1" in page.url
        panel = page.locator("#runtimeEnvironment")
        action = page.locator("#installRuntimeEnvironment")
        expect(panel).to_be_visible()
        expect(action).to_have_text("安装运行环境")
        expect(page.locator("#primaryAction")).to_be_disabled()
        action.click()
        expect(action).to_be_disabled()
        expect(page.locator("#runtimeEnvironmentProgress")).to_be_visible()
        expect(action).to_have_text("重试安装", timeout=10000)
        page.screenshot(path=str(tmp_path / f"environment-retry-{skin}-{theme}.png"))
        action.click()
        expect(panel).to_be_hidden(timeout=10000)
        expect(page.locator("#primaryAction")).to_be_enabled()
        assert not errors
    finally:
        page.close()
