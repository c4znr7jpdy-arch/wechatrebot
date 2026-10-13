"""Model editor behavior through the delivered asset routes, without a robot."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("theme,width,height", [("light", 1440, 1024), ("dark", 1080, 680)])
def test_provider_icons_default_save_and_editor_navigation(browser, manager_url, tmp_path, theme, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    try:
        page.add_init_script(f"localStorage.setItem('wechatrebot-theme', '{theme}')")
        page.goto(manager_url)
        expect(page).to_have_title(re.compile("机器人管理$"))
        expect(page.locator("#globalRefresh")).to_be_enabled()
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator("#settingsTabs small")).to_have_count(0)
        page.locator("[data-provider-create]").click()
        expect(page.locator("#saveProvider")).to_have_text("保存并设为回复模型")
        expect(page.locator("#providerSaveIntentTitle, .provider-form-note")).to_have_count(0)
        for preset in ["glm", "xiaomi_mimo", "qwen", "volcengine", "hunyuan", "gemini_proxy", "deepseek"]:
            page.locator("#providerPreset").select_option(preset)
            expect(page.locator("#providerPresetIcon")).to_have_attribute("data-preset", preset)
            expect(page.locator("#providerPresetIcon img")).to_be_visible()
            # Visibility plus the response-failure guard below proves the
            # bundled asset loaded without CSP-unsafe wait_for_function eval.
            expect(page.locator("#providerPresetIcon img")).to_have_attribute("src", re.compile(r"assets/provider-"))
        page.locator('[name="api_base"]').fill("https://api.deepseek.com.relay.example/v1")
        expect(page.locator("#providerPresetIcon")).to_have_attribute("data-preset", "custom")
        expect(page.locator("#providerPresetIcon svg")).to_be_visible()
        page.locator("#providerPreset").select_option("deepseek")
        page.locator("#closeProviderEditor").hover()
        colors = page.locator("#closeProviderEditor").evaluate("""el => {
          const style = getComputedStyle(el);
          return {color: style.color, background: style.backgroundColor};
        }""")
        assert colors["color"] == ("rgb(166, 198, 255)" if theme == "dark" else "rgb(43, 69, 57)")
        assert colors["background"] != "rgba(0, 0, 0, 0)"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"model-editor-{theme}.png"))
        page.locator("#providerApiKey").fill("preview-only")
        page.locator("#closeProviderEditor").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(page.locator("#providerEditorPage")).to_be_visible()
        page.locator("#saveProvider").click()
        expect(page.locator("#providerOverview")).to_be_visible()
        expect(page.locator('.provider-service.active .provider-logo img')).to_have_attribute("src", "assets/provider-deepseek.ico")
        page.locator("[data-provider-create]").click()
        expect(page.locator("#saveProvider")).to_have_text("保存模型服务")
        page.locator("#providerPreset").select_option("custom")
        page.locator('[name="api_base"]').fill("https://relay.example/v1")
        page.locator("#providerModelInput").fill("deepseek-flash")
        page.locator("#providerApiKey").fill("preview-only")
        page.locator("#saveProvider").click()
        relay = page.locator(".provider-service").filter(has_text="其他兼容服务")
        expect(relay.locator(".provider-logo svg")).to_be_visible()
        expect(relay.locator(".provider-logo img")).to_have_count(0)
        relay.locator(".provider-service-actions [data-provider-edit]").click()
        expect(page.locator("#providerPreset")).to_have_value("custom")
        expect(page.locator("#providerPresetIcon")).to_have_attribute("data-preset", "custom")
        assert not failures, failures
        assert not errors, errors
    finally:
        page.close()
