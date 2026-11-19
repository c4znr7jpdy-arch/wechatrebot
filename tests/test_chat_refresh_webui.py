"""Chat configuration retains verified views without periodic HOOK directory reads.

Browser plugin not available: use the project's Playwright preview fixtures.
Only synthetic preview APIs are exercised; no robot or native connection starts.
"""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.fixture
def chat_page(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 1380, "height": 920})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const interval = window.setInterval, timeout = window.setTimeout;
      window.chatTestIntervals = new Map();
      window.setInterval = (callback, ...args) => {
        window.chatTestIntervals.set(callback.name, callback);
        return ['refreshStatus', 'pollGroups', 'pollHistory'].includes(callback.name)
          ? 0 : interval(callback, ...args);
      };
      window.setTimeout = (callback, ...args) => callback.name === 'pollSelectedGroupPolicy'
        ? 0 : timeout(callback, ...args);
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.wait_for_function("window.chatTestIntervals.has('refreshStatus')")
        page.evaluate("""() => {
          const api = window.pywebview.api, status = api.get_status;
          window.chatTest = {
            account: 'wxid_demo_account', phase: 'online', calls: [],
            missingCount: false, deferContacts: false, pendingContacts: [],
          };
          api.get_status = async () => {
            const result = structuredClone(await status()), value = result.data;
            const unknown = chatTest.phase === 'unknown';
            const account = chatTest.phase === 'logout' ? '' : chatTest.account;
            value.online = !unknown && Boolean(account);
            value.state = unknown ? 'VERIFYING_CONNECTION' : account ? 'RUNNING' : 'WAITING_WECHAT';
            value.title = unknown ? '正在确认连接' : account ? '机器人可用' : '等待登录微信';
            value.history_account = unknown || chatTest.phase === 'revoked' ? '' : account;
            value.runtime = {...value.runtime, account_status_available: !unknown,
              wechat_identity: unknown ? '' : account, hook_ready: !unknown,
              upstream_connected: !unknown, heartbeat_fresh: !unknown};
            return result;
          };
          for (const name of ['get_groups', 'get_contacts', 'get_group_members',
              'get_group_policy', 'get_private_policy', 'get_contact_detail',
              'preview_group_policy', 'save_group_policy', 'save_private_policy', 'request_robot_core']) {
            const original = api[name];
            api[name] = async (...args) => {
              const account = chatTest.account;
              chatTest.calls.push({name, args: structuredClone(args), account});
              if (name === 'get_groups') return {ok: true, data: {
                account_id: account, updated_at: '2026-11-19T00:00:00Z', stale: false,
                items: account === 'wxid_demo_account' ? [
                  {group_id: '51632940287@chatroom', name: '保留规则的群聊', member_count: 8},
                  {group_id: '81263495012@chatroom', name: '后台读取人数的群聊', member_count: chatTest.missingCount ? null : 12},
                ] : [{group_id: 'new_account@chatroom', name: '新账号群聊', member_count: 2}],
              }};
              if (name === 'get_contacts') {
                if (chatTest.deferContacts) await new Promise(resolve => chatTest.pendingContacts.push(resolve));
                return {ok: true, data: {account_id: account, updated_at: '2026-11-19T00:00:00Z', stale: false,
                  items: account === 'wxid_demo_account' ? [
                    {user_id: 'wxid_zhangsan', name: '保留私聊草稿的联系人'},
                    {user_id: 'wxid_lisi', name: '另一位联系人'},
                  ] : [{user_id: 'new_account_contact', name: '新账号联系人'}],
                }};
              }
              if (name === 'get_group_members' && chatTest.missingCount) {
                await new Promise(resolve => { chatTest.finishMembers = resolve; });
                return {ok: true, data: {account_id: account, group_id: args[0], items: [], total: 23}};
              }
              const result = await original(...args);
              if (result?.data?.policy) result.data.policy.account_id = account;
              return result;
            };
          }
          window.chatTestStatus = async (phase, account) => {
            chatTest.phase = phase;
            if (account) chatTest.account = account;
            await chatTestIntervals.get('refreshStatus')();
          };
          window.chatTestResume = async () => {
            window.dispatchEvent(new Event('focus'));
            document.dispatchEvent(new Event('visibilitychange'));
            await new Promise(requestAnimationFrame);
            await new Promise(requestAnimationFrame);
          };
        }""")
        page.evaluate("chatTestStatus('online')")
        yield page
        assert page.url == manager_url
        assert "WxRebot" in page.title()
        assert page.locator(".page.active").inner_text().strip()
        assert not page.locator("vite-error-overlay, nextjs-portal, #webpack-dev-server-client-overlay").count()
        assert not errors, errors
    finally:
        if not page.is_closed():
            page.screenshot(path=str(tmp_path / "chat-refresh.png"), animations="disabled")
            print(f"Chat refresh browser evidence: {tmp_path / 'chat-refresh.png'}")
            page.close()


