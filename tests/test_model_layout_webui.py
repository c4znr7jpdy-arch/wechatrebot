"""Model cards fill the shipped settings page without starting a robot."""
import json
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


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

    def preview(route):
        response = route.fetch()
        route.fulfill(response=response, body=response.text().replace(
            "let previewProviders = [];", f"let previewProviders = {json.dumps(providers)};",
        ))
    page.route("**/preview.js*", preview)


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
