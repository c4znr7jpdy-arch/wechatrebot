"""Unknown connection probes retain the current history view without granting access.

Browser plugin not available: reuse the project's synthetic Playwright preview.
No robot, customer history, or native connection is started by these tests.
"""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


GROUP = "56594698995@chatroom"


@pytest.fixture
def history_page(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 1440, "height": 920})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const interval = window.setInterval;
      window.historyTestPollers = new Map();
      window.setInterval = (callback, ...args) => {
        if (['refreshStatus', 'pollHistory', 'pollGroups'].includes(callback.name)) {
          historyTestPollers.set(callback.name, callback);
          return 0;
        }
        return interval(callback, ...args);
      };
      Object.defineProperty(window, 'createMessageHistory', {configurable: true, set(create) {
        Object.defineProperty(window, 'createMessageHistory', {value: helpers => {
          window.historyTestState = helpers.state;
          return window.historyTestController = create(helpers);
        }});
      }});
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.wait_for_function("historyTestPollers.has('refreshStatus')")
        page.evaluate("""group => {
          const api = window.pywebview.api, readStatus = api.get_status;
          window.historyTest = {account: 'wxid_demo_account', phase: 'online', group,
            calls: [], hold: '', finishRead: null};
          api.get_status = async () => {
            const response = structuredClone(await readStatus()), status = response.data;
            const unknown = historyTest.phase === 'unknown';
            const account = historyTest.phase === 'logout' ? '' : historyTest.account;
            status.state = unknown ? 'VERIFYING_CONNECTION' : account ? 'RUNNING' : 'WAITING_WECHAT';
            status.title = unknown ? '正在确认连接' : account ? '机器人可用' : '等待登录微信';
            status.online = !unknown && Boolean(account);
            status.history_account = unknown || historyTest.phase === 'revoked' ? '' : account;
            status.runtime = {...status.runtime, account_status_available: !unknown,
              wechat_identity: unknown ? '' : account, hook_ready: !unknown,
              upstream_connected: !unknown, heartbeat_fresh: !unknown};
            return response;
          };
          api.get_conversations = async filters => {
            const account = filters.account_id;
            historyTest.calls.push({name: 'get_conversations', args: structuredClone([filters])});
            document.documentElement.dataset.historyTestSearch = filters.search || '';
            document.documentElement.dataset.historyTestDate = filters.start && filters.end ? 'filtered' : '';
            const items = account === 'wxid_demo_account'
              ? Array.from({length: 24}, (_, index) => ({
                  conversation_id: index === 8 ? group : `fixture_${index}@chatroom`,
                  type: 'group', display_name: index === 8 ? '正在阅读的群聊' : `保留列表 ${index}`,
                  message_count: 60, last_timestamp: 1795000000,
                }))
              : [{conversation_id: 'new_account_group@chatroom', type: 'group',
                  display_name: '新账号群聊', message_count: 60, last_timestamp: 1795000000}];
            const result = {ok: true, data: {account_id: account, items, has_more: false,
              summary: {messages: items.length * 60, group_messages: items.length * 60}}};
            if (historyTest.hold === 'get_conversations') {
              return new Promise(resolve => { historyTest.finishRead = () => {
                result.data.items = [{...items[0], conversation_id: 'late_response@chatroom', display_name: '迟到目录不可覆盖'}];
                resolve(result);
              }; });
            }
            return result;
          };
          api.get_messages = async (id, filters) => {
            historyTest.calls.push({name: 'get_messages', args: structuredClone([id, filters])});
            const result = {ok: true, data: {account_id: filters.account_id, has_more: false,
              items: Array.from({length: 60}, (_, index) => ({id: index + 1, timestamp: 1795000000 + index,
                content: `${id} 保留消息 ${index + 1}`, role: 'user',
                sender_id: index % 2 ? 'member_b' : 'member_a', sender_name: index % 2 ? '另一位成员' : '选中的成员'}))}};
            if (historyTest.hold === 'get_messages') {
              return new Promise(resolve => { historyTest.finishRead = () => {
                result.data.items = [{id: 999, timestamp: 1795000099, content: '迟到消息不可覆盖', role: 'user', sender_id: 'late'}];
                resolve(result);
              }; });
            }
            return result;
          };
          api.get_group_qrcode = async (account, groupId) => {
            historyTest.calls.push({name: 'get_group_qrcode', args: [account, groupId]});
            return {ok: true, data: {account_id: account, group_id: groupId,
              image_data_url: 'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aPa0AAAAASUVORK5CYII='}};
          };
          for (const name of ['export_history', 'delete_conversation', 'delete_history_before', 'get_message_image']) {
            const original = api[name];
            api[name] = async (...args) => {
              historyTest.calls.push({name, args: structuredClone(args)});
              return original(...args);
            };
          }
          window.historyTestStatus = async (phase, account) => {
            historyTest.phase = phase;
            if (account) historyTest.account = account;
            await historyTestPollers.get('refreshStatus')();
          };
          window.historyTestSettled = async () => {
            await new Promise(requestAnimationFrame);
            await new Promise(requestAnimationFrame);
          };
        }""", GROUP)
        page.evaluate("historyTestStatus('online')")
        yield page
        assert page.url == manager_url
        assert page.title().endswith("WxRebot")
        assert page.locator(".page.active").inner_text().strip()
        assert not page.locator("vite-error-overlay, nextjs-portal, #webpack-dev-server-client-overlay").count()
        assert not errors, errors
    finally:
        if not page.is_closed():
            page.screenshot(path=str(tmp_path / "history-connection-state.png"), animations="disabled")
            print(f"History connection evidence: {tmp_path / 'history-connection-state.png'}")
            page.close()