def open_chat(page, kind):
    page.locator('.nav-item[data-page="groups"]').click()
    if kind == "private":
        page.locator("#privateManagementTab").click()
    rows = "#contactRows .contact-row" if kind == "private" else "#groupRows .group-row"
    expect(page.locator(rows)).to_have_count(2)
    expect(page.locator("#refreshContacts" if kind == "private" else "#refreshGroups")).to_be_enabled()


def edit_rule(page, kind, value="17"):
    rows = "#contactRows .contact-row" if kind == "private" else "#groupRows .group-row"
    form = "#privatePolicyForm" if kind == "private" else "#groupPolicyForm"
    page.locator(rows).first.click()
    expect(page.locator(form)).to_be_visible()
    expect(page.locator(f'button[form="{form[1:]}"]')).to_be_enabled()
    page.locator(form).locator('details:has([name="daily_limit"]) > summary').click()
    page.locator(f'{form} [name="daily_limit"]').fill(value)
    return form


def directory_calls(page):
    return page.evaluate("chatTest.calls.filter(call => ['get_groups', 'get_contacts'].includes(call.name))")


def test_loaded_main_and_child_tabs_do_not_reload_directories(chat_page):
    page = chat_page
    open_chat(page, "groups")
    edit_rule(page, "groups")
    page.locator("#privateManagementTab").click()
    expect(page.locator("#contactRows .contact-row")).to_have_count(2)
    edit_rule(page, "private", "19")
    page.evaluate("""() => { chatTest.forms = [document.querySelector('#groupPolicyForm'),
      document.querySelector('#privatePolicyForm')]; }""")
    before = directory_calls(page)
    page.locator('.nav-item[data-page="home"]').click()
    page.locator('.nav-item[data-page="groups"]').click()
    page.locator("#groupManagementTab").click()
    page.locator("#privateManagementTab").click()
    assert directory_calls(page) == before, "Showing a loaded main/child tab must not query the directory"
    assert page.evaluate("chatTest.forms.every(form => form.isConnected)")
    expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("19")
    page.locator("#groupManagementTab").click()
    expect(page.locator('#groupPolicyForm [name="daily_limit"]')).to_have_value("17")


def test_chat_directory_does_not_register_a_periodic_poller(chat_page):
    assert not chat_page.evaluate("chatTestIntervals.has('pollGroups')"), "Configuration directories must not poll HOOK every 15 seconds"


@pytest.mark.parametrize("kind", ["groups", "private"])
def test_focus_visibility_preserve_loaded_directory_but_manual_refresh_reads(chat_page, kind):
    page = chat_page
    open_chat(page, kind)
    form = edit_rule(page, kind)
    page.evaluate("form => { chatTest.retainedForm = document.querySelector(form); }", form)
    before = directory_calls(page)
    page.evaluate("chatTestResume()")
    assert directory_calls(page) == before, "Tray/window restoration must retain an already loaded configuration"
    assert page.evaluate("chatTest.retainedForm.isConnected")
    button = "#refreshContacts" if kind == "private" else "#refreshGroups"
    page.locator(button).click()
    page.wait_for_function("count => chatTest.calls.filter(call => ['get_groups', 'get_contacts'].includes(call.name)).length > count", arg=len(before))
    expect(page.locator(button)).to_be_enabled()
    assert directory_calls(page)[-1]["args"] == [True]
    expect(page.locator(f'{form} [name="daily_limit"]')).to_have_value("17")


@pytest.mark.parametrize("kind", ["groups", "private"])
def test_unknown_account_preserves_both_drafts_and_blocks_actions(chat_page, kind):
    page = chat_page
    open_chat(page, "groups")
    edit_rule(page, "groups")
    page.locator("#privateManagementTab").click()
    expect(page.locator("#contactRows .contact-row")).to_have_count(2)
    edit_rule(page, "private", "19")
    if kind == "groups":
        page.locator("#groupManagementTab").click()
    page.evaluate("""() => {
      chatTest.retained = ['#groupPolicyForm', '#privatePolicyForm', '#groupRows .selected', '#contactRows .selected']
        .map(selector => document.querySelector(selector));
    }""")
    before = directory_calls(page)
    page.evaluate("chatTestStatus('unknown')")
    expect(page.locator("#sideHealthText")).to_have_text("正在确认连接")
    assert page.evaluate("chatTest.retained.every(node => node.isConnected)"), "An unknown probe is not an account switch"
    assert page.evaluate("['groupManagementPanel', 'privateManagementPanel'].every(id => document.getElementById(id).inert)")
    calls = page.evaluate("chatTest.calls.length")
    page.evaluate("""async () => {
      await chatTestResume();
      for (const id of ['groupPolicyForm', 'privatePolicyForm']) {
        document.getElementById(id).dispatchEvent(new Event('submit', {bubbles: true, cancelable: true}));
      }
      await new Promise(requestAnimationFrame);
    }""")
    assert page.evaluate("chatTest.calls.length") == calls, "Pending forms must not submit while identity is unverified"
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.evaluate("chatTestStatus('online')")
    assert directory_calls(page) == before, "Recovering the same account must reuse its verified directory"
    assert page.evaluate("chatTest.retained.every(node => node.isConnected)")
    assert page.evaluate("['groupManagementPanel', 'privateManagementPanel'].every(id => !document.getElementById(id).inert)")
    page.locator("#groupManagementTab").click()
    expect(page.locator('#groupPolicyForm [name="daily_limit"]')).to_have_value("17")
    expect(page.locator('#groupPolicyForm details:has([name="daily_limit"])')).to_have_attribute("open", "")
    page.locator("#privateManagementTab").click()
    expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("19")
    expect(page.locator('#privatePolicyForm details:has([name="daily_limit"])')).to_have_attribute("open", "")


