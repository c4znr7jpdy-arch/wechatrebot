"""Exercise update consent in the shipped UI without downloading or stopping anything."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.fixture
def update_page(browser, manager_url):
    page = browser.new_page(viewport={"width": 1380, "height": 860}, reduced_motion="reduce")
    errors, failed_responses = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failed_responses.append(response.url) if response.status >= 400 else None)
    # Capture the actual controller, including its normal automatic-check entrypoint.
    page.add_init_script("""(() => {
      Object.defineProperty(window, 'createUpdateNotifications', {
        configurable: true,
        set(create) {
          Object.defineProperty(window, 'createUpdateNotifications', {value: helpers => {
            window.updateTestState = helpers.state;
            window.updateTestController = create(helpers);
            return window.updateTestController;
          }});
        }
      });
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          clearTimeout(window.updateTestState.updateCheckTimer);
          const api = window.pywebview.api, ok = data => ({ok: true, data});
          window.updateCalls = {download: [], install: [], effects: []};
          window.testRelease = {version: '1.0.1', build: 'first', release_id: 'release-first',
            title: '更新体验改进', notes: ['下载完成后确认安装'], package_size: 10485760, mandatory: false};
          window.testUpdateStatus = {state: 'idle', phase: 'idle'};
          api.check_update = async () => ok({enabled: true, release: window.testRelease});
          api.get_update_status = async () => ok(window.testUpdateStatus);
          api.download_update = async (...args) => {
            window.updateCalls.download.push(args);
            window.testUpdateStatus = {state: 'running', phase: 'download', percent: 25,
              release_id: args[1], version: args[0], message: '正在下载更新包'};
            return ok({accepted: true, version: args[0]});
          };
          window.finishUpdateDownload = () => {
            window.testUpdateStatus = {state: 'ready', phase: 'ready_to_install', percent: 100,
              version: window.testRelease.version, release_id: window.testRelease.release_id,
              confirmation_token: `consent-${window.testRelease.release_id}`, message: '更新包已校验'};
          };
          api.install_update = async (...args) => {
            window.updateCalls.install.push(args);
            window.updateCalls.effects.push('stop', 'install');
            window.testUpdateStatus = {...window.testUpdateStatus, state: 'restarting', phase: 'launching'};
            return ok({accepted: true, version: args[0]});
          };
        }""")
        yield page
        assert page.url == manager_url
        assert "WxRebot" in page.title()
        assert not failed_responses, failed_responses
        assert not errors, errors
    finally:
        page.close()


def _check_automatically(page):
    page.evaluate("window.updateTestController.scheduleAutomaticUpdateCheck(0)")
    expect(page.locator("#modalTitle")).to_have_text("发现新版本 v1.0.1")
    expect(page.locator("#modalBackdrop")).to_be_visible()


def _download_and_wait_for_consent(page):
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardMessage")).to_contain_text("正在下载更新包")
    assert page.evaluate("window.updateCalls.download") == [["1.0.1", "release-first"]]
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("window.finishUpdateDownload()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    expect(page.locator("#modalConfirm")).to_have_text("确认关闭并安装")
    expect(page.locator("#modalConfirm")).to_be_enabled()


def _open_update_settings(page):
    page.locator('.nav-item[data-page="settings"]').click()
    page.locator('[data-settings-tab="system"]').click()


def _open_history_cleanup(page):
    page.locator('.nav-item[data-page="history"]').click()
    expect(page.locator("#messageList .message-bubble").first).to_be_visible()
    _open_update_settings(page)
    page.locator("#cleanHistory").click()
    expect(page.locator("#modalTitle")).to_have_text("按日期清理聊天记录")


@pytest.mark.parametrize("width,height", [(1380, 860), (1080, 680)])
def test_update_download_then_explicit_shutdown_consent(update_page, tmp_path: Path, width, height):
    page = update_page
    page.set_viewport_size({"width": width, "height": height})
    _check_automatically(page)
    assert page.evaluate("window.updateCalls.download") == []
    expect(page.locator("#modalMessage")).to_contain_text("10.0 MB")
    page.screenshot(path=str(tmp_path / f"update-available-{width}.png"))
    # The first click downloads directly; there is no extra pre-download confirmation.
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardMessage")).to_contain_text("正在下载更新包")
    expect(page.locator("#updateCardPercent")).to_have_text("25%")
    expect(page.locator("#updateCardProgress")).to_have_attribute("aria-valuenow", "25")
    assert not page.locator(".app-shell").evaluate("node => node.inert")
    # Existing navigation remains available while the update card is visible.
    page.locator('.nav-item[data-page="groups"]').click()
    expect(page.locator("#page-groups")).to_be_visible()
    page.screenshot(path=str(tmp_path / f"update-background-{width}.png"))
    assert page.evaluate("window.updateCalls.download") == [["1.0.1", "release-first"]]
    assert page.evaluate("window.updateCalls.effects") == []
    page.evaluate("window.finishUpdateDownload()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    expect(page.locator("#modalMessage")).to_contain_text("更新会强制关闭机器人程序及当前连接的微信，是否确认？")
    expect(page.locator("#modalMessage")).to_contain_text("自动打开新版管理器")
    expect(page.locator("#modalConfirm")).to_be_enabled()
    assert page.evaluate("window.updateCalls.effects") == []
    page.screenshot(path=str(tmp_path / f"update-install-confirmation-{width}.png"))
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
    assert page.evaluate("window.updateCalls.effects") == ["stop", "install"]


def test_cancel_install_keeps_ready_package_and_manual_retry_skips_download(update_page):
    page = update_page
    _download_and_wait_for_consent(page)
    page.locator("#modalCancel").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardAction")).to_have_text("立即安装")
    page.evaluate("window.updateTestController.pollUpdateStatus()")
    page.evaluate("window.updateTestController.refreshUpdate(false)")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.testUpdateStatus.state") == "ready"
    assert page.evaluate("window.updateCalls.effects") == []
    _open_update_settings(page)
    page.locator("#checkUpdate").click()
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    assert len(page.evaluate("window.updateCalls.download")) == 1
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]


def test_failed_window_close_offers_close_retry_and_protects_explicit_exit(update_page, tmp_path):
    page = update_page
    _download_and_wait_for_consent(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("""() => {
      window.testUpdateStatus = {...window.testUpdateStatus, state:'ready', phase:'close_failed',
        close_pending:true, confirmation_token:'close-retry',
        error:{code:'UPD-1007',message:'管理器未能自动关闭，请再次关闭管理器以继续更新。'}};
      window.pywebview.api.install_update = async (...args) => {
        window.updateCalls.install.push(args);
        window.testUpdateStatus = {...window.testUpdateStatus, state:'restarting',close_pending:false};
        return {ok:true,data:{accepted:true}};
      };
      return window.updateTestController.pollUpdateStatus();
    }""")
    expect(page.locator("#modalTitle")).to_have_text("再次关闭管理器以继续更新？")
    expect(page.locator("#modalMessage")).to_contain_text("同一次更新")
    page.locator("#modalCancel").click()
    expect(page.locator("#updateCardTitle")).to_have_text("管理器未能关闭")
    expect(page.locator("#updateCardAction")).to_have_text("再次关闭管理器")
    assert page.locator("#updateCardDismiss").is_hidden()
    assert page.evaluate("""() => {
      const event = new Event('beforeunload',{cancelable:true});
      dispatchEvent(event); return event.defaultPrevented;
    }""")
    page.screenshot(path=str(tmp_path / "update-close-retry.png"))
    page.locator("#updateCardAction").click()
    expect(page.locator("#modalConfirm")).to_have_text("再次关闭管理器")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.effects") == ["stop", "install"]
    assert len(page.evaluate("window.updateCalls.download")) == 1
    assert page.evaluate("window.updateCalls.install")[-1] == ["1.0.1", "close-retry"]


@pytest.mark.parametrize("editor", ["core", "provider", "feature", "schedule", "search", "group"])
def test_install_preserves_unsaved_settings_and_the_downloaded_package(update_page, tmp_path, editor):
    page = update_page
    if editor == "core":
        page.set_viewport_size({"width": 1080, "height": 680})
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    if editor in {"core", "provider", "search"}:
        page.locator('.nav-item[data-page="settings"]').click()
        if editor == "core":
            page.locator('[data-settings-tab="core"]').click()
            field = page.locator('#coreForm [name="robot_name"]')
            expect(field).to_have_value("小妹")
            field.fill("尚未保存的核心设置")
        elif editor == "provider":
            page.locator('[data-provider-create]').click()
            field = page.locator('#providerModelInput')
            field.fill("unsaved-test-model")
        else:
            page.locator('[data-settings-tab="search"]').click()
            field = page.locator('#webSearchForm [name="enabled"]')
            page.locator('label[for="webSearchEnabled"]').click()
    elif editor == "feature":
        page.evaluate("""() => {
          const read = window.pywebview.api.get_feature;
          window.pywebview.api.get_feature = async (...args) => {
            const result = await read(...args);
            // This test exercises the schema draft, without an optional native page.
            if (result.data) result.data.pages = [];
            return result;
          };
        }""")
        page.locator('.nav-item[data-page="features"]').click()
        page.locator('.feature-item[data-feature-id="feature-group-analysis"]').click()
        field = page.locator('#config-basic--analysis_days')
        field.fill("7")
    elif editor == "schedule":
        page.locator('.nav-item[data-page="schedules"]').click()
        page.locator('#newScheduledTask').click()
        field = page.locator('#scheduledName')
        field.fill("尚未保存的定时任务")
    else:
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('#groupRows .group-row').first.click()
        field = page.locator('#groupPolicyForm [name="reply_mode"]')
        field.select_option("off")
    saved_value = field.is_checked() if editor == "search" else field.input_value()
    page.evaluate("window.finishUpdateDownload(); window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_contain_text("有设置尚未保存")
    expect(page.locator("#modalError")).to_contain_text("无需重新下载")
    assert page.evaluate("window.updateCalls.install") == []
    page.screenshot(path=str(tmp_path / f"update-keeps-{editor}-draft.png"), animations="disabled")
    page.locator("#modalCancel").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert (field.is_checked() if editor == "search" else field.input_value()) == saved_value
    expect(page.locator("#updateCardAction")).to_have_text("立即安装")
    assert len(page.evaluate("window.updateCalls.download")) == 1
    assert page.evaluate("window.testUpdateStatus.state") == "ready"
    if editor == "core":
        page.locator('#updateCardDismiss').click()
        expect(page.locator('#updateCard')).to_be_hidden()
        page.evaluate("window.updateTestController.pollUpdateStatus()")
        expect(page.locator('#updateCard')).to_be_hidden()
        page.screenshot(path=str(tmp_path / 'update-card-dismissed-core-save.png'), animations="disabled")
        page.locator('#saveCore').click()
        expect(page.locator('#coreNote')).to_contain_text("已更新")
        expect(page.locator('#saveCore')).to_be_enabled()
        page.locator('[data-settings-tab="system"]').click()
        page.locator('#checkUpdate').click()
        expect(page.locator('#modalTitle')).to_have_text("安装 v1.0.1？")
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
        assert len(page.evaluate("window.updateCalls.download")) == 1


@pytest.mark.parametrize("width,theme", [(1380, "light"), (600, "dark")])
def test_ready_card_can_be_dismissed_without_hiding_a_new_confirmation(update_page, tmp_path, width, theme):
    page = update_page
    page.set_viewport_size({"width": width, "height": 820})
    if page.locator("html").get_attribute("data-theme") != theme:
        page.locator("#themeToggle").click()
    _download_and_wait_for_consent(page)
    page.locator('#modalCancel').click()
    expect(page.locator('#updateCardDismiss')).to_be_visible()
    page.screenshot(path=str(tmp_path / f'update-ready-card-{width}-{theme}.png'), animations="disabled")
    page.locator('#updateCardDismiss').click()
    page.evaluate("window.updateTestController.pollUpdateStatus()")
    expect(page.locator('#updateCard')).to_be_hidden()
    assert page.evaluate("window.testUpdateStatus.state") == "ready"
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("""() => {
      window.testUpdateStatus.confirmation_token = 'new-confirmation-token';
      return window.updateTestController.pollUpdateStatus();
    }""")
    expect(page.locator('#modalTitle')).to_have_text("安装 v1.0.1？")
    expect(page.locator('#modalBackdrop')).to_be_visible()
    expect(page.locator('#updateCard')).to_be_visible()
    assert len(page.evaluate("window.updateCalls.download")) == 1


@pytest.mark.parametrize("busy", ["featureSaving", "webSearchSaving", "scheduledBusy", "historyExporting",
                                  "startPreparing", "startPending", "stopPending", "stopRequestPending"])
def test_install_rechecks_work_in_progress_then_reuses_ready_package(update_page, busy):
    page = update_page
    _download_and_wait_for_consent(page)
    page.evaluate("name => { window.updateTestState[name] = true; }", busy)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_contain_text("请稍候")
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("name => { window.updateTestState[name] = false; }", busy)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
    assert len(page.evaluate("window.updateCalls.download")) == 1


@pytest.mark.parametrize("operation", ["start_preparing", "stop_pending"])
def test_install_waits_for_real_robot_request_even_after_idle_status(update_page, operation):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("""async operation => {
      const api = window.pywebview.api, idle = structuredClone((await api.get_status()).data);
      Object.assign(idle, {state:'LICENSE_READY', title:'机器人未启动', message:'', busy:false,
        online:false, can_start:true, can_stop:false, start_pending:false, error:null});
      Object.assign(idle.runtime, {engine_ready:false, connection_ready:true,
        account_status_available:true, wechat_identity:'wxid_demo_account'});
      window.robotUpdatePhase = operation === 'stop_pending' ? 'loading' : 'idle';
      window.robotUpdateRequestFinished = false;
      api.get_status = async () => {
        const status = structuredClone(idle);
        if (window.robotUpdatePhase === 'loading') Object.assign(status, {state:'CORE_STARTING',
          title:'正在启动机器人', busy:true, can_start:false, can_stop:true, start_pending:true});
        return {ok:true, data:status};
      };
      if (operation === 'start_preparing') {
        const read = api.get_license_status;
        let held = false;
        api.get_license_status = async (...args) => {
          const result = await read(...args);
          if (held) return result;
          held = true;
          return new Promise(resolve => { window.finishRobotUpdateRequest = () => {
            window.robotUpdateRequestFinished = true; resolve(result);
          }; });
        };
        api.start_robot = async () => ({ok:true, data:{accepted:false}});
      } else {
        api.stop_robot = () => {
          window.robotUpdatePhase = 'idle';
          return new Promise(resolve => { window.finishRobotUpdateRequest = () => {
            window.robotUpdateRequestFinished = true; resolve({ok:true, data:{accepted:true}});
          }; });
        };
      }
    }""", operation)
    expect(page.locator("#primaryAction")).to_have_text("启动" if operation == "start_preparing" else "取消启动")
    expect(page.locator("#primaryAction")).to_be_enabled()
    page.locator("#primaryAction").click()
    if operation == "start_preparing":
        expect(page.locator("#stateTitle")).to_have_text("微信已登录，正在确认启动")
        assert page.evaluate("window.updateTestState.startPreparing")
    else:
        expect(page.locator("#stateTitle")).to_have_text("机器人未启动")
        assert page.evaluate("window.updateTestState.stopRequestPending")
    expect(page.locator("#primaryAction")).to_be_disabled()
    page.evaluate("window.finishUpdateDownload(); window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_contain_text("机器人正在启动、停止或处理其他操作")
    assert page.evaluate("window.updateCalls.install") == []
    assert not page.evaluate("window.robotUpdateRequestFinished")
    page.evaluate("window.finishRobotUpdateRequest()")
    expect(page.locator("#primaryAction")).to_have_text("启动")
    expect(page.locator("#primaryAction")).to_be_enabled()
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
    assert len(page.evaluate("window.updateCalls.download")) == 1


@pytest.mark.parametrize("stable", ["running", "retained"])
def test_install_waits_for_runtime_transition_then_allows_stable_state(update_page, stable):
    page = update_page
    _download_and_wait_for_consent(page)
    page.evaluate("""() => {
      window.originalRobotStatus = window.pywebview.api.get_status;
      window.pywebview.api.get_status = async () => {
        const result = structuredClone(await window.originalRobotStatus());
        Object.assign(result.data, {state:'STOPPING', title:'正在停止机器人', busy:true,
          can_start:false, can_stop:false, online:false, start_pending:false});
        return result;
      };
    }""")
    expect(page.locator("#stateTitle")).to_have_text("正在停止机器人")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_contain_text("请稍候")
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("""stable => {
      window.pywebview.api.get_status = async () => {
        const result = structuredClone(await window.originalRobotStatus());
        if (stable === 'retained') {
          Object.assign(result.data, {state:'LICENSE_READY', title:'机器人未启动', busy:false,
            can_start:true, can_stop:true, online:false, start_pending:false});
          Object.assign(result.data.runtime, {engine_ready:false, connection_ready:true});
        }
        return result;
      };
    }""", stable)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用" if stable == "running" else "机器人未启动")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]


def test_install_busy_rejection_keeps_consent_and_explains_robot_or_update_work(update_page):
    page = update_page
    _download_and_wait_for_consent(page)
    page.evaluate("""() => {
      window.originalInstallUpdate = window.pywebview.api.install_update;
      window.pywebview.api.install_update = async () => ({ok:true, data:{accepted:false, busy:true}});
    }""")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_contain_text("机器人或更新操作尚未完成")
    expect(page.locator("#modalError")).to_contain_text("更新包会保留")
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("() => { window.pywebview.api.install_update = window.originalInstallUpdate; }")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]


@pytest.mark.parametrize("operation", ["core_read", "provider_save"])
def test_install_waits_for_real_settings_controller_operation(update_page, operation):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.locator('.nav-item[data-page="settings"]').click()
    if operation == "core_read":
        page.locator('[data-settings-tab="core"]').click()
        expect(page.locator('#coreForm [name="robot_name"]')).to_have_value("小妹")
        page.evaluate("""() => {
          const read = window.pywebview.api.get_robot_core;
          window.pywebview.api.get_robot_core = async () => {
            const result = await read();
            return new Promise(resolve => { window.finishSettingsOperation = () => resolve(result); });
          };
        }""")
        page.locator('#refreshCore').click()
        expect(page.locator('#refreshCore')).to_be_disabled()
    else:
        page.locator('[data-provider-create]').click()
        page.locator('#providerModelInput').fill("pending-save-model")
        page.locator('#providerApiKey').fill("synthetic-key")
        page.evaluate("""() => {
          const save = window.pywebview.api.save_provider;
          window.pywebview.api.save_provider = (...args) => new Promise(resolve => {
            window.finishSettingsOperation = async () => resolve(await save(...args));
          });
        }""")
        page.locator('#saveProvider').click()
        expect(page.locator('#saveProvider')).to_have_text("正在保存并应用…")
    page.evaluate("window.finishUpdateDownload(); window.updateTestController.pollUpdateStatus()")
    expect(page.locator('#modalTitle')).to_have_text("安装 v1.0.1？")
    page.locator('#modalConfirm').click()
    expect(page.locator('#modalError')).to_contain_text("请稍候")
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("window.finishSettingsOperation()")
    if operation == "core_read":
        expect(page.locator('#refreshCore')).to_be_enabled()
    else:
        expect(page.locator('#providerEditorPage')).to_be_hidden()
    page.locator('#modalConfirm').click()
    expect(page.locator('#modalBackdrop')).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
    assert len(page.evaluate("window.updateCalls.download")) == 1


def test_same_version_new_package_needs_new_download_and_consent(update_page):
    page = update_page
    _download_and_wait_for_consent(page)
    page.locator("#modalCancel").click()
    page.evaluate("""() => {
      window.testRelease = {...window.testRelease, build: 'second', release_id: 'release-second'};
      // A formerly ready package must not be offered for the new release identity.
    }""")
    _check_automatically(page)
    expect(page.locator("#modalConfirm")).to_have_text("立即更新")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardMessage")).to_contain_text("正在下载更新包")
    page.evaluate("window.finishUpdateDownload()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.download") == [
        ["1.0.1", "release-first"], ["1.0.1", "release-second"],
    ]
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-second"]]


@pytest.mark.parametrize("failure", ["download", "unverified"])
def test_failed_or_unverified_download_cannot_reach_install(update_page, failure):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardMessage")).to_contain_text("正在下载更新包")
    page.evaluate("""failure => {
      window.testUpdateStatus = {...window.testUpdateStatus,
        state: failure === 'download' ? 'failed' : 'ready',
        error: failure === 'download' ? {code: 'UPD-1002', message: '下载中断，请重试'} : null};
    }""", failure)
    expect(page.locator("#updateCardTitle")).to_have_text("更新未完成")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardAction")).to_have_text("重试下载")
    assert page.evaluate("window.updateCalls.effects") == []
    page.locator("#updateCardAction").click()
    expect(page.locator("#updateCardMessage")).to_contain_text("正在下载更新包")
    page.evaluate("window.finishUpdateDownload()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    assert len(page.evaluate("window.updateCalls.download")) == 2
    assert page.evaluate("window.updateCalls.install") == []


def test_automatic_prompt_waits_for_other_dialog_and_does_not_repeat(update_page):
    page = update_page
    page.locator('.nav-item[data-page="history"]').click()
    expect(page.locator("#messageList .message-bubble").first).to_be_visible()
    _open_history_cleanup(page)
    page.evaluate("window.updateTestController.refreshUpdate(false)")
    expect(page.locator("#modalTitle")).to_have_text("按日期清理聊天记录")
    page.locator("#modalCancel").click()
    expect(page.locator("#modalTitle")).to_have_text("发现新版本 v1.0.1")
    page.locator("#modalCancel").click()
    page.evaluate("window.updateTestController.refreshUpdate(false)")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.download") == []


def test_view_update_while_downloading_keeps_page_available(update_page):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    _open_update_settings(page)
    page.evaluate("""() => {
      window.pywebview.api.check_update = async () => ({ok: true, data: {busy: true}});
      window.testUpdateStatus = {...window.testUpdateStatus, phase: 'extract',
        percent: 73, message: '正在解压更新包'};
    }""")
    page.locator("#checkUpdate").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    expect(page.locator("#updateCardMessage")).to_have_text("正在解压更新包")
    expect(page.locator("#updateCardPercent")).to_have_text("73%")
    assert page.locator("#updateCard").evaluate("node => node === document.activeElement")
    assert len(page.evaluate("window.updateCalls.download")) == 1
    assert page.evaluate("window.updateCalls.install") == []


def test_stale_status_response_cannot_hide_new_download(update_page):
    page = update_page
    _check_automatically(page)
    page.evaluate("""() => {
      const read = window.pywebview.api.get_update_status;
      let pending = true;
      window.pywebview.api.get_update_status = () => {
        if (!pending) return read();
        pending = false;
        return new Promise(resolve => { window.resolveStaleStatus = resolve; });
      };
      window.stalePoll = window.updateTestController.pollUpdateStatus();
    }""")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("""async () => {
      window.resolveStaleStatus({ok: true, data: {state: 'idle', phase: 'idle'}});
      await window.stalePoll;
    }""")
    expect(page.locator("#updateCard")).to_be_visible()
    page.evaluate("window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#updateCardPercent")).to_have_text("25%")
    assert len(page.evaluate("window.updateCalls.download")) == 1
    assert page.evaluate("window.updateCalls.install") == []


def test_ready_prompt_waits_for_other_confirmation_then_only_appears_once(update_page):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    _open_history_cleanup(page)
    expect(page.locator("#modalTitle")).to_have_text("按日期清理聊天记录")
    page.evaluate("window.finishUpdateDownload()")
    page.evaluate("window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#updateCardAction")).to_have_text("立即安装")
    expect(page.locator("#modalTitle")).to_have_text("按日期清理聊天记录")
    page.locator("#modalCancel").click()
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    page.locator("#modalCancel").click()
    for _ in range(3):
        page.evaluate("window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.locator("#updateCardAction").click()
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    assert page.evaluate("window.updateCalls.install") == []


@pytest.mark.parametrize("matching", [True, False])
def test_busy_download_only_tracks_the_requested_release(update_page, matching):
    page = update_page
    _check_automatically(page)
    page.evaluate("""matching => {
      window.originalDownload = window.pywebview.api.download_update;
      window.pywebview.api.download_update = async () => {
        window.testUpdateStatus = {state: 'running', phase: 'download', percent: 30,
          release_id: matching ? 'release-first' : 'unrelated-release', message: '正在下载更新包'};
        return {ok: true, data: {accepted: false, busy: true}};
      };
    }""", matching)
    page.locator("#modalConfirm").click()
    if matching:
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#updateCardPercent")).to_have_text("30%")
    else:
        expect(page.locator("#modalError")).to_contain_text("另一个更新任务正在处理中")
        expect(page.locator("#updateCard")).to_be_hidden()
        page.evaluate("window.pywebview.api.download_update = window.originalDownload")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#updateCardPercent")).to_have_text("25%")
    assert page.evaluate("window.updateCalls.install") == []


def test_changed_release_discards_queued_ready_confirmation(update_page):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    _open_history_cleanup(page)
    page.evaluate("window.finishUpdateDownload()")
    page.evaluate("window.updateTestController.pollUpdateStatus()")
    page.evaluate("""() => {
      window.testRelease = {...window.testRelease, version: '1.0.2', release_id: 'release-second'};
    }""")
    page.evaluate("window.updateTestController.refreshUpdate(false)")
    page.locator("#modalCancel").click()
    expect(page.locator("#modalTitle")).to_have_text("发现新版本 v1.0.2")
    expect(page.locator("#modalConfirm")).to_have_text("立即更新")
    expect(page.locator("#updateCard")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == []


@pytest.mark.parametrize("width,theme", [(1380, "dark"), (600, "light"), (600, "dark")])
def test_update_card_fits_viewport_and_theme(update_page, tmp_path, width, theme):
    page = update_page
    page.set_viewport_size({"width": width, "height": 820})
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    if page.locator("html").get_attribute("data-theme") != theme:
        page.locator("#themeToggle").click()
    card = page.locator("#updateCard")
    expect(card).to_be_visible()
    bounds = card.bounding_box()
    assert bounds["x"] >= 15
    assert bounds["x"] + bounds["width"] <= width - 15
    assert bounds["y"] + bounds["height"] <= 805
    assert not card.evaluate("node => node.scrollWidth > node.clientWidth")
    assert card.evaluate("node => getComputedStyle(node).position") == "fixed"
    assert not page.locator(".app-shell").evaluate("node => node.inert")
    page.screenshot(path=str(tmp_path / f"update-card-{width}-{theme}.png"), animations="disabled")


@pytest.mark.parametrize("width,height", [(1380, 860), (1080, 680)])
def test_long_update_notes_scroll_with_actions_visible_and_keyboard_escape(update_page, tmp_path, width, height):
    page = update_page
    page.set_viewport_size({"width": width, "height": height})
    page.evaluate("""() => {
      window.testRelease.notes = Array.from({length: 20}, (_, index) =>
        `${index + 1}. 修复账号切换后的缓存隔离，完善环境检查、消息回执与自动撤回、失败提示和重试体验。`);
    }""")
    _check_automatically(page)
    body = page.locator("#modalBody")
    expect(body).to_have_attribute("tabindex", "0")
    for selector in ["#modalTitle", "#modalCancel", "#modalConfirm"]:
        bounds = page.locator(selector).bounding_box()
        assert bounds and bounds["y"] >= 20 and bounds["y"] + bounds["height"] <= height - 20
    assert body.evaluate("node => node.scrollHeight > node.clientHeight")
    page.locator("#modalConfirm").focus()
    page.keyboard.press("Tab")
    expect(body).to_be_focused()
    page.keyboard.press("End")
    page.wait_for_timeout(150)
    assert body.evaluate("node => node.scrollTop > 0")
    page.keyboard.press("Shift+Tab")
    expect(page.locator("#modalConfirm")).to_be_focused()
    page.screenshot(path=str(tmp_path / f"update-long-notes-{width}.png"))
    page.keyboard.press("Escape")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert not page.locator(".app-shell").evaluate("node => node.inert")
    assert page.evaluate("window.updateCalls.download") == []
    _open_update_settings(page)
    page.locator("#checkUpdate").click()
    expect(page.locator("#modalBackdrop")).to_be_visible()
    assert body.evaluate("node => node.scrollTop") == 0
    page.locator("#modalCancel").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()


@pytest.mark.parametrize("width,height", [(1380, 860), (390, 700)])
def test_update_notes_preserve_multiline_emoji_and_literal_markup(update_page, tmp_path, width, height):
    page = update_page
    page.set_viewport_size({"width": width, "height": height})
    notes = ("✨ 体验优化\n\n🛠️ 修复连接提示\n1. 保留你填写的编号和空行\n"
             '<img src="missing" onerror="window.notesExecuted=true">\n'
             + "\n".join(f"第 {index + 1} 项：完善更新体验。" for index in range(50)))
    page.evaluate("""notes => {
      window.testRelease.title = '🎉 本次更新';
      window.testRelease.notes = [notes];
    }""", notes)
    _check_automatically(page)
    message = page.locator("#modalMessage")
    assert message.text_content().startswith(f"🎉 本次更新\n\n{notes}\n\n更新包约 ")
    assert message.evaluate("node => getComputedStyle(node).whiteSpace") == "pre-wrap"
    expect(message.locator("*")).to_have_count(0)
    assert not page.evaluate("Boolean(window.notesExecuted)")
    assert page.locator("#modalBody").evaluate("node => node.scrollHeight > node.clientHeight")
    assert not message.evaluate("node => node.scrollWidth > node.clientWidth")
    for selector in ["#modalTitle", "#modalCancel", "#modalConfirm"]:
        bounds = page.locator(selector).bounding_box()
        assert bounds and bounds["y"] >= 20 and bounds["y"] + bounds["height"] <= height - 20
    page.screenshot(path=str(tmp_path / f"update-multiline-notes-{width}.png"))
    page.locator("#modalCancel").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.download") == []
    assert page.evaluate("window.updateCalls.install") == []
