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


@pytest.mark.parametrize("resume", ["navigation", "poll"])
def test_unchanged_history_refresh_retains_panels_and_reading_position(
    browser, manager_url, tmp_path, resume,
):
    page = browser.new_page(viewport={"width": 1440, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => {
        if (callback.name === 'pollHistory') { window.refreshRetainedHistory = callback; return 0; }
        return callback.name === 'refreshStatus' ? 0 : schedule(callback, ...args);
      };
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""src => {
          const api = window.pywebview.api;
          window.retainedDirectoryReads = 0;
          window.retainedDirectoryRefreshes = [];
          window.retainedQrReads = 0;
          api.get_conversations = async filters => {
            window.retainedDirectoryReads++;
            window.retainedDirectoryRefreshes.push(filters.refresh_groups);
            return {ok:true, data:{account_id:'wxid_demo_account', summary:{directory_groups:24},
              items:Array.from({length:24}, (_, index) => ({conversation_id:`retained-${index}@chatroom`,
                display_name:`阅读状态测试群 ${index}`, type:'group', message_count:40, last_timestamp:40}))}};
          };
          api.get_messages = async () => ({ok:true, data:{account_id:'wxid_demo_account', has_more:false,
            items:Array.from({length:40}, (_, index) => ({id:index+1, timestamp:1793200000+index,
              content:`保留阅读位置 ${index+1}`, role:'user', sender_id:`member-${index % 2}`,
              sender_name:`测试成员 ${index % 2}`}))}});
          api.get_group_qrcode = async (account, group) => {
            window.retainedQrReads++;
            return {ok:true, data:{account_id:account, group_id:group, image_data_url:src}};
          };
        }""", image_source((64, 64), "PNG"))
        page.locator('.nav-item[data-page="history"]').click()
        page.locator('[data-conversation="retained-1@chatroom"]').click()
        expect(page.locator("#messageList .message-bubble")).to_have_count(40)
        page.locator("#messageList .message-avatar-button").first.click()
        expect(page.locator("#conversationInspector .group-qrcode img")).to_be_visible()
        before = page.evaluate("""() => {
          const ids = ['conversationList', 'conversationHeader', 'messageList', 'conversationInspector'];
          const elements = ids.map(id => document.getElementById(id));
          elements[0].scrollTop = 180;
          elements[2].scrollTop = 300;
          window.retainedHistoryNodes = elements.map(element => element.firstElementChild);
          window.retainedHistoryScrolls = elements.map(element => element.scrollTop);
          window.retainedQrNode = document.querySelector('.group-qrcode img');
          window.retainedHistoryIds = ids;
          return {reads:window.retainedDirectoryReads, qrReads:window.retainedQrReads,
            scrolls:window.retainedHistoryScrolls};
        }""")
        assert before["scrolls"][0] > 0 and before["scrolls"][2] > 0
        assert page.evaluate("window.retainedDirectoryRefreshes[0]") is True
        if resume == "navigation":
            page.locator('.nav-item[data-page="home"]').click()
            page.locator('.nav-item[data-page="history"]').click()
        else:
            page.evaluate("window.refreshRetainedHistory()")
        page.wait_for_function("reads => window.retainedDirectoryReads > reads", arg=before["reads"])
        expect(page.locator("#conversationInspector .group-qrcode img")).to_be_visible()
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        state = page.evaluate("""() => ({
          nodes:window.retainedHistoryIds.map((id, index) =>
            document.getElementById(id).firstElementChild === window.retainedHistoryNodes[index]),
          scrolls:window.retainedHistoryIds.map(id => document.getElementById(id).scrollTop),
          qrRetained:document.querySelector('.group-qrcode img') === window.retainedQrNode,
          qrReads:window.retainedQrReads,
        })""")
        page.screenshot(path=str(tmp_path / f"history-stable-panels-{resume}.png"), animations="disabled")
        print(f"History refresh retention ({resume}): {state}; evidence: {tmp_path}")
        expect(page.locator('[data-conversation="retained-1@chatroom"]')).to_have_class("conversation-item active")
        expect(page.locator("#messageList .message-avatar-button.selected").first).to_be_visible()
        assert state["scrolls"] == before["scrolls"]
        assert state["nodes"] == [True, True, True, True]
        assert state["qrRetained"]
        assert state["qrReads"] == before["qrReads"]
        assert page.evaluate("window.retainedDirectoryRefreshes.at(-1)") is False
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert not errors, errors
    finally:
        page.close()


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


def test_history_denial_during_pending_export_keeps_export_locked_until_dialog_returns(
    browser, manager_url, tmp_path,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => ['refreshStatus','pollHistory'].includes(callback.name)
        ? 0 : schedule(callback, ...args);
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api;
          window.overlappingExports = 0;
          api.export_history = () => {
            overlappingExports++;
            return new Promise(resolve => { window.finishOverlappingExport = resolve; });
          };
          api.delete_conversation = async () => ({ok:true,data:{deleted:0,requires_login:true}});
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble").first).to_be_visible()
        button = page.locator("#exportHistory")
        button.click()
        expect(button).to_have_attribute("aria-busy", "true")
        page.locator("#deleteConversation").click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#toastRegion .toast.error")).to_contain_text("本次删除未执行")
        expect(page.locator("#messageList .message-bubble")).to_have_count(0)
        expect(button).to_be_disabled()
        expect(button).to_have_text("导出中…")
        expect(button).to_have_attribute("aria-busy", "true")
        button.dispatch_event("click")
        assert page.evaluate("window.overlappingExports") == 1
        page.set_viewport_size({"width": 1080, "height": 860})
        page.screenshot(path=str(tmp_path / "history-denial-while-export-pending-1080.png"), animations="disabled")
        page.locator("#refreshHistory").click()
        expect(page.locator("#messageList .message-bubble").first).to_be_visible()
        expect(button).to_be_disabled()
        expect(button).to_have_attribute("aria-busy", "true")
        button.dispatch_event("click")
        assert page.evaluate("window.overlappingExports") == 1
        page.evaluate("window.finishOverlappingExport({ok:true,data:{cancelled:true}})")
        expect(button).to_be_enabled()
        expect(button).to_have_text("导出记录")
        expect(button).not_to_have_attribute("aria-busy", "true")
        expect(page.locator("#toastRegion")).not_to_contain_text("已导出到")
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


@pytest.mark.parametrize("outcome", [
    "cancelled", "success", "error", "requires_login", "account_changed",
    "logged_out", "account_switched", "status_unknown", "account_returned",
])
def test_history_export_pending_blocks_duplicates_and_restores_current_access(
    browser, manager_url, tmp_path, outcome,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => {
        if (callback.name === 'refreshStatus') { window.refreshExportStatus = callback; return 0; }
        return callback.name === 'pollHistory' ? 0 : schedule(callback, ...args);
      };
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api, status = api.get_status;
          window.exportAccess = 'allowed';
          window.exportCalls = [];
          window.exportAccount = () => exportAccess === 'account_switched' ? 'wxid_second' : 'wxid_demo_account';
          api.get_status = async () => {
            const result = structuredClone(await status());
            result.data.history_account = exportAccess === 'logged_out' ? '' : exportAccount();
            result.data.runtime.wechat_identity = result.data.history_account;
            result.data.runtime.account_status_available = exportAccess !== 'status_unknown';
            return result;
          };
          api.get_conversations = async () => ({ok:true, data:{account_id:exportAccount(),
            items:[{conversation_id:'export-chat', display_name:'导出测试会话', type:'private',
              message_count:1, last_timestamp:1}], summary:{}}});
          api.get_messages = async () => ({ok:true, data:{account_id:exportAccount(), has_more:false,
            items:[{id:1, timestamp:1793200001, content:'导出测试消息', role:'user', sender_id:'member'}]}});
          api.export_history = (...args) => {
            exportCalls.push(args);
            return new Promise(resolve => { window.resolveHistoryExport = resolve; });
          };
        }""")
        page.evaluate("window.refreshExportStatus()")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble")).to_have_text("导出测试消息")
        button = page.locator("#exportHistory")
        expect(button).to_be_enabled()
        page.evaluate("document.querySelector('#toastRegion').replaceChildren()")
        button.click()
        expect(button).to_be_disabled()
        expect(button).to_have_attribute("aria-busy", "true")
        expect(button).to_have_text("导出中…")
        button.dispatch_event("click")
        page.evaluate("window.refreshExportStatus()")
        expect(button).to_be_disabled()
        page.locator("#refreshHistory").click()
        expect(page.locator("#refreshHistory")).to_have_text("刷新")
        expect(button).to_be_disabled()
        button.dispatch_event("click")
        assert page.evaluate("window.exportCalls") == [["csv", "wxid_demo_account"]]
        expect(page.locator("#toastRegion .toast")).to_have_count(0)
        expect(page.locator("dialog[open]")).to_have_count(0)
        page.screenshot(path=str(tmp_path / f"history-export-pending-{outcome}.png"), animations="disabled")
        if outcome in {"logged_out", "account_switched", "status_unknown"}:
            page.evaluate("mode => { window.exportAccess = mode; return window.refreshExportStatus(); }", outcome)
            expect(button).to_be_disabled()
        elif outcome == "account_returned":
            for mode in ["logged_out", "allowed"]:
                page.evaluate("mode => { window.exportAccess = mode; return window.refreshExportStatus(); }", mode)
                expect(button).to_be_disabled()
            expect(page.locator("#messageList .message-bubble")).to_have_text("导出测试消息")
            button.dispatch_event("click")
            assert page.evaluate("window.exportCalls.length") == 1
        result = {"ok": True, "data": {"cancelled": True}}
        if outcome in {"success", "logged_out", "account_switched", "status_unknown"}:
            result["data"] = {"path": "测试导出.csv"}
        elif outcome == "error":
            result = {"ok": False, "error": {"message": "导出暂时失败", "recorded": True}}
        elif outcome in {"requires_login", "account_changed"}:
            result["data"][outcome] = True
        page.evaluate("result => window.resolveHistoryExport(result)", result)
        expect(button).to_have_text("导出记录")
        expect(button).not_to_have_attribute("aria-busy", "true")
        expect(button.locator('svg use[href="#i-download"]')).to_have_count(1)
        if outcome in {"cancelled", "success", "error", "account_returned"}:
            expect(button).to_be_enabled()
            if outcome == "success":
                expect(page.locator("#toastRegion .toast")).to_have_text("已导出到 测试导出.csv")
            elif outcome == "error":
                expect(page.locator("#toastRegion .toast.error")).to_contain_text("导出暂时失败")
            else:
                expect(page.locator("#toastRegion .toast")).to_have_count(0)
            button.click()
            expect(button).to_be_disabled()
            assert page.evaluate("window.exportCalls.length") == 2
            page.evaluate("window.resolveHistoryExport({ok:true,data:{cancelled:true}})")
            expect(button).to_be_enabled()
        else:
            expect(button).to_be_disabled()
            button.dispatch_event("click")
            assert page.evaluate("window.exportCalls.length") == 1
            if outcome == "status_unknown":
                expect(page.locator("#toastRegion .toast")).to_have_count(0)
            else:
                expect(page.locator("#toastRegion .toast.error")).to_contain_text("微信")
                expect(page.locator("#toastRegion .toast")).not_to_contain_text("已导出到")
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("operation", ["export", "delete", "cleanup"])
@pytest.mark.parametrize("outcome", ["requires_login", "account_changed", "local_account_change", "before_confirm"])
def test_history_denied_action_explains_result_and_offers_recovery(
    browser, manager_url, tmp_path, operation, outcome,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => {
        if (callback.name === 'refreshStatus') { window.refreshDeniedStatus = callback; return 0; }
        if (callback.name === 'pollHistory') { window.pollDeniedHistory = callback; return 0; }
        return schedule(callback, ...args);
      };
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api, status = api.get_status;
          window.deniedAccount = 'wxid_demo_account'; window.deniedRead = '';
          window.deniedCalls = [];
          api.get_status = async () => {
            const result = structuredClone(await status());
            result.data.history_account = deniedAccount;
            result.data.runtime.wechat_identity = deniedAccount;
            result.data.runtime.account_status_available = true;
            return result;
          };
          api.get_conversations = async () => ({ok:true, data:deniedRead
            ? {account_id:'', items:[], summary:{}, [deniedRead]:true}
            : {account_id:deniedAccount, items:[{conversation_id:'denied-chat', display_name:'原账号会话',
              type:'private', message_count:1, last_timestamp:1}], summary:{}}});
          api.get_messages = async () => ({ok:true, data:{account_id:deniedAccount, has_more:false,
            items:[{id:1, timestamp:1793200001, content:'原账号私密消息', role:'user', sender_id:'private-member'}]}});
          for (const method of ['export_history','delete_conversation','delete_history_before']) {
            api[method] = (...args) => {
              deniedCalls.push({method,args});
              return new Promise(resolve => { window.finishDeniedAction = resolve; });
            };
          }
        }""")
        page.evaluate("window.refreshDeniedStatus()")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble")).to_have_text("原账号私密消息")
        page.evaluate("document.querySelector('#toastRegion').replaceChildren()")
        if operation == "export":
            page.locator("#exportHistory").click()
        elif operation == "delete":
            page.locator("#deleteConversation").click()
            if outcome == "before_confirm":
                page.evaluate("window.deniedAccount = ''; window.refreshDeniedStatus()")
            page.locator("#modalConfirm").click()
        else:
            page.locator('.nav-item[data-page="settings"]').click()
            page.locator('[data-settings-tab="system"]').click()
            page.locator("#cleanHistory").click()
            page.locator("#modalInput").fill("2026-10-01")
            if outcome == "before_confirm":
                page.evaluate("window.deniedAccount = ''; window.refreshDeniedStatus()")
            page.locator("#modalConfirm").click()
        before_confirm = outcome == "before_confirm" and operation != "export"
        if not before_confirm:
            page.wait_for_function("typeof window.finishDeniedAction === 'function'")
        if outcome == "local_account_change":
            page.evaluate("window.deniedAccount = ''; window.refreshDeniedStatus()")
        action = {"export": "导出", "delete": "删除", "cleanup": "清理"}[operation]
        result = {"path": "preview.csv"} if operation == "export" else {"deleted": 1}
        if outcome != "local_account_change":
            result = {"cancelled": True, "deleted": 0,
                      "requires_login" if outcome == "before_confirm" else outcome: True}
        if not before_confirm:
            page.evaluate("data => window.finishDeniedAction({ok:true,data})", result)
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator("#toastRegion .toast.error")).to_be_visible()
        expect(page.locator("#toastRegion .toast")).not_to_contain_text("已导出到")
        expect(page.locator("#toastRegion .toast")).not_to_contain_text("已清理")
        expect(page.locator("#toastRegion .toast")).not_to_contain_text("已删除")
        if outcome == "local_account_change":
            expect(page.locator("#toastRegion .toast")).to_contain_text("核对")
            expect(page.locator("#toastRegion .toast")).not_to_contain_text("未执行")
        else:
            expect(page.locator("#toastRegion .toast")).to_contain_text(f"本次{action}未执行")
        if operation == "cleanup":
            expect(page.locator("#page-settings")).to_be_visible()
            page.screenshot(path=str(tmp_path / f"cleanup-denied-visible-{outcome}.png"), animations="disabled")
        expect(page.locator("#messageList .message-bubble")).to_have_count(0)
        expect(page.locator("#conversationList")).not_to_contain_text("原账号会话")
        expect(page.locator("#exportHistory")).to_be_disabled()
        expect(page.locator("#cleanHistory")).to_be_disabled()
        page.evaluate("window.refreshDeniedStatus()")
        expect(page.locator("#exportHistory")).to_be_disabled()
        page.evaluate("mode => { window.deniedRead = mode; return window.pollDeniedHistory(); }",
                      "requires_login" if outcome in {"local_account_change", "before_confirm"} else outcome)
        if operation == "cleanup":
            page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#historySummary")).not_to_contain_text("正在读取")
        expect(page.locator("#toastRegion")).to_have_attribute("aria-live", "polite")
        expect(page.locator('#messageList [data-history-recovery="home"]')).to_be_visible()
        expect(page.locator('#messageList [data-history-recovery="subscription"]')).to_be_visible()
        page.screenshot(path=str(tmp_path / f"history-action-denied-{operation}-{outcome}.png"), animations="disabled")
        page.locator('#messageList [data-history-recovery="home"]').click()
        expect(page.locator("#page-home")).to_be_visible()
        page.locator('.nav-item[data-page="history"]').click()
        page.locator('#messageList [data-history-recovery="subscription"]').click()
        expect(page.locator("#page-subscription")).to_be_visible()
        page.evaluate("window.deniedAccount = 'wxid_demo_account'; window.deniedRead = ''; window.refreshDeniedStatus()")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble")).to_have_text("原账号私密消息")
        expect(page.locator("#exportHistory")).to_be_enabled()
        expect(page.locator("#historySummary")).not_to_contain_text("未执行")
        expect(page.locator("#historySummary")).not_to_contain_text("核对")
        assert page.evaluate("window.deniedCalls.length") == (0 if before_confirm else 1)
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert not errors, errors
    finally:
        page.close()
