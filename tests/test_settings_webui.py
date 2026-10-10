"""Focused settings/chat UI regressions; preview data only, no robot."""
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("width,height", [(1440, 1024), (1080, 680)])
def test_flat_settings_and_aligned_chat_details(browser, manager_url, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#globalRefresh")).to_be_enabled()
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="core"]').click()
        expect(page.locator("#coreForm")).to_be_visible()
        paths = page.locator("#coreForm .config-input").evaluate_all(
            "els => els.map(e => e.dataset.path)")
        assert len(paths) == len(set(paths)), "Fields must not appear in multiple sections"
        expect(page.locator("#memoryForm, #emotionForm, [data-core-tab]")).to_have_count(0)
        page.locator('#coreForm [name="robot_name"]').fill("未保存名称")
        page.locator('[data-settings-tab="system"]').click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(page.locator('#coreForm [name="robot_name"]')).to_have_value("未保存名称")
        page.locator('[data-settings-tab="search"]').click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#webSearchForm")).to_be_visible()
        page.locator('#webSearchForm [name="enabled"]').check(force=True)
        page.locator("#webSearchKey").fill("preview-draft")
        page.locator('[data-settings-tab="model"]').click()
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchKey")).to_have_value("preview-draft")
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator("#groupRows tr").first.click()
        expect(page.locator("#groupRows .group-list-count").first).to_have_text("128 人")
        expect(page.locator("#groupChatProfile select")).to_be_enabled()
        controls = page.locator('#groupChatProfile select, #groupPolicyForm select[name="reply_mode"]').evaluate_all(
            "els => els.map(e => ({x:e.getBoundingClientRect().x, right:e.getBoundingClientRect().right}))")
        assert abs(controls[0]["x"] - controls[1]["x"]) <= 1
        assert abs(controls[0]["right"] - controls[1]["right"]) <= 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.locator("#groupChatProfile select").select_option("__manage_templates__")
        expect(page.locator("#coreTemplates")).to_have_attribute("open", "")
        expect(page.locator("#coreWorkspace")).to_be_visible()
        assert not errors
    finally:
        page.close()


@pytest.mark.parametrize("scenario", ["apply", "cancel", "account", "stale", "conflict", "readback", "private", "ambiguous", "missing", "offline"])
def test_chat_profile_selection_preserves_scope_and_confirms_readback(browser, scenario):
    page = browser.new_page()
    try:
        page.set_content('<div id="profile"></div>')
        page.add_script_tag(path=str(Path(__file__).parents[1] / "src/wechatrebot_manager/app_webui/chat-profiles.js"))
        result = page.evaluate("""async scenario => {
          const node = document.querySelector('#profile');
          const state = {groupAccount:'account', contactAccount:'account', selectedGroup:'group', selectedContact:'person'};
          const kind = scenario === 'private' || scenario === 'missing' || scenario === 'ambiguous' ? 'private' : 'group';
          const key = kind === 'private' ? 'wechat:FriendMessage:person' : 'group';
          const profiles = {templates:[{id:'custom', name:'安静方案'}], bindings:{}};
          const writes = []; let confirmation; let gets = 0;
          const controller = window.createChatProfiles({state, escapeHtml:s=>String(s), openCoreTemplates:()=>{},
            confirmAction: value => {confirmation=value;},
            api: async (_, method, endpoint, payload) => {
              if (scenario === 'offline') throw new Error('offline');
              if (endpoint === 'profiles') {
                gets++;
                if (scenario === 'stale') state.selectedGroup='elsewhere';
                if (scenario === 'conflict' && gets === 2) profiles.bindings[key]='changed';
                return structuredClone(profiles);
              }
              if (endpoint === 'chats') return scenario === 'missing' ? [] :
                scenario === 'ambiguous' ? [{chat_id:key},{chat_id:'other:FriendMessage:person'}] : [{chat_id:key}];
              if (endpoint === 'chat_status') return [];
              writes.push(payload);
              if (scenario !== 'readback') profiles.bindings[payload.chat_id]=payload.template_id;
              return {};
            }});
          await controller.mount(node, kind === 'private' ? 'person' : 'group', kind, 'account');
          const select=node.querySelector('select');
          if (!select || select.disabled) return {writes, disabled:select?.disabled, text:node.textContent};
          select.value='custom'; select.dispatchEvent(new Event('change'));
          const before=select.value;
          if (scenario === 'account') state.groupAccount='other';
          let error='';
          if (scenario !== 'cancel') {try {await confirmation.onConfirm();} catch(e) {error=e.message;}}
          return {writes, before, after:select.value, error, text:node.textContent};
        }""", scenario)
        if scenario in {"apply", "private", "readback"}:
            assert len(result["writes"]) == 1
            expected = "wechat:FriendMessage:person" if scenario == "private" else "group"
            assert result["writes"][0] == {"chat_id": expected, "template_id": "custom"}
            if scenario == "readback":
                assert result["error"] and result["after"] == ""
            else:
                assert result["before"] == "" and result["after"] == "custom"
                assert "回读确认" in result["text"]
        else:
            assert result["writes"] == []
            if scenario == "missing":
                assert result["disabled"]
            if scenario in {"account", "conflict"}:
                assert result["error"]
    finally:
        page.close()