def open_retained_history(page, *, filters=False):
    page.locator('.nav-item[data-page="history"]').click()
    expect(page.locator("#messageList article")).to_have_count(60)
    if filters:
        page.locator("#historySearch").fill("保留")
        expect(page.locator("html")).to_have_attribute("data-history-test-search", "保留")
        page.locator("#historyDateRange").click()
        page.locator("#rangeWeek").click()
        page.locator("#rangeApply").click()
        expect(page.locator("html")).to_have_attribute("data-history-test-date", "filtered")
    page.locator(f'[data-conversation="{GROUP}"]').click()
    expect(page.locator("#conversationHeader")).to_contain_text(GROUP)
    expect(page.locator("#messageList article")).to_have_count(60)
    page.wait_for_function("!historyTestState.historyMessagesLoading && !historyTestState.historyConversationsLoading")
    page.locator("#messageList .message-avatar-button").first.click()
    expect(page.locator("#conversationInspector .current-sender")).to_contain_text("选中的成员")
    page.evaluate("""() => {
      const messages = document.querySelector('#messageList'), directory = document.querySelector('#conversationList');
      messages.scrollTop = 350;
      directory.scrollTop = 240;
      historyTest.retained = {
        messages: [...messages.querySelectorAll('article')],
        conversations: [...directory.querySelectorAll('[data-conversation]')],
        header: document.querySelector('#conversationHeader').firstElementChild,
        inspector: document.querySelector('#conversationInspector').firstElementChild,
        sender: historyTestState.selectedSenderKey, conversation: historyTestState.selectedConversation.conversation_id,
        messageScroll: messages.scrollTop, directoryScroll: directory.scrollTop,
        search: document.querySelector('#historySearch').value,
        start: document.querySelector('#historyStart').value, end: document.querySelector('#historyEnd').value,
        dateLabel: document.querySelector('#historyDateRange').textContent,
      };
    }""")
    assert page.evaluate("historyTest.retained.messageScroll > 0 && historyTest.retained.directoryScroll > 0")


def assert_retained_history(page):
    assert page.evaluate("""() => {
      const saved = historyTest.retained;
      return [...saved.messages, ...saved.conversations, saved.header, saved.inspector].every(node => node.isConnected)
        && historyTestState.selectedConversation?.conversation_id === saved.conversation
        && historyTestState.selectedSenderKey === saved.sender
        && document.querySelector('#messageList').scrollTop === saved.messageScroll
        && document.querySelector('#conversationList').scrollTop === saved.directoryScroll
        && document.querySelector('#historySearch').value === saved.search
        && document.querySelector('#historyStart').value === saved.start
        && document.querySelector('#historyEnd').value === saved.end
        && document.querySelector('#historyDateRange').textContent === saved.dateLabel;
    }"""), "Connection uncertainty must preserve the current group's nodes, sender, filters and reading position"
    expect(page.locator(f'[data-conversation="{GROUP}"]')).to_have_class("conversation-item active")
    expect(page.locator("#messageList")).not_to_contain_text("授权并登录后查看")


def test_unknown_probe_and_same_account_recovery_keep_current_group_view(history_page, tmp_path):
    page = history_page
    open_retained_history(page, filters=True)
    before = page.evaluate("historyTest.calls.filter(call => call.name === 'get_conversations').length")
    page.evaluate("historyTestStatus('unknown')")
    expect(page.locator("#sideHealthText")).to_have_text("正在确认连接")
    assert_retained_history(page)
    page.screenshot(path=str(tmp_path / "same-group-connection-unknown.png"), animations="disabled")
    page.evaluate("historyTestStatus('online')")
    page.evaluate("historyTestSettled()")
    assert_retained_history(page)
    assert page.evaluate("historyTest.calls.filter(call => call.name === 'get_conversations').length") == before
    expect(page.locator("#refreshHistory")).to_be_enabled()
    expect(page.locator("#exportHistory")).to_be_enabled()
    page.screenshot(path=str(tmp_path / "same-group-connection-restored.png"), animations="disabled")


