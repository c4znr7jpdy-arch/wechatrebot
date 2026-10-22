"""Internal tab content motion through preview data; no robot is started."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def open_preview(page, url):
    # The legacy preview uses a data: iframe, blocked by the shipped CSP. Keep
    # this tab-flow fixture on the same origin as real feature pages instead.
    page.route("**/__motion-preview-page", lambda route: route.fulfill(content_type="text/html", body="<p>Preview feature page</p>"))
    page.goto(url)
    expect(page).to_have_title(re.compile("机器人管理$"))
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""() => {
      window.pywebview.api.get_feature_page = async () => ({ok: true, data: {
        url: location.origin + '/__motion-preview-page', origin: location.origin,
      }});
      window.tabAnimations = [];
      const animate = Element.prototype.animate;
      Element.prototype.animate = function(...args) {
        const animation = animate.apply(this, args);
        if (window.pauseHistoryAnimations && this.matches('.history-layout')) animation.pause();
        window.tabAnimations.push(animation);
        return animation;
      };
    }""")


def assert_content_motion(page, selector):
    # The tab becomes active before its awaited content read completes. Wait
    # for that panel's real animation instead of sampling the active tab state.
    animation = page.wait_for_function("""selector => window.tabAnimations.findLast(
      a => a.id === 'manager-tab-enter' && a.effect.target.matches(selector)
    ) || false""", arg=selector, timeout=5000)
    try:
        sample = animation.evaluate("""animation => {
          animation.pause();
          animation.currentTime = animation.effect.getTiming().duration / 3;
          const style = getComputedStyle(animation.effect.target);
          const sample = {opacity: Number(style.opacity), transform: style.transform};
          animation.finish();
          window.tabAnimations = [];
          return sample;
        }""")
    finally:
        animation.dispose()
    assert 0 < sample["opacity"] < 1, sample
    assert sample["transform"] != "none", sample
    expect(page.locator(selector)).to_have_css("transform", "none")


@pytest.mark.parametrize("skin,width,height", [("default", 1440, 900), ("soft", 1440, 900), ("soft", 1080, 680)])
def test_internal_tabs_animate_content_and_keep_filters_and_focus(browser, manager_url, tmp_path, skin, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors, failed_responses = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failed_responses.append(response.url) if response.status >= 400 else None)
    try:
        open_preview(page, manager_url + ("&style=soft" if skin == "soft" else ""))
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message").first).to_be_visible()
        total = page.locator("#conversationList .conversation-item").count()
        for kind in ("group", "private", "all"):
            page.locator(f'#historyKinds [data-kind="{kind}"]').click()
            expect(page.locator(f'#historyKinds [data-kind="{kind}"]')).to_have_class("active")
            assert_content_motion(page, ".history-layout")
            if kind == "all":
                expect(page.locator("#conversationList .conversation-item")).to_have_count(total)
            else:
                assert page.locator("#conversationList .conversation-item").count() > 0
                expect(page.locator(f'#conversationList .conversation-item:not([data-type="{kind}"])')).to_have_count(0)
        page.screenshot(path=str(tmp_path / f"history-tabs-{skin}-{width}.png"))
        page.locator('#historyKinds [data-kind="all"]').click()
        assert page.evaluate("window.tabAnimations.length") == 0

        page.locator('.nav-item[data-page="features"]').click()
        page.locator('#featureAudience [data-audience="common"]').click()
        assert_content_motion(page, "#featureList")
        assert page.locator("#featureList .feature-item small").evaluate_all("items => items.every(item => item.textContent.includes('常用'))")
        page.locator('[data-feature-id="feature-group-analysis"]').first.click()
        expect(page.locator("#saveFeature")).to_be_enabled()
        assert_content_motion(page, "#featureDetail")
        page.locator('.config-tabs [data-tab="advanced"]').click()
        assert_content_motion(page, "#featureForm")
        expect(page.locator('.config-tabs [data-tab="advanced"]')).to_be_focused()
        page.locator('.config-tabs [data-tab="basic"]').click()
        assert_content_motion(page, "#featureForm")
        page.screenshot(path=str(tmp_path / f"feature-tabs-{skin}-{width}.png"))

        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('[data-chat-management-tab="private"]').click()
        expect(page.locator("#privateManagementPanel")).to_be_visible()
        assert_content_motion(page, "#privateManagementPanel")
        page.locator('[data-chat-management-tab="private"]').press("ArrowLeft")
        expect(page.locator('[data-chat-management-tab="groups"]')).to_be_focused()
        assert_content_motion(page, "#groupManagementPanel")

        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="skin"]').click()
        expect(page.locator("#skinOptions")).to_be_visible()
        assert_content_motion(page, ".skin-settings")
        page.locator('.nav-item[data-page="help"]').click()
        page.locator('#helpAudience [data-help-audience="common"]').click()
        assert_content_motion(page, "#helpFeatureGrid")
        page.locator('.nav-item[data-page="schedules"]').click()
        page.locator("#newScheduledTask").click()
        page.locator('[data-scheduled-target-tab="private"]').click()
        assert_content_motion(page, "#scheduledTargetChoices")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        assert not failed_responses, failed_responses
    finally:
        page.close()


