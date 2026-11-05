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


@pytest.mark.parametrize("skin", ["default", "soft"])
@pytest.mark.parametrize("viewport", [{"width": 1520, "height": 900}, {"width": 600, "height": 900}])
def test_paid_subscription_remains_active_and_compact_after_stop(browser, manager_url, skin, viewport, tmp_path):
    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    url = manager_url + f"&license=paused&style={skin}"
    try:
        page.goto(url)
        expect(page.locator("#stateTitle")).to_have_text("机器人尚未启动")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        expect(page.locator("#page-home")).to_be_visible()
        expect(page.locator("#globalStatus")).to_have_text("等待启动")
        expect(page.locator("#homeLicenseNotice")).to_be_hidden()
        assert page.evaluate("window.pywebview.api.get_status().then(result => result.data.online)") is False
        page.locator('.nav-item[data-page="subscription"]').click()
        expect(page.locator("#licenseStateTitle")).to_have_text("订阅已生效")
        expect(page.locator("#licenseStateTag")).to_have_text("已订阅")
        expect(page.locator("#licenseStatusPanel")).to_have_attribute("data-tone", "success")
        expect(page.locator("#licenseStateMessage")).not_to_contain_text("暂停")
        expect(page.locator("#licenseSteps")).to_be_hidden()
        expect(page.locator("#licensePausedInfo")).to_be_hidden()
        expect(page.locator("#licenseCurrentAccountRow")).to_be_hidden()
        expect(page.locator("#licenseResume")).to_be_enabled()
        page.evaluate("document.fonts.ready")
        assert page.locator("#licenseStatusPanel").bounding_box()["height"] < (330 if viewport["width"] > 800 else 460)
        assert "Alibaba PuHuiTi 2" in page.locator("#licenseStateTitle").evaluate("el => getComputedStyle(el).fontFamily")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        assert page.title() and "preview=1" in page.url
        page.screenshot(path=str(tmp_path / f"paid-{skin}-{viewport['width']}.png"))
        page.locator("#licenseResume").click()
        expect(page.locator("#licenseResume")).to_have_text("自动化运行中")
        expect(page.locator("#globalStatus")).to_have_text("机器人可用")
        assert not errors, errors
    finally:
        page.close()


def test_unactivated_entry_and_start_toast_return_to_subscription(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 1520, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url + "&license=unactivated&style=soft")
        expect(page.locator("#page-subscription")).to_be_visible()
        expect(page.locator("#licenseSteps")).to_be_visible()
        page.locator('.nav-item[data-page="home"]').click()
        page.locator("#primaryAction").click()
        expect(page.locator("#toastRegion")).to_contain_text("尚未授权，请先完成授权后再启动。")
        expect(page.locator("#page-subscription")).to_be_visible()
        expect(page.locator("#licenseFeedback")).to_have_text("尚未授权，请先完成授权后再启动。")
        assert page.evaluate("window.pywebview.api.get_status().then(result => result.data.online)") is False
        assert not errors, errors
        page.screenshot(path=str(tmp_path / "unactivated-start-toast.png"))
    finally:
        page.close()


@pytest.mark.parametrize("viewport", [{"width": 1520, "height": 900}, {"width": 600, "height": 900}])
def test_revoked_subscription_previews_new_month_without_automatic_start(browser, manager_url, viewport, tmp_path):
    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    url = manager_url + "&license=unactivated"
    try:
        page.goto(url)
        expect(page.locator("#page-subscription")).to_be_visible()
        page.evaluate("""async () => {
          const api = window.pywebview.api;
          const original = await api.get_license_status();
          window.testActivationStarts = 0;
          window.testActivationSnapshot = {...original.data, state: 'revoked', license_id: 'revoked-year',
            plan: 'year', account: 'wxid_demo_account', current_account: 'wxid_demo_account',
            authorized: false, can_connect: false, can_run: false,
            plans: original.data.plans.map(plan => ({...plan,
              id: {monthly: 'month', yearly: 'year'}[plan.id] || plan.id}))};
          api.get_license_status = async () => ({ok: true, data: structuredClone(window.testActivationSnapshot)});
          api.preview_activation_code = async () => ({ok: true, data: {preview_id: 'new-month', plan: 'month',
            mode: 'new', valid_until: Math.floor(Date.now() / 1000) + 30 * 86400, license_id: '',
            already_redeemed: false,
            message: '旧授权保持撤销；确认后新开通授权，从本次兑换起算，不继承旧授权的剩余时长。兑换后需重新绑定微信并手动恢复机器人。'}});
          api.redeem_activation_code = async () => {
            Object.assign(window.testActivationSnapshot, {state: 'active_unbound', license_id: 'new-month',
              plan: 'month', account: '', authorized: true, can_connect: true});
            return {ok: true, data: structuredClone(window.testActivationSnapshot)};
          };
          api.start_robot = async () => { window.testActivationStarts += 1; throw new Error('unexpected start'); };
        }""")
        page.locator('.nav-item[data-page="home"]').click()
        page.locator('.nav-item[data-page="subscription"]').click()
        expect(page.locator("#licenseStateTitle")).to_have_text("授权已撤销")
        expect(page.locator("#licenseRedeemTitle")).to_have_text("兑换激活码")
        page.locator("#licenseCode").fill("NEW-MONTH-CODE")
        page.locator("#licenseCodeForm button[type=submit]").click()
        expect(page.locator("#licenseCodePreview")).to_be_visible()
        expect(page.locator("#licenseCodePreviewDetails")).to_contain_text("月付")
        expect(page.locator("#licenseCodePreviewDetails")).to_contain_text("新开通")
        expect(page.locator("#licenseCodePreviewMessage")).to_contain_text("旧授权保持撤销")
        expect(page.locator("#licenseCodePreviewMessage")).to_contain_text("从本次兑换起算")
        expect(page.locator("#licenseCodePreviewMessage")).to_contain_text("手动恢复机器人")
        page.screenshot(path=str(tmp_path / f"revoked-new-month-preview-{viewport['width']}.png"))
        page.locator("#licenseConfirmRedeem").click()
        expect(page.locator("#licenseBind")).to_be_visible()
        expect(page.locator("#licenseFeedback")).to_contain_text("微信绑定")
        assert page.evaluate("window.testActivationStarts") == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.url == url and page.title()
        assert not errors, errors
    finally:
        page.close()
