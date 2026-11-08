"""Use the group's verified save receipt instead of replaying a confirmed save."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def test_group_policy_uses_verified_save_receipt_when_followup_read_is_unavailable(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={'width': 1380, 'height': 920})
    try:
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('#groupRows .group-row').first.click()
        mode = page.locator('#groupPolicyForm [name="reply_mode"]')
        expect(mode).to_be_visible()
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_group_policy, save = api.save_group_policy;
          window.groupSaveCalls = [];
          window.groupSaved = false;
          window.groupReadAfterSave = 0;
          api.save_group_policy = async (...args) => {
            window.groupSaveCalls.push(structuredClone(args));
            const result = await save(...args);
            window.groupSaveReceipt = structuredClone(result.data);
            window.groupSaved = true;
            return result;
          };
          api.get_group_policy = (...args) => {
            if (!window.groupSaved) return read(...args);
            window.groupReadAfterSave++;
            return {ok:false,error:{message:'模拟保存后的读取暂不可用',recorded:true}};
          };
        }""")
        mode.select_option('off')
        page.locator('button[form="groupPolicyForm"]').click()
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        expect(mode).to_have_value('off')
        expect(page.locator('#groupPolicyNote')).to_contain_text('已保存')
        assert page.evaluate('window.groupReadAfterSave') == 0
        assert page.evaluate('window.groupSaveCalls.length') == 1
        assert page.locator('#groupPolicyForm').get_attribute('data-revision') == page.evaluate('window.groupSaveReceipt.revision')
        page.locator('#groupPolicyNote').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / 'group-save-verified-receipt.png'), animations='disabled')
        # The next deliberate edit must use the received revision, without
        # repeating the successful off operation just to refresh the form.
        mode.select_option('mention')
        page.locator('button[form="groupPolicyForm"]').click()
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        assert page.evaluate('window.groupSaveCalls.length') == 2
        assert page.evaluate('window.groupSaveCalls[1][3]') != page.evaluate('window.groupSaveCalls[0][3]')
        expect(mode).to_have_value('mention')
    finally:
        page.close()


@pytest.mark.parametrize('failure', ['rejected', 'wrong_scope'])
def test_group_policy_unconfirmed_save_keeps_draft_and_original_revision(browser, manager_url, failure):
    page = browser.new_page()
    try:
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('#groupRows .group-row').first.click()
        mode = page.locator('#groupPolicyForm [name="reply_mode"]')
        expect(mode).to_be_visible()
        revision = page.locator('#groupPolicyForm').get_attribute('data-revision')
        page.evaluate("""failure => {
          const api = window.pywebview.api, save = api.save_group_policy;
          window.groupSaveCalls = 0;
          api.save_group_policy = async (...args) => {
            window.groupSaveCalls++;
            if (failure === 'rejected')
              return {ok:false,error:{message:'模拟保存未完成',recorded:true}};
            const result = await save(...args);
            delete result.message;
            result.data.group_id = 'different@chatroom';
            return result;
          };
        }""", failure)
        mode.select_option('off')
        page.locator('button[form="groupPolicyForm"]').click()
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_visible()
        expect(page.locator('#modalError')).to_contain_text('保存未完成' if failure == 'rejected' else '未收到本群规则的保存确认')
        page.locator('#modalCancel').click()
        expect(mode).to_have_value('off')
        expect(page.locator('#groupPolicyNote')).to_contain_text('未保存')
        assert page.locator('#groupPolicyForm').get_attribute('data-revision') == revision
        assert page.evaluate('window.groupSaveCalls') == 1
    finally:
        page.close()


def test_group_policy_late_save_receipt_does_not_replace_another_account(browser, manager_url):
    page = browser.new_page()
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('#groupRows .group-row').first.click()
        mode = page.locator('#groupPolicyForm [name="reply_mode"]')
        expect(mode).to_be_visible()
        page.evaluate("""() => {
          const api = window.pywebview.api, groups = api.get_groups;
          const save = api.save_group_policy, read = api.get_group_policy;
          api.save_group_policy = async (...args) => {
            const result = await save(...args);
            return new Promise(resolve => { window.finishOldGroupSave = () => resolve(result); });
          };
          api.get_groups = async (...args) => {
            const result = await groups(...args);
            result.data.account_id = 'second-account';
            return result;
          };
          api.get_group_policy = async (...args) => {
            const result = await read(...args);
            result.data.policy.account_id = args[0];
            result.data.policy.reply_mode = 'natural';
            return result;
          };
        }""")
        mode.select_option('off')
        page.locator('button[form="groupPolicyForm"]').click()
        page.locator('#modalConfirm').click()
        page.wait_for_function("typeof window.finishOldGroupSave === 'function'")
        # A runtime/account refresh can occur while the save confirmation is busy.
        page.evaluate("document.querySelector('#refreshGroups').click()")
        expect(page.locator('#groupInspector')).to_contain_text('选择一个群聊')
        page.evaluate('window.finishOldGroupSave()')
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        page.locator('#groupRows .group-row').first.click()
        expect(mode).to_have_value('natural')
        expect(page.locator('#groupPolicyNote')).not_to_contain_text('已保存')
        assert not errors, errors
    finally:
        page.close()


def test_new_group_edits_during_slow_preview_survive_confirmed_save(browser, manager_url):
    page = browser.new_page()
    try:
        page.goto(manager_url)
        expect(page.locator('#stateTitle')).to_have_text('机器人可用')
        if page.locator('#onboardingRecommended').is_visible():
            page.locator('#onboardingRecommended').click()
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator('#groupRows .group-row').first.click()
        mode = page.locator('#groupPolicyForm [name="reply_mode"]')
        expect(mode).to_be_visible()
        original_revision = page.locator('#groupPolicyForm').get_attribute('data-revision')
        page.evaluate("""() => {
          const api = window.pywebview.api, preview = api.preview_group_policy;
          api.preview_group_policy = async (...args) => {
            const result = await preview(...args);
            return new Promise(resolve => { window.finishGroupPreview = () => resolve(result); });
          };
        }""")
        mode.select_option('off')
        page.locator('button[form="groupPolicyForm"]').click()
        page.wait_for_function("typeof window.finishGroupPreview === 'function'")
        mode.select_option('natural')
        page.evaluate('window.finishGroupPreview()')
        page.locator('#modalConfirm').click()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        expect(mode).to_have_value('natural')
        expect(page.locator('#groupPolicyNote')).to_contain_text('未保存')
        assert page.locator('#groupPolicyForm').get_attribute('data-revision') != original_revision
    finally:
        page.close()