@pytest.mark.parametrize("kind", ["groups", "private"])
def test_first_directory_read_waits_for_confirmed_account_then_recovers(chat_page, kind):
    page = chat_page
    page.evaluate("chatTestStatus('unknown')")
    page.locator('.nav-item[data-page="groups"]').click()
    if kind == "private":
        page.locator("#privateManagementTab").click()
    assert directory_calls(page) == []
    page.evaluate("chatTestResume()")
    assert directory_calls(page) == []
    page.evaluate("chatTestStatus('online')")
    rows = "#contactRows .contact-row" if kind == "private" else "#groupRows .group-row"
    expect(page.locator(rows)).to_have_count(2)
    assert len(directory_calls(page)) == 1


def test_verified_account_switch_rejects_old_directory_response(chat_page):
    page = chat_page
    open_chat(page, "private")
    edit_rule(page, "private")
    page.evaluate("""() => {
      chatTest.oldForm = document.querySelector('#privatePolicyForm');
      chatTest.deferContacts = true;
    }""")
    page.locator("#refreshContacts").click()
    page.wait_for_function("chatTest.pendingContacts.length === 1")
    page.evaluate("""async () => {
      chatTest.deferContacts = false;
      await chatTestStatus('online', 'wxid_new_account');
    }""")
    expect(page.locator("#contactRows .contact-row")).to_have_count(1)
    expect(page.locator("#contactRows")).to_contain_text("新账号联系人")
    assert not page.evaluate("chatTest.oldForm.isConnected")
    expect(page.locator("#groupRows .group-row")).to_have_count(0)
    page.evaluate("""async () => {
      chatTest.pendingContacts.shift()();
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
    }""")
    expect(page.locator("#contactRows .contact-row")).to_have_count(1)
    expect(page.locator("#contactRows")).to_contain_text("新账号联系人")
    expect(page.locator("#contactRows")).not_to_contain_text("保留私聊草稿")
    expect(page.locator("#privatePolicyForm")).to_have_count(0)


def test_background_member_count_does_not_replace_selected_rule_form(chat_page):
    page = chat_page
    page.evaluate("chatTest.missingCount = true")
    open_chat(page, "groups")
    page.wait_for_function("typeof chatTest.finishMembers === 'function'")
    form = edit_rule(page, "groups")
    page.evaluate("""() => {
      chatTest.retainedForm = document.querySelector('#groupPolicyForm');
      chatTest.retainedInput = document.activeElement;
      chatTest.finishMembers();
    }""")
    expect(page.locator('#groupRows [data-group-id="81263495012@chatroom"]').first).to_contain_text("23 人")
    assert page.evaluate("chatTest.retainedForm.isConnected"), "Another group's background count must not rebuild this rule form"
    assert page.evaluate("chatTest.retainedInput === document.activeElement")
    expect(page.locator(f'{form} [name="daily_limit"]')).to_have_value("17")
    expect(page.locator(f'{form} details:has([name="daily_limit"])')).to_have_attribute("open", "")


