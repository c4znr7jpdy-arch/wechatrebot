"""Run the delivered subscription UI's activation and expiry regressions."""
from pathlib import Path
import shutil
import subprocess
import tempfile

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def save_flow_screenshot(page, name):
    output = Path(tempfile.gettempdir()) / "auth-wording-qa"
    output.mkdir(exist_ok=True)
    page.screenshot(path=str(output / name))


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
        expect(page.locator("#licenseActivateTrial")).to_be_enabled()
        expect(page.locator('[data-plan="trial"] #licenseTrialStatus')).to_have_count(1)
        expect(page.locator('[data-plan="trial"] #licenseTrialRetry')).to_have_count(1)
        page.locator("#licenseActivateTrial").click()
        expect(page.locator("#modalTitle")).to_have_text("确认激活免费试用")
        expect(page.locator("#modalMessage")).to_contain_text("接下来登录的微信号")
        expect(page.locator("#modalSubject")).to_be_hidden()
        page.locator("#modalConfirm").click()
        expect(page.locator("#licenseStateTitle")).to_have_text("免费试用已生效")
        expect(page.locator("#licenseOptions")).not_to_have_attribute("open", "")
        expect(page.locator("#licenseConnect, #licenseBind, #licenseResume")).to_have_count(0)
        expect(page.locator("#licenseRebind")).to_be_hidden()
        assert page.evaluate("window.pywebview.api.get_status().then(result => result.data.online)") is False
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        assert page.url == url and page.title()
        page.screenshot(path=str(tmp_path / "free-trial-active.png"))
    finally:
        page.close()


