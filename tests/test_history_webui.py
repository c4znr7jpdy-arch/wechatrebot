"""Run message-center state regressions against the delivered JavaScript."""
from pathlib import Path
import shutil
import subprocess

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_message_image_preview_webui import image_source


def test_message_center_date_cache_and_paging_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    script = Path(__file__).with_name("history_webui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "history date, cache, pagination, refresh and account isolation: passed" in result.stdout


@pytest.mark.parametrize("resume", ["focus", "visibilitychange", "navigation"])
def test_long_absence_keeps_visible_history_until_latest_is_requested(
    browser, manager_url, tmp_path, resume,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => callback.name === 'pollHistory'
        ? 0 : schedule(callback, ...args);
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api;
          window.testHistoryLatest = 40;
          api.get_conversations = async () => ({ok:true, data:{account_id:'wxid_demo_account',
            items:[{conversation_id:'retained-chat', display_name:'缓存保留测试', type:'private',
              message_count:window.testHistoryLatest, last_timestamp:window.testHistoryLatest}], summary:{}}});
          api.get_messages = async () => {
            if (window.testHistoryLatest > 40 && !window.testHistoryResolved) {
              await new Promise(resolve => { window.resolveHistoryRefresh = () => {
                window.testHistoryResolved = true; resolve();
              }; });
            }
            const last = window.testHistoryLatest;
            return {ok:true, data:{account_id:'wxid_demo_account', has_more:true,
              items:Array.from({length:20}, (_, i) => ({id:last-19+i, timestamp:1793200000+last-19+i,
                content:`测试记录 ${last-19+i}`, role:'user', sender_id:'member', sender_name:'测试联系人'}))}};
          };
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        bubbles = page.locator("#messageList .message-bubble")
        expect(bubbles).to_have_count(20)
        page.locator("#messageList .message-avatar-button").last.click()
        page.evaluate("""() => {
          const list = document.querySelector('#messageList');
          list.scrollTop = list.scrollHeight;
          window.retainedMessageNode = list.firstElementChild;
          window.retainedMessageHtml = list.innerHTML;
          window.retainedMessageScroll = list.scrollTop;
        }""")
        if resume == "navigation":
            page.locator('.nav-item[data-page="home"]').click()
        page.evaluate("window.testHistoryLatest = 500")
        if resume == "navigation":
            page.locator('.nav-item[data-page="history"]').click()
        else:
            page.evaluate("event => (event === 'focus' ? window : document).dispatchEvent(new Event(event))", resume)
        page.wait_for_function("typeof window.resolveHistoryRefresh === 'function'")
        assert page.evaluate("document.querySelector('#messageList').innerHTML === window.retainedMessageHtml")
        page.evaluate("window.resolveHistoryRefresh()")
        latest = page.locator("#newMessageButton")
        expect(latest).to_be_visible()
        expect(latest).to_contain_text("回到最新消息")
        assert page.evaluate("document.querySelector('#messageList').firstElementChild === window.retainedMessageNode")
        assert page.evaluate("document.querySelector('#messageList').scrollTop === window.retainedMessageScroll")
        expect(bubbles.first).to_have_text("测试记录 21")
        expect(page.locator("#messageList .message-avatar-button.selected").last).to_be_visible()
        page.screenshot(path=str(tmp_path / f"history-retained-{resume}.png"), animations="disabled")
        latest.click()
        expect(bubbles.first).to_have_text("测试记录 481")
        expect(bubbles).to_have_count(20)
        expect(latest).to_be_hidden()
        assert page.url == manager_url
        assert "消息中心" in page.title()
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("width", [1380, 1080])
@pytest.mark.parametrize("denied", ["unlicensed", "logged_out", "both"])
def test_history_access_loss_clears_private_data_and_rejects_late_responses(
    browser, manager_url, tmp_path, width, denied,
):
    page = browser.new_page(viewport={"width": width, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => {
        if (callback.name === 'refreshStatus') { window.refreshAccessStatus = callback; return 0; }
        return callback.name === 'pollHistory' ? 0 : schedule(callback, ...args);
      };
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""thumbnail => {
          const api = window.pywebview.api, status = api.get_status;
          window.historyAccess = 'allowed';
          window.historyVersion = 1;
          window.historyMessageReads = 0;
          window.historyImageReads = 0;
          api.get_status = async () => {
            const result = structuredClone(await status()), allowed = historyAccess === 'allowed';
            result.data.history_account = allowed ? 'wxid_demo_account' : '';
            // Stale offline metadata must not override the explicit denied account.
            result.data.history_offline = !allowed;
            result.data.runtime.wechat_identity = ['allowed', 'unlicensed'].includes(historyAccess)
              ? 'wxid_demo_account' : '';
            result.data.runtime.account_status_available = true;
            result.data.runtime.connection_ready = !!result.data.runtime.wechat_identity;
            result.data.runtime.engine_ready = !!result.data.runtime.wechat_identity;
            return result;
          };
          api.get_conversations = async () => ({ok:true, data:{account_id:'wxid_demo_account',
            items:[{conversation_id:'protected-chat', display_name:'私密会话测试', type:'private',
              message_count:historyVersion, last_timestamp:historyVersion}], summary:{}}});
          api.get_messages = async () => {
            historyMessageReads++;
            const version = historyVersion;
            if (window.holdHistoryRead) await new Promise(resolve => { window.resolveHistoryRead = resolve; });
            return {ok:true, data:{account_id:'wxid_demo_account', has_more:false, items:[
              {id:version, timestamp:1793200000+version, content:`私密消息 ${version}`, role:'user',
                sender_id:'private-member', sender_name:'私密发送者', media_previews:[thumbnail]}]}};
          };
          api.get_message_image = () => {
            historyImageReads++;
            return new Promise(resolve => { window.resolveHistoryImage = resolve; });
          };
        }""", image_source((120, 240)))
        page.evaluate("window.refreshAccessStatus()")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble")).to_contain_text("私密消息 1")
        page.locator("#messageList .message-avatar-button").click()
        expect(page.locator("#conversationInspector")).to_contain_text("私密发送者")
        page.evaluate("window.holdHistoryRead = true; window.historyVersion = 2")
        page.locator("#refreshHistory").click()
        page.wait_for_function("typeof window.resolveHistoryRead === 'function'")
        page.locator(".message-image-preview").click()
        preview = page.locator(".report-preview-dialog")
        expect(preview).to_be_visible()
        page.evaluate("mode => { window.historyAccess = mode; return window.refreshAccessStatus(); }", denied)
        expect(page.locator("#conversationList")).not_to_contain_text("私密会话测试")
        expect(page.locator("#messageList .message-bubble")).to_have_count(0)
        expect(page.locator("#conversationInspector")).not_to_contain_text("私密发送者")
        expect(page.locator("#exportHistory")).to_be_disabled()
        expect(page.locator("#refreshHistory")).to_be_disabled()
        expect(preview).not_to_be_visible()
        expect(preview.locator("img")).not_to_have_attribute("src", image_source((120, 240)))
        page.evaluate("""() => {
          window.holdHistoryRead = false;
          window.resolveHistoryRead();
          window.resolveHistoryImage({ok:true, data:{account_id:'wxid_demo_account', src:'', quality:'original'}});
        }""")
        expect(page.locator("#messageList .message-bubble")).to_have_count(0)
        expect(page.locator("#exportHistory")).to_be_disabled()
        expect(page.locator("#refreshHistory")).to_be_disabled()
        evidence = tmp_path / f"history-denied-{denied}-{width}.png"
        page.screenshot(path=str(evidence), animations="disabled")
        page.evaluate("window.historyAccess = 'allowed'; window.historyVersion = 3; window.refreshAccessStatus()")
        expect(page.locator("#messageList .message-bubble")).to_contain_text("私密消息 3")
        expect(page.locator("#exportHistory")).to_be_enabled()
        expect(page.locator("#refreshHistory")).to_be_enabled()
        assert page.evaluate("window.historyMessageReads") == 3
        page.locator(".message-image-preview").click()
        expect(preview).to_be_visible()
        assert page.evaluate("window.historyImageReads") == 2
        page.keyboard.press("Escape")
        page.locator('.nav-item[data-page="home"]').click()
        page.evaluate("mode => { window.historyAccess = mode; return window.refreshAccessStatus(); }", denied)
        expect(page.locator("#page-home")).to_be_visible()
        expect(page.locator("#messageList .message-bubble")).to_have_count(0)
        expect(page.locator("#conversationInspector")).not_to_contain_text("私密发送者")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble")).to_have_count(0)
        expect(page.locator("#historySummary")).to_have_text("请先完成授权并登录微信")
        assert page.evaluate("window.historyMessageReads") == 3
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert not errors, errors
        print(f"History access evidence: {evidence}")
    finally:
        page.close()
