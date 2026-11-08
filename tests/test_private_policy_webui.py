"""Private rule drafts stay scoped to their contact and current WeChat account."""

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_update_flow_webui import _check_automatically, update_page  # noqa: F401


def open_private_rules(page, manager_url, *, deferred_detail=False):
    page.goto(manager_url)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""deferred => {
      const api = window.pywebview.api;
      window.privateAccount = 'wxid_demo_account';
      window.privateReads = 0; window.privateWrites = [];
      window.privateSaved = new Map();
      const readPolicy = api.get_private_policy, details = api.get_contact_detail;
      api.get_contacts = async () => ({ok:true, data:{account_id:window.privateAccount, items:[
        {user_id:'private_a',name:'联系人甲'}, {user_id:'private_b',name:'联系人乙'}]}});
      api.get_private_policy = async (...args) => {
        window.privateReads++;
        const result = await readPolicy(...args);
        result.data.policy.account_id = args[0];
        if (window.privateSaved.has(args.join('/')))
          result.data.policy = structuredClone(window.privateSaved.get(args.join('/')));
        return result;
      };
      api.get_contact_detail = async id => {
        if (deferred && id === 'private_a')
          await new Promise(resolve => {window.finishPrivateDetail = resolve;});
        const result = await details(id);
        window.privateDetailCompleted = true;
        return result;
      };
      api.save_private_policy = async (account, id, values, revision) => {
        window.privateWrites.push({account, id, values:structuredClone(values), revision});
        if (window.deferPrivateSave) await new Promise(resolve => {window.finishPrivateSave = resolve;});
        if (window.rejectPrivateSave) throw new Error('模拟保存失败');
        const policy = {...structuredClone(values), account_id:account, contact_id:id,
          saved:true, configured:true, revision:`private-saved-${window.privateWrites.length}`};
        window.privateSaved.set([account,id].join('/'), policy);
        return {ok:true, data:policy};
      };
    }""", deferred_detail)
    page.locator('.nav-item[data-page="groups"]').click()
    page.locator("#privateManagementTab").click()
    select_contact(page, "private_a")


def select_contact(page, contact_id):
    page.locator(f'.contact-row[data-contact-id="{contact_id}"]').click()
    expect(page.locator("#privatePolicyForm")).to_be_visible()


def edit_limit(page, value):
    details = page.locator("#privatePolicyForm details").first
    if details.get_attribute("open") is None:
        details.locator("summary").click()
    page.locator('#privatePolicyForm [name="daily_limit"]').fill(value)


@pytest.mark.parametrize("resume,width", [("contact", 1380), ("contact", 430), ("tab", 1380), ("page", 1380), ("detail", 1380)])
def test_private_rule_draft_survives_view_changes(browser, manager_url, tmp_path, resume, width):
    page = browser.new_page(viewport={"width": width, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_private_rules(page, manager_url, deferred_detail=resume == "detail")
        edit_limit(page, "17")
        page.locator('#privatePolicyForm label[for="privateBotEnabled"]').click()
        page.locator('#privatePolicyForm label[for="privateQuietEnabled"]').click()
        page.locator('#privatePolicyForm [name="quiet_start"]').fill("21:30")
        page.locator("#privatePolicyForm details").nth(1).locator("summary").click()
        feature = page.locator('#privatePolicyForm [name="allowed_features"]:checked').first
        feature_id = feature.get_attribute("value")
        feature.uncheck()
        if resume == "contact":
            select_contact(page, "private_b")
            expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("3")
            select_contact(page, "private_a")
        elif resume == "tab":
            page.locator("#groupManagementTab").click()
            page.locator("#privateManagementTab").click()
        elif resume == "page":
            page.locator('.nav-item[data-page="home"]').click()
            page.locator('.nav-item[data-page="groups"]').click()
        else:
            page.evaluate("window.finishPrivateDetail()")
            page.wait_for_function("() => window.privateDetailCompleted")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("17")
        expect(page.locator('#privatePolicyForm [name="bot_enabled"]')).not_to_be_checked()
        expect(page.locator('#privatePolicyForm [name="quiet_enabled"]')).to_be_checked()
        expect(page.locator('#privatePolicyForm [name="quiet_start"]')).to_have_value("21:30")
        expect(page.locator(f'#privatePolicyForm [name="allowed_features"][value="{feature_id}"]')).not_to_be_checked()
        expect(page.locator("#privatePolicyNote")).to_contain_text("未保存")
        assert page.evaluate("window.privateReads") == (2 if resume == "contact" else 1)
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"private-draft-{resume}-{width}.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()


def test_private_rule_save_keeps_newer_draft_and_advances_revision(browser, manager_url):
    page = browser.new_page()
    try:
        open_private_rules(page, manager_url)
        edit_limit(page, "17")
        page.evaluate("window.deferPrivateSave = true")
        page.locator('button[form="privatePolicyForm"]').click()
        page.wait_for_function("() => typeof window.finishPrivateSave === 'function'")
        expect(page.locator("#refreshPrivatePolicy")).to_be_disabled()
        edit_limit(page, "19")
        select_contact(page, "private_b")
        select_contact(page, "private_a")
        expect(page.locator('button[form="privatePolicyForm"]')).to_be_disabled()
        page.evaluate("window.deferPrivateSave = false; window.finishPrivateSave()")
        expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("19")
        expect(page.locator("#privatePolicyNote")).to_contain_text("未保存")
        expect(page.locator("#privatePolicyForm")).to_have_attribute("data-revision", "private-saved-1")
        page.locator('button[form="privatePolicyForm"]').click()
        expect(page.locator("#privatePolicyNote")).not_to_contain_text("未保存")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("19")
        writes = page.evaluate("window.privateWrites")
        assert [write["values"]["daily_limit"] for write in writes] == [17, 19]
        assert writes[1]["revision"] == "private-saved-1"
        select_contact(page, "private_b")
        select_contact(page, "private_a")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("19")
    finally:
        page.close()


def test_private_save_failure_keeps_draft(browser, manager_url):
    page = browser.new_page()
    try:
        open_private_rules(page, manager_url)
        edit_limit(page, "17")
        page.evaluate("window.rejectPrivateSave = true")
        page.locator('button[form="privatePolicyForm"]').click()
        expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
        select_contact(page, "private_b")
        select_contact(page, "private_a")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("17")
        expect(page.locator("#privatePolicyNote")).to_contain_text("未保存")
        assert len(page.evaluate("window.privateWrites")) == 1
    finally:
        page.close()


def test_private_save_receipt_survives_unavailable_followup_read(browser, manager_url):
    page = browser.new_page()
    try:
        open_private_rules(page, manager_url)
        edit_limit(page, "17")
        page.evaluate("""() => {
          window.pywebview.api.get_private_policy = async () => {
            window.privateReads++; throw new Error('模拟规则读取中断');
          };
        }""")
        page.locator('button[form="privatePolicyForm"]').click()
        expect(page.locator("#privatePolicyNote")).to_have_text("私聊规则已保存。")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("17")
        expect(page.locator("#privatePolicyForm")).to_have_attribute("data-revision", "private-saved-1")
        expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
        expect(page.locator("#refreshPrivatePolicy")).to_be_enabled()
        assert page.evaluate("window.privateReads") == 1
        page.locator('.contact-row[data-contact-id="private_b"]').click()
        expect(page.locator("#privatePolicyForm")).to_have_count(0)
        page.locator('.contact-row[data-contact-id="private_a"]').click()
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("17")
        expect(page.locator("#privatePolicyNote")).to_have_text("私聊规则已保存。")
        assert len(page.evaluate("window.privateWrites")) == 1
    finally:
        page.close()


@pytest.mark.parametrize("invalid", ["saved", "account_id", "contact_id", "revision"])
def test_private_save_requires_matching_receipt(browser, manager_url, invalid):
    page = browser.new_page()
    try:
        open_private_rules(page, manager_url)
        edit_limit(page, "17")
        page.evaluate("""invalid => {
          const save = window.pywebview.api.save_private_policy;
          window.pywebview.api.save_private_policy = async (...args) => {
            const result = await save(...args);
            result.data[invalid] = invalid === 'saved' ? false : '';
            return result;
          };
        }""", invalid)
        page.locator('button[form="privatePolicyForm"]').click()
        expect(page.locator("#privatePolicyNote")).to_contain_text("未保存")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("17")
        expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
        assert page.evaluate("window.privateReads") == 1
    finally:
        page.close()


@pytest.mark.parametrize("saved", [False, True])
def test_private_close_warning_tracks_unsaved_and_saved_rules(browser, manager_url, saved):
    page = browser.new_page()
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
    try:
        open_private_rules(page, manager_url)
        if saved:
            edit_limit(page, "17")
            page.locator('button[form="privatePolicyForm"]').click()
            expect(page.locator("#privatePolicyNote")).not_to_contain_text("未保存")
            expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
        else:
            page.evaluate("window.deferPrivateSave = true")
            page.locator('button[form="privatePolicyForm"]').click()
            page.wait_for_function("() => typeof window.finishPrivateSave === 'function'")
        page.goto("about:blank")
        assert dialogs == ([] if saved else ["beforeunload"])
    finally:
        page.close()


def test_private_draft_refresh_requires_discard_and_account_switch_clears(browser, manager_url):
    page = browser.new_page()
    try:
        open_private_rules(page, manager_url)
        edit_limit(page, "17")
        page.locator("#refreshPrivatePolicy").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("17")
        assert page.evaluate("window.privateReads") == 1
        page.locator("#refreshPrivatePolicy").click()
        page.locator("#modalConfirm").click()
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("3")
        expect(page.locator("#privatePolicyNote")).not_to_contain_text("未保存")
        edit_limit(page, "19")
        page.evaluate("window.privateAccount = 'wxid_other_account'")
        page.locator("#refreshContacts").click()
        expect(page.locator("#privatePolicyForm")).to_have_count(0)
        select_contact(page, "private_a")
        expect(page.locator('#privatePolicyForm [name="daily_limit"]')).to_have_value("3")
        expect(page.locator("#privatePolicyNote")).not_to_contain_text("未保存")
    finally:
        page.close()


def test_private_draft_warns_before_browser_close(browser, manager_url):
    page = browser.new_page()
    dialogs = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.type), dialog.accept()))
    try:
        open_private_rules(page, manager_url)
        edit_limit(page, "17")
        page.goto("about:blank")
        assert dialogs == ["beforeunload"]
    finally:
        page.close()


def test_update_install_waits_for_private_rule_save(update_page):
    page = update_page
    _check_automatically(page)
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    page.locator('.nav-item[data-page="groups"]').click()
    page.locator("#privateManagementTab").click()
    page.locator("#contactRows .contact-row").first.click()
    expect(page.locator("#privatePolicyForm")).to_be_visible()
    page.evaluate("""() => {
      const api = window.pywebview.api, save = api.save_private_policy;
      api.save_private_policy = async (...args) => new Promise(resolve => {
        window.finishPrivateRuleSave = async () => resolve(await save(...args));
      });
    }""")
    page.locator('button[form="privatePolicyForm"]').click()
    page.wait_for_function("() => typeof window.finishPrivateRuleSave === 'function'")
    page.evaluate("window.finishUpdateDownload(); window.updateTestController.pollUpdateStatus()")
    expect(page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalError")).to_contain_text("请稍候")
    assert page.evaluate("window.updateCalls.install") == []
    page.evaluate("window.finishPrivateRuleSave()")
    expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
    expect(page.locator("#privatePolicyNote")).to_have_text("私聊规则已保存。")
    page.locator("#modalConfirm").click()
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