@pytest.mark.parametrize("code,title,message,solution", [
    ("11001", "未检测到微信", "这台电脑尚未安装可用的微信客户端。", "下载安装微信客户端"),
    ("11001", "微信路径无效", "此前选择的微信主程序已不存在。", "重新选择微信主程序"),
    ("11002", "微信版本不兼容", "当前微信版本无法连接。", "安装兼容版本后重新检测"),
])
def test_trial_connection_shows_environment_guidance_before_login(
    browser, manager_url, code, title, message, solution,
):
    page = browser.new_page(viewport={"width": 1080, "height": 680})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url + "&demo=subscription&license=unactivated")
        expect(page.locator("#licenseActivateTrial")).to_be_enabled()
        page.evaluate("""problem => {
          const api = window.pywebview.api, status = api.get_status;
          window.connectionAttempts = 0;
          api.get_status = async () => {
            const result = structuredClone(await status());
            result.data.environment = {...result.data.environment, ...problem, ready: false};
            result.data.can_start = false;
            return result;
          };
          api.check_environment = api.get_status;
          api.connect_license_wechat = async () => {
            window.connectionAttempts += 1;
            return {ok: true, data: {accepted: true}};
          };
        }""", {"code": code, "title": title, "message": message, "solutions": [solution]})
        page.locator("#licenseActivateTrial").click()
        expect(page.locator("#modalTitle")).to_have_text("确认激活免费试用")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#page-home")).to_be_visible()
        expect(page.locator("#environmentAlert")).to_be_visible()
        expect(page.locator("#environmentTitle")).to_have_text(title)
        expect(page.locator("#environmentMessage")).to_have_text(message)
        expect(page.locator("#environmentSolutions")).to_contain_text(solution)
        assert page.evaluate("window.connectionAttempts") == 0
        assert not errors, errors
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
        expect(page.locator("#licenseConnect, #licenseBind, #licenseResume")).to_have_count(0)
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
        expect(page.locator("#licenseSteps, #licenseCurrentAccountRow")).to_have_count(0)
        expect(page.locator("#licensePausedInfo")).to_be_hidden()
        expect(page.locator("#licenseOptions")).not_to_have_attribute("open", "")
        page.evaluate("document.fonts.ready")
        assert page.locator("#licenseStatusPanel").bounding_box()["height"] < (330 if viewport["width"] > 800 else 460)
        assert "Alibaba PuHuiTi 2" in page.locator("#licenseStateTitle").evaluate("el => getComputedStyle(el).fontFamily")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        assert page.title() and "preview=1" in page.url
        page.screenshot(path=str(tmp_path / f"paid-{skin}-{viewport['width']}.png"))
        page.locator('.nav-item[data-page="home"]').click()
        page.locator("#primaryAction").click()
        expect(page.locator("#globalStatus")).to_have_text("机器人可用")
        expect(page.locator("#modalBackdrop")).to_be_hidden()
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
        expect(page.locator("#licenseOptions")).to_have_attribute("open", "")
        page.locator('.nav-item[data-page="home"]').click()
        page.locator("#primaryAction").click()
        expect(page.locator("#toastRegion")).to_contain_text("当前未授权。请激活免费试用或兑换付费激活码，再启动机器人。")
        expect(page.locator("#page-subscription")).to_be_visible()
        expect(page.locator("#licenseFeedback")).to_have_text("当前未授权。请激活免费试用或兑换付费激活码，再启动机器人。")
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
          window.testActivationPreviews = 0;
          window.testActivationSnapshot = {...original.data, state: 'revoked', license_id: 'revoked-year',
            plan: 'year', account: 'wxid_demo_account', current_account: 'wxid_demo_account',
            authorized: false, can_connect: false, can_run: false,
            message: '授权已撤销，自动化已暂停，请联系管理员。',
            plans: original.data.plans.map(plan => ({...plan,
              id: {monthly: 'month', yearly: 'year'}[plan.id] || plan.id}))};
          api.get_license_status = async () => ({ok: true, data: structuredClone(window.testActivationSnapshot)});
          const runtime = api.get_status;
          api.get_status = async () => {
            const result = await runtime();
            result.data = {...result.data, state: 'LICENSE_PAUSED', title: '授权已撤销',
              message: window.testActivationSnapshot.message, online: false};
            return result;
          };
          api.refresh_license = async () => ({ok: false, error: {code: 'LIC-license_revoked',
            message: '授权已撤销，自动化已暂停，请联系管理员。'}});
          api.preview_activation_code = async () => { window.testActivationPreviews += 1; return {ok: true, data: {preview_id: 'new-month', plan: 'month',
            mode: 'new', valid_until: Math.floor(Date.now() / 1000) + 30 * 86400, license_id: '',
            already_redeemed: false,
            message: '旧授权保持撤销；确认后新开通授权，从本次兑换起算，不继承旧授权的剩余时长。兑换后需重新绑定微信并手动恢复机器人。'}}; };
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
        message = "授权已撤销，当前未授权。请联系管理员或兑换新的付费激活码。"
        expect(page.locator("#licenseStateMessage")).to_have_text(message)
        expect(page.locator("#licensePausedInfo")).to_have_text(
            "机器人未运行。微信和本机数据保留，处理授权问题后可手动启动；未运行期间的任务不会补发。")
        page.locator("#licenseRefresh").click()
        expect(page.locator("#licenseFeedback")).to_have_text(message)
        page.locator('.nav-item[data-page="home"]').click()
        expect(page.locator("#homeLicenseMessage")).to_have_text(message)
        expect(page.locator("#stateMessage")).to_have_text(message)
        page.locator("#primaryAction").click()
        expect(page.locator("#toastRegion")).to_contain_text(message)
        expect(page.locator("#page-subscription")).to_be_visible()
        expect(page.locator("#licenseFeedback")).to_have_text(message)
        expect(page.locator("#licenseRedeemTitle")).to_have_text("兑换激活码")
        business_number = "WRB-20991231235959-0123456789ABCDEF"
        page.locator("#licenseCode").fill(business_number)
        page.locator("#licenseCodeForm button[type=submit]").click()
        expect(page.locator("#licenseCodeFeedback")).to_have_text("这是业务编号，请复制详情中的完整激活码。")
        expect(page.locator("#licenseCode")).to_have_value(business_number)
        assert page.evaluate("window.testActivationPreviews") == 0
        page.locator("#licenseCode").fill("NEW-MONTH-CODE")
        page.locator("#licenseCodeForm button[type=submit]").click()
        expect(page.locator("#licenseCodePreview")).to_be_visible()
        assert page.evaluate("window.testActivationPreviews") == 1
        expect(page.locator("#licenseCodePreviewDetails")).to_contain_text("月付")
        expect(page.locator("#licenseCodePreviewDetails")).to_contain_text("新开通")
        expect(page.locator("#licenseCodePreviewMessage")).to_contain_text("旧授权保持撤销")
        expect(page.locator("#licenseCodePreviewMessage")).to_contain_text("从本次兑换起算")
        expect(page.locator("#licenseCodePreviewMessage")).to_contain_text("手动恢复机器人")
        page.screenshot(path=str(tmp_path / f"revoked-new-month-preview-{viewport['width']}.png"))
        page.locator("#licenseConfirmRedeem").click()
        expect(page.locator("#licenseStateTitle")).to_have_text("订阅已生效")
        expect(page.locator("#licenseOptions")).not_to_have_attribute("open", "")
        expect(page.locator("#licenseFeedback")).to_contain_text("首次启动时先确认绑定提示，再登录微信")
        assert page.evaluate("window.testActivationStarts") == 0
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.url == url and page.title()
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("viewport", [{"width": 1520, "height": 900}, {"width": 600, "height": 900}])
def test_start_requests_consent_before_connecting_wechat(browser, manager_url, viewport):
    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    url = manager_url + "&license=pending&style=soft"
    try:
        page.goto(url)
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          window.flowCalls = {bind_license_account: 0, start_robot: 0, connect_license_wechat: 0};
          for (const method of Object.keys(window.flowCalls)) {
            const original = window.pywebview.api[method];
            window.pywebview.api[method] = (...args) => { window.flowCalls[method]++; return original(...args); };
          }
        }""")
        page.locator('.nav-item[data-page="subscription"]').click()
        expect(page.locator("#licenseStateTitle")).to_have_text("订阅已生效")
        expect(page.locator("#licenseOptions")).not_to_have_attribute("open", "")
        expect(page.locator("#licenseConnect, #licenseBind, #licenseResume, #licenseSteps")).to_have_count(0)
        save_flow_screenshot(page, f"subscription-active-{viewport['width']}.png")
        page.locator('.nav-item[data-page="home"]').click()
        for cancel in ("button", "escape", "backdrop"):
            page.locator("#primaryAction").click()
            expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
            expect(page.locator("#modalMessage")).to_contain_text("接下来登录的微信号将绑定当前套餐")
            expect(page.locator("#modalMessage")).to_contain_text("剩余可换绑")
            expect(page.locator("#modalSubject")).to_be_hidden()
            expect(page.locator("#primaryAction")).to_be_disabled()
            assert page.evaluate("Object.values(window.flowCalls).reduce((sum, value) => sum + value, 0)") == 0
            expect(page.locator("#modalMessage")).not_to_contain_text("wxid_demo_account")
            if cancel == "button":
                page.locator("#modalCancel").click()
            elif cancel == "escape":
                page.keyboard.press("Escape")
            else:
                page.locator("#modalBackdrop").click(position={"x": 3, "y": 3})
            expect(page.locator("#modalBackdrop")).to_be_hidden()
            expect(page.locator("#primaryAction")).to_be_enabled()
            assert page.evaluate("Object.values(window.flowCalls).reduce((sum, value) => sum + value, 0)") == 0
        page.locator("#primaryAction").click()
        expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
        save_flow_screenshot(page, f"start-binding-consent-{viewport['width']}.png")
        page.locator("#modalConfirm").click()
        expect(page.locator("#globalStatus")).to_have_text("机器人可用")
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        assert page.evaluate("window.flowCalls") == {
            "bind_license_account": 1, "start_robot": 1, "connect_license_wechat": 1}
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.title() and page.url.startswith(manager_url) and "license=pending" in page.url
        assert not errors, errors
    finally:
        page.close()


def test_start_waits_for_login_and_recovers_from_connect_or_binding_failure(browser, manager_url):
    page = browser.new_page()
    try:
        page.goto(manager_url + "&license=pending")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api;
          window.originalConnect = api.connect_license_wechat;
          window.originalBind = api.bind_license_account;
          window.bindCount = 0;
          api.connect_license_wechat = async () => ({ok: false, error: {message: '连接暂时失败，请重试'}});
          api.bind_license_account = (...args) => { window.bindCount++; return window.originalBind(...args); };
        }""")
        page.locator("#primaryAction").click()
        expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
        page.locator("#modalConfirm").click()
        expect(page.locator("#toastRegion")).to_contain_text("连接暂时失败，请重试")
        expect(page.locator("#primaryAction")).to_be_enabled()
        page.evaluate("window.pywebview.api.connect_license_wechat = async () => ({ok: true, data: {accepted: true}})")
        page.locator("#primaryAction").click()
        expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalTitle")).to_have_text("请登录微信")
        page.keyboard.press("Escape")
        expect(page.locator("#primaryAction")).to_be_enabled()
        assert page.evaluate("window.bindCount") == 0
        page.locator("#primaryAction").click()
        expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalTitle")).to_have_text("请登录微信")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalError")).to_contain_text("尚未读取到登录的微信账号")
        expect(page.locator("#modalCancel")).to_be_enabled()
        page.evaluate("window.originalConnect()")
        page.evaluate("window.pywebview.api.bind_license_account = async () => ({ok: false, error: {message: '绑定服务暂时不可达'}})")
        page.locator("#modalConfirm").click()
        expect(page.locator("#toastRegion")).to_contain_text("绑定服务暂时不可达")
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#primaryAction")).to_be_enabled()
        page.evaluate("window.pywebview.api.bind_license_account = window.originalBind")
        page.locator("#primaryAction").click()
        expect(page.locator("#modalTitle")).to_have_text("确认绑定当前套餐")
        page.locator("#modalConfirm").click()
        expect(page.locator("#globalStatus")).to_have_text("机器人可用")
    finally:
        page.close()


