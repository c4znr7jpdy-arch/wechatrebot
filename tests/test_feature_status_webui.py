"""Feature availability and refresh feedback use preview data, without a robot."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_settings_webui import _open_feature_draft_page


@pytest.mark.parametrize("online,enabled,label", [
    (True, True, "已加载"),
    (True, False, "未加载"),
    (False, True, "启动后检查"),
    (False, False, "启动后检查"),
])
def test_feature_list_and_help_show_confirmed_loading_state(
    browser, manager_url, tmp_path, online, enabled, label,
):
    page = _open_feature_draft_page(browser, manager_url)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.evaluate("""([online, enabled]) => {
          const api = window.pywebview.api, read = api.get_features;
          api.get_features = async () => {
            const result = await read();
            result.data.engine_online = online;
            result.data.plugins = result.data.plugins.filter(item => item.id === 'feature-group-analysis');
            result.data.plugins.forEach(item => { item.enabled = enabled; });
            return result;
          };
        }""", [online, enabled])
        page.locator("#refreshFeatures").click()
        item = page.locator('.feature-item[data-feature-id="feature-group-analysis"]')
        expect(item.locator(".feature-state")).to_have_attribute("title", label)
        expect(item.locator(".feature-load-state")).to_have_text(label)
        expect(item.locator(".feature-state.enabled")).to_have_count(int(online and enabled))
        assert page.url == manager_url
        expect(page).to_have_title(re.compile("功能中心"))
        page.screenshot(path=str(tmp_path / f"features-{online}-{enabled}.png"))
        page.locator('.nav-item[data-page="help"]').click()
        card = page.locator('.help-feature-card[data-help-name="群聊分析"]')
        expect(card.locator(".status-badge")).to_have_text(label)
        expect(card.locator(".status-badge.online")).to_have_count(int(online and enabled))
        expect(page).to_have_title(re.compile("帮助"))
        card.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f"help-{online}-{enabled}.png"))
        assert not errors, errors
    finally:
        page.close()


def test_failed_feature_check_offers_retry_without_success_feedback(browser, manager_url, tmp_path):
    page = _open_feature_draft_page(browser, manager_url)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_features;
          window.failFeatureCheck = true;
          api.get_features = async () => {
            if (window.failFeatureCheck) return {ok:false, error:{code:'13003', title:'读取失败',
                message:'模拟功能读取失败，请重试', recorded:true}};
            const result = await read();
            result.data.plugins = result.data.plugins.filter(item => item.id === 'feature-group-analysis');
            return result;
          };
        }""")
        page.locator("#refreshFeatures").click()
        expect(page.locator("#featureList")).to_contain_text("功能暂时无法读取")
        expect(page.locator("#featureSummary")).to_have_text("读取失败，请重新检查")
        expect(page.locator(".toast:not(.error)").filter(has_text="功能准备情况已重新检查")).to_have_count(0)
        expect(page.locator("#refreshFeatures")).to_be_enabled()
        page.screenshot(path=str(tmp_path / "feature-check-failed.png"))
        page.evaluate("window.failFeatureCheck = false")
        page.locator("#refreshFeatures").click()
        expect(page.locator('.feature-item[data-feature-id="feature-group-analysis"]')).to_be_visible()
        expect(page.locator("#featureSummary")).not_to_contain_text("读取失败")
        expect(page.locator(".toast:not(.error)")).to_contain_text("功能准备情况已重新检查")
        assert not errors, errors
    finally:
        page.close()
