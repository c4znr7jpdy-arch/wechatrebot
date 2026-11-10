"""Model cards fill the shipped settings page without starting a robot."""
import json
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_skin_webui import contrast_ratio
from wechatrebot_manager.ui_skins import load_skin_catalog


def mock_model_cards(page):
    providers = [
        {"id": f"custom/{index}", "name": name, "model": name,
         "service_kind": kind, "configured": True, "api_key_configured": True,
         "is_default": index == 0, "thinking_supported": kind == "chat",
         "thinking_enabled": True}
        for index, (name, kind) in enumerate([
            ("mimo-v2.6-pro", "chat"), ("DeepSeek · V4.1-flash-long-model-name", "chat"),
            ("gemini-embedding-2-preview", "embedding"), ("glm-5.3-flash", "chat"),
            ("gpt-image-2", "image"), ("grok-4.7", "chat"), ("qwen3.5-plus", "chat"),
        ])
    ]
    providers[1]["api_base"] = "https://api.deepseek.com/v1"

    preview_source = (Path(__file__).resolve().parents[1] / "src/wechatrebot_manager/app_webui/preview.js").read_text(encoding="utf-8")
    preview_body = preview_source.replace(
        "let previewProviders = [];", f"let previewProviders = {json.dumps(providers)};",
    )

    def preview(route):
        # Keep the fixture independent of the temporary HTTP server during page teardown.
        route.fulfill(status=200, content_type="application/javascript", body=preview_body)
    page.route("**/preview.js*", preview)


@pytest.mark.parametrize("skin", [item["id"] for item in load_skin_catalog()])
@pytest.mark.parametrize("theme", ["light", "dark"])
def test_provider_icons_stay_readable_in_every_skin(browser, manager_url, skin, theme):
    page = browser.new_page(viewport={"width": 1440, "height": 1024}, reduced_motion="reduce")
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    mock_model_cards(page)
    page.add_init_script(f"localStorage.setItem('wechatrebot-theme', '{theme}')")
    try:
        page.goto(f"{manager_url}&style={skin}")
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator("html")).to_have_attribute("data-theme", theme)
        expect(page.locator(".provider-service")).to_have_count(7)
        expect(page.locator(".provider-service.active .provider-logo svg")).to_be_visible()
        generic_colors = page.locator(".provider-logo svg").evaluate_all("""icons => icons.map(icon => ({
          color: getComputedStyle(icon).color,
          background: getComputedStyle(icon.parentElement).backgroundColor
        }))""")
        assert len(generic_colors) == 6
        # Measure the real chip on both the active card and the inactive cards.
        for colors in generic_colors:
            assert colors["background"] != "rgba(0, 0, 0, 0)", colors
            assert contrast_ratio(colors["color"], colors["background"]) >= 4.5, colors
        brand = page.locator(".provider-logo img")
        expect(brand).to_have_attribute("src", "assets/provider-deepseek.ico")
        expect(brand).to_have_js_property("complete", True)
        image = brand.evaluate("""img => ({loaded: img.naturalWidth > 0,
          width: img.getBoundingClientRect().width, height: img.getBoundingClientRect().height,
          fit: getComputedStyle(img).objectFit,
          background: getComputedStyle(img.parentElement).backgroundColor})""")
        assert image == {"loaded": True, "width": 24, "height": 24, "fit": "contain",
                         "background": generic_colors[0]["background"]}, image

        page.locator('.provider-service.active footer [data-provider-edit]').click()
        expect(page.locator("#providerEditorPage")).to_be_visible()
        expect(page.locator("#providerPresetIcon svg")).to_be_visible()
        colors = page.locator("#providerPresetIcon svg").evaluate("""icon => ({
          color: getComputedStyle(icon).color,
          background: getComputedStyle(document.querySelector('#providerPreset')).backgroundColor
        })""")
        assert contrast_ratio(colors["color"], colors["background"]) >= 4.5, colors
        page.locator("#providerPreset").select_option("deepseek")
        expect(page.locator("#providerPresetIcon img")).to_be_visible()
        expect(page.locator("#providerPresetIcon img")).to_have_js_property("complete", True)
        assert page.locator("#providerPresetIcon img").evaluate("img => img.naturalWidth > 0")
        page.locator('[name="api_base"]').fill("https://api.deepseek.com.relay.example/v1")
        expect(page.locator("#providerPresetIcon svg")).to_be_visible()
        expect(page.locator("#providerPresetIcon img")).to_have_count(0)
        assert not errors, errors
        assert not failures, failures
    finally:
        page.close()


@pytest.mark.parametrize("skin", ["default", "soft"])
@pytest.mark.parametrize("width,columns", [(1504, 3), (1578, 3), (1920, 3), (1380, 2), (1080, 2), (600, 1)])
def test_model_grid_fills_width_and_ignores_legacy_panel_size(browser, manager_url, tmp_path, skin, width, columns):
    # 1504 x 861 allows for the native frame within the 1520 x 900 default window.
    page = browser.new_page(viewport={"width": width, "height": 861}, reduced_motion="reduce")
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    mock_model_cards(page)
    page.add_init_script("""localStorage.setItem('wechatrebot-panel-layout', JSON.stringify({
      'page-settings/providerOverview': [600, 800], 'page-groups/groupInspector': [460, 640]
    }));""")
    try:
        url = f"{manager_url}&style={skin}"
        page.goto(url)
        expect(page).to_have_title(re.compile("机器人管理$"))
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator(".provider-service")).to_have_count(7)
        for theme in ("light", "dark"):
            if page.locator("html").get_attribute("data-theme") != theme:
                page.locator("#themeToggle").click()
            expect(page.locator("html")).to_have_attribute("data-theme", theme)
            geometry = page.locator(".provider-service-cards").evaluate("""grid => {
              const rect = grid.getBoundingClientRect(), style = getComputedStyle(grid);
              const cards = [...grid.children].map(card => card.getBoundingClientRect());
              const row = cards.filter(card => Math.abs(card.top - cards[0].top) < 1);
              return {columns: row.length, rightGap: rect.right - row.at(-1).right,
                paddingRight: parseFloat(style.paddingRight), widths: row.map(card => card.width),
                height: cards[0].height,
                overflow: [...grid.querySelectorAll('.provider-service, .provider-card-header, .provider-service-actions')]
                  .some(node => node.scrollWidth > node.clientWidth + 1)};
            }""")
            assert geometry["columns"] == columns, geometry
            assert abs(geometry["rightGap"] - geometry["paddingRight"]) < 1, geometry
            assert max(geometry["widths"]) - min(geometry["widths"]) < 1, geometry
            assert geometry["height"] < 280, geometry
            assert not geometry["overflow"], geometry
            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
            expect(page.locator("#providerOverview .panel-resize-handle")).to_have_count(0)
            page.screenshot(path=str(tmp_path / f"models-{skin}-{theme}-{width}.png"), animations="disabled")
        stored = page.evaluate("JSON.parse(localStorage.getItem('wechatrebot-panel-layout'))")
        assert "page-settings/providerOverview" not in stored
        assert stored["page-groups/groupInspector"] == [460, 640]
        page.locator(".provider-service footer [data-provider-edit]").first.click()
        expect(page.locator("#providerEditorPage")).to_be_visible()
        page.locator("#closeProviderEditor").click()
        expect(page.locator("#providerOverview")).to_be_visible()
        expect(page.locator(".provider-service")).to_have_count(7)
        assert page.url == manager_url  # Skin URL migration keeps the canonical manager route.
        assert not errors, errors
        assert not failures, failures
    finally:
        page.close()