@pytest.mark.parametrize("viewport", [{"width": 1520, "height": 900}, {"width": 600, "height": 900}])
def test_self_service_rebind_requires_new_account_and_keeps_robot_stopped(browser, manager_url, viewport):
    page = browser.new_page(viewport=viewport)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url + "&license=active")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async () => {
          const api = window.pywebview.api;
          window.bindingSnapshot = (await api.get_license_status()).data;
          window.bindingCalls = {stop: 0, rebind: 0, connect: 0, start: 0};
          window.bindingRemaining = 1;
          window.bindingRevision = 41;
          api.get_license_status = async () => ({ok: true, data: structuredClone(window.bindingSnapshot)});
          api.get_license_binding_status = async () => ({ok: true, data: {
            license_id: window.bindingSnapshot.license_id, account: window.bindingSnapshot.account,
            plan: window.bindingSnapshot.plan, revision: window.bindingRevision,
            rebind_limit: 1, rebind_used: 1 - window.bindingRemaining,
            rebind_remaining: window.bindingRemaining, can_rebind: window.bindingRemaining > 0}});
          const stop = api.stop_robot;
          api.stop_robot = (...args) => { window.bindingCalls.stop++; return stop(...args); };
          api.connect_license_wechat = async () => { window.bindingCalls.connect++; throw new Error('unexpected connection'); };
          api.start_robot = async () => { window.bindingCalls.start++; throw new Error('unexpected start'); };
          api.rebind_license_account = async (next, previous, revision) => {
            if (next !== window.bindingSnapshot.current_account || previous !== window.bindingSnapshot.account
              || next === previous || revision !== window.bindingRevision) throw new Error('invalid rebind request');
            window.bindingCalls.rebind++;
            window.bindingRemaining--; window.bindingRevision++;
            Object.assign(window.bindingSnapshot, {account: next, state: 'paused', can_run: false});
            return {ok: true, data: structuredClone(window.bindingSnapshot)};
          };
        }""")
        page.locator('.nav-item[data-page="subscription"]').click()
        expect(page.locator("#licenseRebindSummary")).to_be_visible()
        expect(page.locator("#licenseRebindQuota")).to_have_text("1")
        expect(page.locator("#licenseRebindQuota")).to_have_attribute("aria-label", "剩余可换绑 1 次（已用 0 / 1 次）。首次绑定不扣换绑次数。")
        page.locator("#licenseRebind").click()
        expect(page.locator("#modalTitle")).to_have_text("更换套餐绑定微信")
        expect(page.locator("#modalSubjectLabel")).to_have_text("当前绑定微信")
        expect(page.locator("#modalSubjectValue")).to_have_text("wxid_demo_account")
        expect(page.locator("#modalMessage")).to_contain_text("剩余可换绑 1 次")
        save_flow_screenshot(page, f"rebind-consent-{viewport['width']}.png")
        page.locator("#modalCancel").click()
        expect(page.locator("#licenseRebind")).to_be_enabled()
        assert page.evaluate("Object.values(window.bindingCalls).reduce((sum, value) => sum + value, 0)") == 0
        page.locator("#licenseRebind").click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalTitle")).to_have_text("请切换微信账号")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalError")).to_contain_text("仍是原绑定微信")
        assert page.evaluate("window.bindingRemaining") == 1
        assert page.evaluate("window.bindingCalls.rebind") == 0
        page.locator("#modalCancel").click()
        expect(page.locator("#licenseFeedback")).to_contain_text("已取消换绑，原绑定和换绑次数保留")
        expect(page.locator("#licenseRebind")).to_be_enabled()
        page.locator("#licenseRebind").click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalTitle")).to_have_text("请切换微信账号")
        page.evaluate("window.bindingSnapshot.current_account = 'wxid_new_account'")
        page.locator("#modalConfirm").click()
        expect(page.locator("#licenseFeedback")).to_contain_text("微信换绑已完成。机器人保持停止")
        expect(page.locator("#licenseAccount")).to_have_text("wxid_new_account")
        expect(page.locator("#licenseRebindQuota")).to_have_text("0")
        expect(page.locator("#licenseRebind")).to_be_disabled()
        assert page.evaluate("window.bindingCalls") == {"stop": 2, "rebind": 1, "connect": 0, "start": 0}
        assert page.evaluate("window.pywebview.api.get_status().then(result => result.data.online)") is False
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        save_flow_screenshot(page, f"rebind-completed-{viewport['width']}.png")
    finally:
        page.close()
