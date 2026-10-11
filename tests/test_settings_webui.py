"""Focused settings/chat UI regressions; preview data only, no robot."""
from pathlib import Path
import re

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
        if width > 1100:
            inspector = page.locator("#groupInspector")
            before = inspector.bounding_box()["width"]
            handle = inspector.locator(".panel-resize-handle")
            handle.press("ArrowLeft")
            expect(inspector).to_have_class(re.compile("has-panel-size"))
            assert inspector.bounding_box()["width"] < before
            assert page.locator("#groupRows").bounding_box()["width"] >= 250
            handle.press("Home")
            assert abs(inspector.bounding_box()["width"] - before) <= 1
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


def test_group_switch_reuses_rules_and_keeps_drafts(browser, manager_url):
    page = browser.new_page()
    try:
        page.goto(manager_url)
        expect(page.locator("#globalRefresh")).to_be_enabled()
        page.evaluate("""() => {
          window.policyReads = 0; window.forcedGroupReads = 0;
          const api = window.pywebview.api;
          const policy = api.get_group_policy;
          api.get_group_policy = (...args) => { window.policyReads++; return policy(...args); };
          const groups = api.get_groups;
          api.get_groups = (...args) => { if(args[0]) window.forcedGroupReads++; return groups(...args); };
        }""")
        page.locator('.nav-item[data-page="groups"]').click()
        rows = page.locator('#groupRows .group-row')
        rows.nth(0).click()
        mode = page.locator('#groupPolicyForm [name="reply_mode"]')
        expect(mode).to_be_visible()
        mode.select_option('off')
        rows.nth(1).click()
        expect(mode).to_be_visible()
        rows.nth(0).click()
        expect(mode).to_have_value('off')
        page.locator('[data-chat-management-tab="private"]').click()
        page.locator('[data-chat-management-tab="groups"]').click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('.nav-item[data-page="groups"]').click()
        expect(mode).to_have_value('off')
        assert page.evaluate('window.policyReads') == 2
        assert page.evaluate('window.forcedGroupReads') == 1
        page.locator('#refreshGroupPolicy').click()
        expect(page.locator('#modalBackdrop')).to_be_visible()
        page.locator('#modalCancel').click()
        expect(mode).to_have_value('off')
        page.locator('#refreshGroupPolicy').click()
        page.locator('#modalConfirm').click()
        expect(mode).not_to_have_value('off')
        assert page.evaluate('window.policyReads') == 3
        mode.select_option('off')
        page.locator('button[form="groupPolicyForm"]').click()
        page.locator('#modalConfirm').click()
        expect(page.locator('#groupPolicyNote')).not_to_contain_text('未保存')
        expect(mode).to_have_value('off')
        assert page.evaluate('window.policyReads') == 4
    finally:
        page.close()


def test_template_editor_inheritance_and_switch_guard(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1024})
    try:
        page.goto(manager_url)
        expect(page.locator('#globalRefresh')).to_be_enabled()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="core"]').click()
        expect(page.locator('#activeCoreTemplate')).to_be_visible()
        page.locator('.template-create > summary').click()
        expect(page.locator('#coreTemplateSource')).to_have_value('empty')
        page.locator('#newCoreTemplate').click()
        expect(page.locator('#modalInput')).to_have_value('默认模板')
        page.locator('#modalConfirm').click()
        expect(page.locator('#coreDetail h2')).to_have_text('默认模板')
        control = page.locator('#core-config-personality--ai_self_identity')
        expect(control).to_be_disabled()
        row = page.locator('.form-field').filter(has=control)
        row.locator('.core-override input').check()
        control.fill('只在此模板中使用的身份')
        page.locator('#activeCoreTemplate').select_option('')
        page.locator('#modalCancel').click()
        expect(control).to_have_value('只在此模板中使用的身份')
        page.locator('#saveCore').click()
        expect(page.locator('#coreNote')).to_contain_text('模板已保存')
        page.locator('#refreshCore').click()
        expect(control).to_have_value('只在此模板中使用的身份')
        expect(row.locator('.core-override input')).to_be_checked()
        row.locator('.core-override input').uncheck()
        page.locator('#saveCore').click()
        expect(control).to_be_disabled()
        expect(control).not_to_have_value('只在此模板中使用的身份')
        assert page.locator('#coreForm label label').count() == 0
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
    finally:
        page.close()


def test_group_policy_old_account_response_is_discarded(browser, manager_url):
    page = browser.new_page()
    try:
        page.goto(manager_url)
        expect(page.locator('#globalRefresh')).to_be_enabled()
        page.evaluate("""() => {
          const api = window.pywebview.api;
          const groups = api.get_groups, policy = api.get_group_policy;
          window.changedAccount = false; window.policyReads = 0;
          api.get_groups = async (...args) => {
            const result = await groups(...args);
            return {...result, data:{...result.data, account_id:window.changedAccount ? 'second-account' : 'first-account'}};
          };
          api.get_group_policy = async (...args) => {
            window.policyReads++;
            const result = await policy(...args);
            const value = {...result, data:{...result.data, policy:{...result.data.policy, reply_mode:args[0] === 'first-account' ? 'off' : 'natural'}}};
            if(args[0] === 'first-account') return new Promise(resolve => { window.finishOldPolicy = () => resolve(value); });
            return value;
          };
        }""")
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('#groupRows .group-row').first.click()
        expect(page.locator('#groupInspector')).to_contain_text('正在读取规则')
        page.locator('#groupRows .group-row').first.click()
        assert page.evaluate('window.policyReads') == 1
        page.evaluate('window.changedAccount = true')
        page.locator('#refreshGroups').click()
        expect(page.locator('#groupInspector')).to_contain_text('选择一个群聊')
        page.locator('#groupRows .group-row').first.click()
        mode = page.locator('#groupPolicyForm [name="reply_mode"]')
        expect(mode).to_have_value('natural')
        page.evaluate('window.finishOldPolicy()')
        expect(mode).to_have_value('natural')
        assert page.evaluate('window.policyReads') == 2
    finally:
        page.close()


