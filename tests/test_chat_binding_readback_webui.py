"""Confirmed template bindings recover with reads instead of repeated writes."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize('kind', ['group', 'private'])
def test_confirmed_chat_binding_recovers_without_repeating_write(browser, kind):
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.set_content('<div id="profile"></div>')
        page.add_script_tag(path=str(Path(__file__).parents[1] / 'src/wechatrebot_manager/app_webui/chat-profiles.js'))
        page.evaluate("""async kind => {
          const state = {groupAccount:'account',contactAccount:'account',selectedGroup:'group',selectedContact:'person'};
          const key = kind === 'group' ? 'group' : 'wechat:FriendMessage:person';
          const profiles = {templates:[{id:'custom',name:'安静方案'}],bindings:{}};
          window.bindingWrites = 0;
          window.failBindingRead = true;
          window.bindingController = window.createChatProfiles({state,escapeHtml:String,openCoreTemplates:()=>{},
            confirmAction: value => {window.bindingConfirmation=value;},
            api: async (_,method,endpoint,payload) => {
              if (endpoint === 'profiles') {
                if (window.bindingWrites && window.failBindingRead) throw new Error('模拟保存后读取中断');
                return structuredClone(profiles);
              }
              if (endpoint === 'chats') return [{chat_id:key}];
              if (endpoint === 'chat_status') return [];
              window.bindingWrites++;
              profiles.bindings[payload.chat_id]=payload.template_id;
              return null; // The production binding endpoint returns a null data body.
            }});
          await window.bindingController.mount(document.querySelector('#profile'),kind === 'group' ? 'group' : 'person',kind,'account');
        }""", kind)
        select = page.locator('#profile select')
        select.select_option('custom')
        await_result = page.evaluate("async () => {try {await window.bindingConfirmation.onConfirm();return '';} catch(error) {return error.message;}}")
        assert await_result == ''
        expect(select).to_have_value('custom')
        expect(select).to_be_disabled()
        expect(page.locator('#profile [role="status"]')).to_contain_text('已提交')
        retry = page.get_by_role('button', name='重新读取方案')
        expect(retry).to_be_visible()
        retry.click()
        expect(retry).to_be_enabled()
        expect(select).to_be_disabled()
        assert page.evaluate('window.bindingWrites') == 1
        page.evaluate('window.failBindingRead = false')
        retry.click()
        expect(select).to_be_enabled()
        expect(select).to_have_value('custom')
        expect(page.locator('#profile [role="status"]')).to_contain_text('回读确认')
        expect(retry).to_have_count(0)
        assert page.evaluate('window.bindingWrites') == 1
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize('kind,width', [('group', 1380), ('private', 430)])
def test_binding_readback_failure_closes_completed_confirmation(browser, manager_url, tmp_path, kind, width):
    page = browser.new_page(viewport={'width': width, 'height': 920})
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.evaluate("""async () => {
          const api = window.pywebview.api, request = api.request_robot_core;
          const created = await request('POST','profiles/create',{name:'安静方案'});
          window.bindingTemplateId = created.data.id;
          window.bindingWrites = 0; window.failBindingRead = true;
          api.request_robot_core = async (...args) => {
            if (args[0] === 'GET' && args[1] === 'profiles' && window.bindingWrites && window.failBindingRead)
              return {ok:false,error:{message:'模拟应用后读取中断',recorded:true}};
            if (args[0] === 'POST' && args[1] === 'bindings/set') window.bindingWrites++;
            return request(...args);
          };
        }""")
        page.locator('.nav-item[data-page="groups"]').click()
        if kind == 'private':
            page.locator('#privateManagementTab').click()
            page.locator('#contactRows .contact-row').first.click()
        else:
            page.locator('#groupRows .group-row').first.click()
        root = page.locator('#privateChatProfile' if kind == 'private' else '#groupChatProfile')
        select = root.locator('select')
        template_id = page.evaluate('window.bindingTemplateId')
        expect(select).to_be_enabled()
        select.select_option(template_id)
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        expect(select).to_have_value(template_id)
        expect(select).to_be_disabled()
        expect(root).to_contain_text('无需重复应用')
        root.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f'binding-pending-{kind}-{width}.png'), animations='disabled')
        page.evaluate('window.failBindingRead = false')
        root.get_by_role('button', name='重新读取方案').click()
        expect(select).to_be_enabled()
        expect(root).to_contain_text('已应用并回读确认')
        assert page.evaluate('window.bindingWrites') == 1
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert page.url == manager_url and page.title()
        assert not errors, errors
    finally:
        page.close()