def test_first_history_open_waits_for_account_confirmation_then_loads(history_page):
    page = history_page
    page.evaluate("historyTestStatus('unknown')")
    page.locator('.nav-item[data-page="history"]').click()
    assert page.evaluate("historyTest.calls.length") == 0
    page.evaluate("historyTestStatus('online')")
    expect(page.locator("#messageList article")).to_have_count(60)
    expect(page.locator(f'[data-conversation="{GROUP}"]')).to_be_visible()
    expect(page.locator("#refreshHistory")).to_be_enabled()


@pytest.mark.parametrize("phase", ["switch", "logout", "revoked"])
def test_verified_account_change_or_access_revocation_still_clears_old_view(history_page, phase):
    page = history_page
    open_retained_history(page)
    if phase == "switch":
        page.evaluate("historyTestStatus('online', 'wxid_new_account')")
        expect(page.locator("#conversationList")).to_contain_text("新账号群聊")
        expect(page.locator("#messageList")).to_contain_text("new_account_group@chatroom")
    else:
        page.evaluate("phase => historyTestStatus(phase)", phase)
        expect(page.locator("#messageList article")).to_have_count(0)
        expect(page.locator("#refreshHistory")).to_be_disabled()
        expect(page.locator("#exportHistory")).to_be_disabled()
    expect(page.locator(f'[data-conversation="{GROUP}"]')).to_have_count(0)
    assert page.evaluate("historyTest.retained.messages.every(node => !node.isConnected)")
    assert page.evaluate("historyTest.retained.conversations.every(node => !node.isConnected)")
    assert page.evaluate("!historyTestState.selectedSenderKey")


def test_unknown_account_blocks_ui_actions_and_public_history_read_entries(history_page):
    page = history_page
    open_retained_history(page)
    page.evaluate("historyTestStatus('unknown')")
    for selector in ("#refreshHistory", "#exportHistory", "#deleteConversation", "#cleanHistory"):
        expect(page.locator(selector)).to_be_disabled()
    before = page.evaluate("historyTest.calls.length")
    page.evaluate("""async () => {
      const controller = historyTestController;
      for (const id of ['refreshHistory', 'exportHistory', 'deleteConversation', 'cleanHistory']) {
        document.getElementById(id).dispatchEvent(new Event('click', {bubbles: true}));
      }
      await controller.refreshHistoryRecords();
      await controller.loadConversations({silent: true, refreshGroups: true});
      await controller.selectConversation(historyTest.group, {silent: true, forceRefresh: true});
      await controller.loadOlderMessages();
      await historyTestPollers.get('pollHistory')();
      window.dispatchEvent(new Event('focus'));
      document.dispatchEvent(new Event('visibilitychange'));
      await historyTestSettled();
    }""")
    assert page.evaluate("historyTest.calls.length") == before
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert_retained_history(page)


@pytest.mark.parametrize("method", ["get_conversations", "get_messages"])
def test_late_read_during_unknown_connection_cannot_replace_retained_context(history_page, method):
    page = history_page
    open_retained_history(page)
    page.evaluate("""method => {
      historyTest.hold = method;
      historyTest.pendingRead = method === 'get_conversations'
        ? historyTestController.loadConversations({silent: true})
        : historyTestController.selectConversation(historyTest.group, {silent: true, forceRefresh: true});
    }""", method)
    page.wait_for_function("typeof historyTest.finishRead === 'function'")
    page.evaluate("historyTestStatus('unknown')")
    page.evaluate("""async () => {
      historyTest.hold = '';
      historyTest.finishRead();
      await historyTest.pendingRead;
    }""")
    assert_retained_history(page)
    expect(page.locator("#page-history")).not_to_contain_text("迟到")
    page.evaluate("historyTestStatus('online')")
    page.evaluate("historyTestSettled()")
    assert_retained_history(page)


def test_pending_delete_confirmation_is_denied_without_clearing_retained_history(history_page):
    page = history_page
    open_retained_history(page)
    page.locator("#deleteConversation").click()
    expect(page.locator("#modalBackdrop")).to_be_visible()
    page.evaluate("historyTestStatus('unknown')")
    before = page.evaluate("historyTest.calls.length")
    page.locator("#modalConfirm").click()
    page.wait_for_function("document.querySelector('#modalBackdrop').dataset.busy !== 'true'")
    assert page.evaluate("historyTest.calls.length") == before
    assert page.evaluate("historyTest.calls.every(call => call.name !== 'delete_conversation')")
    assert_retained_history(page)
