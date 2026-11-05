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
    expect(page.locator("#modalMessage")).to_contain_text("更新会强制关闭机器人程序和所有微信窗口，是否确认？")
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
