"""Pending confirmation writes participate in the native close guard."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_update_flow_webui import _download_and_wait_for_consent, update_page  # noqa: F401
from wechatrebot_manager.windows_tray import _BEFORE_UNLOAD_SCRIPT


@pytest.mark.parametrize("operation", ["backup", "binding", "group"])
@pytest.mark.parametrize("success", [True, False])
def test_close_guard_waits_for_confirmation_write(browser, manager_url, operation, success):
    page = browser.new_page(viewport={"width": 1080, "height": 860}, reduced_motion="reduce")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""async operation => {
          const api=window.pywebview.api, core=api.request_robot_core;
          if (operation === 'binding') {
            window.closeGuardTemplate=(await core('POST','profiles/create',{name:'关闭保护方案',from_global:true})).data.id;
          }
          window.closeGuardWrites=0;
          const hold=(read,args) => {
            window.closeGuardWrites++;
            return new Promise(resolve => {window.finishCloseGuardWrite=async success => resolve(success
              ? await read(...args) : {ok:false,error:{code:'19000',message:'模拟确认操作失败',recorded:true}});});
          };
          if (operation === 'binding') api.request_robot_core=(...args) => args[0] === 'POST' && args[1] === 'bindings/set'
            ? hold(core,args) : core(...args);
          else {
            const method=operation === 'backup' ? 'create_backup' : 'save_group_policy';
            const read=api[method]; api[method]=(...args) => hold(read,args);
          }
        }""", operation)
        if operation == "backup":
            page.locator('.nav-item[data-page="settings"]').click()
            page.locator('[data-settings-tab="system"]').click()
            page.locator("#createBackup").click()
        else:
            page.locator('.nav-item[data-page="groups"]').click()
            page.locator("#groupRows tr").first.click()
            if operation == "binding":
                page.locator("#groupChatProfile select").select_option(page.evaluate("window.closeGuardTemplate"))
            else:
                # Submitting unchanged rules leaves no draft to mask the busy guard.
                page.locator('button[form="groupPolicyForm"]').click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        expect(page.locator("#modalBackdrop")).to_have_attribute("data-busy", "false")
        assert not page.evaluate(_BEFORE_UNLOAD_SCRIPT)
        page.locator("#modalConfirm").click()
        page.wait_for_function("window.closeGuardWrites === 1")
        expect(page.locator("#modalBackdrop")).to_have_attribute("data-busy", "true")
        expect(page.locator("#modalCancel")).to_be_disabled()
        assert page.evaluate(_BEFORE_UNLOAD_SCRIPT)
        page.evaluate("success => window.finishCloseGuardWrite(success)", success)
        expect(page.locator("#modalBackdrop")).to_have_attribute("data-busy", "false")
        if success:
            expect(page.locator("#modalBackdrop")).to_be_hidden()
        else:
            expect(page.locator("#modalError")).to_contain_text("模拟确认操作失败")
        assert not page.evaluate(_BEFORE_UNLOAD_SCRIPT)
        assert page.evaluate("window.closeGuardWrites") == 1
    finally:
        page.close()


def test_install_confirmation_reaches_api_and_protects_pending_request(update_page):
    page = update_page
    _download_and_wait_for_consent(page)
    page.evaluate("""() => {
      const install=window.pywebview.api.install_update;
      window.pywebview.api.install_update=(...args) => new Promise(resolve => {
        window.finishCloseGuardInstall=async () => resolve(await install(...args));
      });
    }""")
    assert not page.evaluate(_BEFORE_UNLOAD_SCRIPT)
    page.locator("#modalConfirm").click()
    page.wait_for_function("typeof window.finishCloseGuardInstall === 'function'")
    expect(page.locator("#modalBackdrop")).to_have_attribute("data-busy", "true")
    assert not page.evaluate("window.updateTestState.updateInstalling")
    assert page.evaluate(_BEFORE_UNLOAD_SCRIPT)
    page.evaluate("window.finishCloseGuardInstall()")
    expect(page.locator("#modalBackdrop")).to_be_hidden()
    assert page.evaluate("window.updateCalls.install") == [["1.0.1", "consent-release-first"]]
    assert page.evaluate("window.updateTestState.updateInstalling")