def test_chat_profile_cache_is_shared_and_invalidated(browser):
    page = browser.new_page()
    try:
        page.set_content('<div id="profile"></div>')
        page.add_script_tag(path=str(Path(__file__).parents[1] / 'src/wechatrebot_manager/app_webui/chat-profiles.js'))
        result = page.evaluate("""async () => {
          let reads = 0;
          const node = document.querySelector('#profile');
          const state = {groupAccount:'a', selectedGroup:'one'};
          const controller = window.createChatProfiles({state, escapeHtml:String, confirmAction:()=>{}, openCoreTemplates:()=>{},
            api: async (_, method, endpoint) => {
              if(endpoint === 'profiles') { reads++; return {templates:[], bindings:{}}; }
              return [];
            }});
          await controller.mount(node, 'one', 'group', 'a');
          state.selectedGroup = 'two';
          await controller.mount(node, 'two', 'group', 'a');
          const reused = reads;
          state.groupAccount = 'b';
          await controller.mount(node, 'two', 'group', 'b');
          const isolated = reads;
          window.dispatchEvent(new Event('chat-profiles-changed'));
          await controller.mount(node, 'two', 'group', 'b');
          const invalidated = reads;
          controller.invalidate();
          await controller.mount(node, 'two', 'group', 'b');
          return {reused, isolated, invalidated, refreshed:reads};
        }""")
        assert result == {"reused": 1, "isolated": 2, "invalidated": 3, "refreshed": 4}
    finally:
        page.close()

@pytest.mark.parametrize("width", [1440, 600])
def test_settings_navigation_capsules_spacing_and_history_images(browser, manager_url, tmp_path, width):
    import base64
    import io
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (320, 240), "#6489bf").save(output, format="WEBP")
    preview = "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode()
    page = browser.new_page(viewport={"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#globalRefresh")).to_be_enabled()
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        assert page.locator('.main-nav [data-page]').evaluate_all("els => els.map(e => e.dataset.page)") == [
            "home", "features", "history", "schedules", "activity", "subscription", "groups", "settings",
        ]
        page.locator('.sidebar-meta [data-page="help"]').click()
        expect(page.locator("#page-help")).to_be_visible()
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator("#groupRows tr").first.click()
        expect(page.locator("#groupRows .group-list-count").first).to_have_text("128 人")
        expect(page.locator("#groupScheduledEnabled")).not_to_be_visible()
        page.get_by_text("定时任务与安静时段", exact=True).click()
        expect(page.locator("#groupScheduledEnabled")).to_be_attached()
        assert page.locator("#refreshGroupPolicy").evaluate("e => getComputedStyle(e).borderRadius") == "999px"
        page.get_by_text("定时任务与安静时段", exact=True).click()
        page.screenshot(path=str(tmp_path / f"groups-{width}.png"))
        page.locator("#themeToggle").click()
        expect(page.locator("#themeToggle")).to_be_enabled()
        page.screenshot(animations="disabled", path=str(tmp_path / f"groups-dark-{width}.png"))
        page.locator("#themeToggle").click()
        expect(page.locator("#themeToggle")).to_be_enabled()
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator("#pageTitle")).to_have_text("机器人总设置")
        page.locator('[data-settings-tab="search"]').click()
        page.locator('#webSearchForm [name="enabled"]').check(force=True)
        expect(page.locator("#webSearchProviderSummary")).to_be_visible()
        assert page.locator("#webSearchProviderSummary").evaluate("e => parseFloat(getComputedStyle(e).marginBottom)") >= 20
        page.screenshot(path=str(tmp_path / f"search-{width}.png"))
        page.locator('[data-settings-tab="core"]').click()
        expect(page.locator("#coreForm")).to_be_visible()
        page.screenshot(path=str(tmp_path / f"core-{width}.png"))
        page.evaluate("""preview => {
          const api = window.pywebview.api, read = api.get_messages;
          api.get_messages = async (...args) => {
            const result = await read(...args), messages = result.data.items || result.data;
            messages[0].message_kind = ''; messages[0].content = '[表情]'; messages[0].media_previews = [preview];
            messages[1].content = '[图片]'; messages[1].media_previews = ['https://invalid.test/private.gif'];
            return result;
          };
        }""", preview)
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator(".message-media img")).to_have_count(1)
        page.locator(".message-media img").scroll_into_view_if_needed()
        expect(page.locator(".message-media img")).to_be_visible()
        page.wait_for_function("document.querySelector('.message-media img')?.naturalWidth === 320")
        expect(page.locator(".message-media img")).to_have_attribute("alt", "表情首帧")
        assert not page.locator('img[src*="invalid.test"]').count()
        page.screenshot(path=str(tmp_path / f"history-{width}.png"))
        page.locator(".message-media img").dispatch_event("error")
        expect(page.get_by_text("表情首帧暂不可用", exact=True)).to_be_visible()
        assert page.url == manager_url and page.title().startswith("消息中心")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        # Help loads the existing hidden feature iframe without src; Chromium
        # reports this pre-existing CSP warning. No external frame is loaded.
        unexpected = [error for error in errors if not (
            error.startswith(("Refused to frame '' because it violates", "Framing '' violates"))
            and '"frame-src \'self\'"' in error
        )]
        assert not unexpected, unexpected
        print(f"UI evidence: {tmp_path}; existing blank-frame CSP warnings: {len(errors)}")
    finally:
        page.close()