def test_feature_tab_keeps_draft_and_guarded_feature_switch_does_not_animate(browser, manager_url):
    page = browser.new_page()
    try:
        open_preview(page, manager_url)
        page.locator('.nav-item[data-page="features"]').click()
        page.locator('[data-feature-id="feature-group-analysis"]').first.click()
        expect(page.locator("#saveFeature")).to_be_enabled()
        field = page.locator('#featureForm [data-path=\'["basic","analysis_days"]\']')
        field.fill("7")
        page.locator('.config-tabs [data-tab="advanced"]').click()
        assert_content_motion(page, "#featureForm")
        expect(field).to_have_value("7")
        page.locator('[data-feature-id="feature-bittorrent"]').first.click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        assert page.evaluate("window.tabAnimations.length") == 0
        page.locator("#modalCancel").click()
        expect(field).to_have_value("7")
        expect(page.locator("#featureDetail h2").first).to_have_text("群聊分析")
        page.locator('[data-feature-id="feature-bittorrent"]').first.click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#featureDetail h2").first).to_have_text("资源检索")
        assert_content_motion(page, "#featureDetail")
    finally:
        page.close()


@pytest.mark.parametrize("reduced", [True, False])
def test_reduced_motion_and_rapid_filters_finish_in_current_state(browser, manager_url, reduced):
    page = browser.new_page(reduced_motion="reduce" if reduced else "no-preference")
    try:
        open_preview(page, manager_url + "&style=soft")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message").first).to_be_visible()
        page.evaluate("""reduced => {
          // Keep the final animation available until reduced-motion is toggled,
          // regardless of how long browser assertions take on a busy CI runner.
          window.pauseHistoryAnimations = !reduced;
          window.tabAnimations = [];
          for (const kind of ['group', 'private', 'all', 'group']) {
            document.querySelector(`#historyKinds [data-kind="${kind}"]`).click();
          }
        }""", reduced)
        expect(page.locator('#historyKinds [data-kind="group"]')).to_have_class("active")
        expect(page.locator('#conversationList .conversation-item:not([data-type="group"])')).to_have_count(0)
        if reduced:
            assert page.evaluate("window.tabAnimations.length") == 0
            page.locator('.nav-item[data-page="settings"]').click()
            page.locator('[data-settings-tab="skin"]').click()
            expect(page.locator("#skinOptions")).to_be_visible()
            assert page.evaluate("window.tabAnimations.length") == 0
        else:
            page.wait_for_function("""() => window.tabAnimations.some(animation =>
              animation.id === 'manager-tab-enter' && animation.playState === 'paused'
              && animation.effect.target.matches('.history-layout'))""", timeout=5000)
            assert page.locator(".history-layout").evaluate("node => node.getAnimations().filter(a => a.id === 'manager-tab-enter').length <= 1")
            page.evaluate("""() => {
              window.activeTabAnimations = window.tabAnimations.filter(animation => animation.playState === 'paused');
            }""")
            assert page.evaluate("window.activeTabAnimations.length") > 0
            page.emulate_media(reduced_motion="reduce")
            expect(page.locator(".history-layout")).to_have_css("transform", "none")
            assert page.evaluate("window.activeTabAnimations.every(animation => animation.playState === 'idle')")
        assert page.locator(".history-layout").evaluate("node => getComputedStyle(node).transform") == "none"
    finally:
        page.close()