@pytest.mark.parametrize("kind,operation", [("groups", "policy"), ("groups", "binding"), ("private", "binding")])
def test_open_confirmation_rechecks_unknown_account_before_writing(chat_page, tmp_path, kind, operation):
    page = chat_page
    template = page.evaluate("""async () => {
      const result = await window.pywebview.api.request_robot_core('POST', 'profiles/create', {name: '确认期间保留的方案'});
      return result.data.id;
    }""") if operation == "binding" else None
    open_chat(page, kind)
    form = edit_rule(page, kind)
    if operation == "policy":
        page.locator('button[form="groupPolicyForm"]').click()
    else:
        select = page.locator("#privateChatProfile select" if kind == "private" else "#groupChatProfile select")
        expect(select).to_be_enabled()
        select.select_option(template)
    expect(page.locator("#modalBackdrop")).to_be_visible()
    page.evaluate("chatTestStatus('unknown')")
    before = page.evaluate("chatTest.calls.length")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_be_visible()
    expect(page.locator("#modalConfirm")).to_be_enabled()
    assert page.evaluate("chatTest.calls.length") == before, "A confirmation opened while online must recheck the account before any read/write"
    assert page.evaluate("chatTest.calls.filter(call => call.name === 'save_group_policy' || (call.name === 'request_robot_core' && call.args[1] === 'bindings/set')).length") == 0
    page.screenshot(path=str(tmp_path / f"confirmation-blocked-{kind}-{operation}.png"), animations="disabled")
    page.locator("#modalCancel").click()
    page.evaluate("chatTestStatus('online')")
    expect(page.locator(f'{form} [name="daily_limit"]')).to_have_value("17")


@pytest.mark.parametrize("kind", ["groups", "private"])
def test_binding_read_cannot_write_if_identity_becomes_unknown_during_confirmation(chat_page, tmp_path, kind):
    page = chat_page
    template = page.evaluate("""async () => {
      const result = await window.pywebview.api.request_robot_core('POST', 'profiles/create', {name: '异步确认方案'});
      return result.data.id;
    }""")
    open_chat(page, kind)
    edit_rule(page, kind)
    select = page.locator("#privateChatProfile select" if kind == "private" else "#groupChatProfile select")
    expect(select).to_be_enabled()
    select.select_option(template)
    expect(page.locator("#modalBackdrop")).to_be_visible()
    page.evaluate("""() => {
      const original = window.pywebview.api.request_robot_core;
      window.pywebview.api.request_robot_core = async (...args) => {
        if (args[0] === 'GET' && args[1] === 'profiles') {
          await new Promise(resolve => { chatTest.finishBindingRead = resolve; });
        }
        return original(...args);
      };
    }""")
    page.locator("#modalConfirm").click()
    page.wait_for_function("typeof chatTest.finishBindingRead === 'function'")
    page.evaluate("chatTestStatus('unknown')")
    page.evaluate("chatTest.finishBindingRead()")
    expect(page.locator("#modalError")).to_be_visible()
    expect(page.locator("#modalConfirm")).to_be_enabled()
    assert page.evaluate("chatTest.calls.filter(call => call.name === 'request_robot_core' && call.args[1] === 'bindings/set').length") == 0
    page.screenshot(path=str(tmp_path / f"binding-read-blocked-{kind}.png"), animations="disabled")
    page.locator("#modalCancel").click()


def test_leaving_cancels_member_count_without_stale_loading_or_lost_draft(chat_page, tmp_path):
    page = chat_page
    page.evaluate("chatTest.missingCount = true")
    open_chat(page, "groups")
    page.wait_for_function("typeof chatTest.finishMembers === 'function'")
    form = edit_rule(page, "groups")
    row = page.locator('#groupRows .group-row[data-group-id="81263495012@chatroom"]')
    expect(row).to_contain_text("读取中")
    page.evaluate("""() => {
      chatTest.retainedForm = document.querySelector('#groupPolicyForm');
      chatTest.oldMembersReply = chatTest.finishMembers;
    }""")
    before = directory_calls(page)
    page.locator('.nav-item[data-page="home"]').click()
    page.locator('.nav-item[data-page="groups"]').click()
    expect(row).to_contain_text("待刷新")
    expect(row).not_to_contain_text("读取中")
    assert directory_calls(page) == before, "Returning to cached groups must not restart directory reads"
    assert page.evaluate("chatTest.retainedForm.isConnected")
    expect(page.locator(f'{form} [name="daily_limit"]')).to_have_value("17")
    expect(page.locator(f'{form} details:has([name="daily_limit"])')).to_have_attribute("open", "")
    page.evaluate("""async () => {
      chatTest.oldMembersReply();
      await new Promise(requestAnimationFrame);
      await new Promise(requestAnimationFrame);
      delete chatTest.finishMembers;
    }""")
    expect(row).to_contain_text("待刷新")
    expect(row).not_to_contain_text("23 人")
    assert directory_calls(page) == before
    assert page.evaluate("chatTest.retainedForm.isConnected")
    page.screenshot(path=str(tmp_path / "member-count-cancelled.png"), animations="disabled")
    page.locator("#refreshGroups").click()
    page.wait_for_function("typeof chatTest.finishMembers === 'function'")
    assert len(directory_calls(page)) == len(before) + 1
    page.evaluate("chatTest.finishMembers()")
    expect(row).to_contain_text("23 人")
    expect(page.locator(f'{form} [name="daily_limit"]')).to_have_value("17")
    page.screenshot(path=str(tmp_path / "member-count-manually-restored.png"), animations="disabled")
