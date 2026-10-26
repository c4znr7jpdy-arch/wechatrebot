"""Run the delivered subscription UI's activation and expiry regressions."""
from pathlib import Path
import shutil
import subprocess

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def test_subscription_confirmation_expiry_and_recovery():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    script = Path(__file__).with_name("subscription_webui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize("viewport", [{"width": 1380, "height": 860}, {"width": 600, "height": 900}])
def test_preview_free_trial_connects_and_activates_without_start(browser, manager_url, viewport, tmp_path):
    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    url = manager_url + "&demo=subscription&license=unactivated"
    try:
        page.goto(url)
        expect(page.locator("#page-subscription")).to_be_visible()
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        expect(page.locator("#licenseActivateTrial")).to_be_disabled()
        expect(page.locator('[data-plan="trial"] #licenseTrialStatus')).to_have_count(1)
        expect(page.locator('[data-plan="trial"] #licenseTrialRetry')).to_have_count(1)
        page.locator("#licenseConnect").click()
        expect(page.locator("#licenseActivateTrial")).to_be_enabled()
        page.locator("#licenseActivateTrial").click()
        expect(page.locator("#licenseActivateTrial")).to_have_text("试用进行中")
        expect(page.locator("#licenseTrialStatus")).to_contain_text("免费试用进行中")
        expect(page.locator("#licenseResume")).to_be_enabled()
        assert page.evaluate("window.pywebview.api.get_status().then(result => result.data.online)") is False
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        assert page.url == url and page.title()
        page.screenshot(path=str(tmp_path / "free-trial-active.png"))
    finally:
        page.close()


@pytest.mark.parametrize("state", ["trial-ended", "trial-used"])
def test_preview_trial_end_message_is_exact(browser, manager_url, state):
    page = browser.new_page()
    try:
        page.goto(manager_url + f"&demo=subscription&license={state}")
        expect(page.locator("#page-subscription")).to_be_visible()
        expect(page.locator("#licenseTrialStatus")).to_have_text("你的试用已结束，可联系群主购买"
            if state == "trial-ended" else "当前设备或微信账号已领取过试用，可联系群主购买")
        expect(page.locator("#licenseActivateTrial")).to_be_disabled()
        expect(page.locator("#licenseResume")).to_be_hidden()
    finally:
        page.close()
