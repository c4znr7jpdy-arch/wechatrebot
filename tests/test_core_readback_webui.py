"""A confirmed core write must not be replayed when its follow-up read fails."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_settings_webui import _open_core_draft_page


@pytest.mark.parametrize("action", ["global", "create", "update", "rename", "delete"])
def test_confirmed_core_write_recovers_with_reads_only(browser, manager_url, tmp_path, action):
    page = _open_core_draft_page(browser, manager_url)
    try:
        if action in {"update", "rename", "delete"}:
            page.locator('.template-create > summary').click()
            page.locator('#newCoreTemplate').click()
            page.locator('#modalConfirm').click()
            expect(page.locator('#coreDetail h2')).to_have_text('默认模板')
            page.wait_for_function('!window.coreDraftController.busy')
        page.evaluate("""action => {
          const api = window.pywebview.api, read = api.get_robot_core;
          const request = api.request_robot_core, save = api.save_robot_core;
          window.coreConfirmedWrites = 0;
          window.coreFailReadback = true;
          window.coreWritten = false;
          window.coreReadbackAction = action;
          api.save_robot_core = async (...args) => {
            window.coreConfirmedWrites++;
            const result = await save(...args);
            window.coreWritten = true;
            return result;
          };
          api.get_robot_core = (...args) => window.coreWritten && window.coreFailReadback
            ? {ok:false,error:{message:'模拟后续读取失败',recorded:true}} : read(...args);
          api.request_robot_core = async (...args) => {
            if (args[0] === 'POST') {
              window.coreConfirmedWrites++;
              const result = await request(...args);
              window.coreWritten = true;
              return result;
            }
            if (window.coreWritten && window.coreFailReadback && args[1] === 'profiles')
              return {ok:false,error:{message:'模拟后续读取失败',recorded:true}};
            return request(...args);
          };
        }""", action)
        if action == "global":
            page.locator('#coreForm [name="robot_name"]').fill('已经保存的名称')
            page.locator('#saveCore').click()
        elif action == "create":
            page.locator('.template-create > summary').click()
            page.locator('#newCoreTemplate').click()
            page.locator('#modalConfirm').click()
        elif action == "update":
            page.locator('#coreForm [name="template_description"]').fill('已经保存的说明')
            page.locator('#saveCore').click()
        elif action == "rename":
            page.locator('#renameCoreTemplate').click()
            page.locator('#modalInput').fill('已经重命名的模板')
            page.locator('#modalConfirm').click()
        else:
            page.locator('#deleteCoreTemplate').click()
            page.locator('#modalConfirm').click()
        page.wait_for_function('window.coreConfirmedWrites === 1 && !window.coreDraftController.busy')
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        expect(page.locator('#coreNote')).to_contain_text('重新读取')
        expect(page.locator('#coreNote')).to_contain_text('已')
        expect(page.locator('#saveCore')).to_be_disabled()
        expect(page.locator('#newCoreTemplate')).to_be_disabled()
        expect(page.locator('#refreshCore')).to_be_enabled()
        page.screenshot(path=str(tmp_path / f'core-{action}-confirmed-read-failed.png'), animations='disabled')
        # Failed read-only retries must keep the lock and the receipt visible.
        page.locator('#refreshCore').click()
        page.wait_for_function('!window.coreDraftController.busy')
        expect(page.locator('#saveCore')).to_be_disabled()
        assert page.evaluate('window.coreConfirmedWrites') == 1
        page.evaluate('window.coreFailReadback = false')
        page.locator('#refreshCore').click()
        page.wait_for_function('!window.coreDraftController.busy')
        expect(page.locator('#saveCore')).to_be_enabled()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        assert page.evaluate('window.coreConfirmedWrites') == 1
        profiles = page.evaluate("window.pywebview.api.request_robot_core('GET', 'profiles')")['data']
        assert len(profiles['templates']) == (0 if action in {'global', 'delete'} else 1)
        if action == "global":
            expect(page.locator('#coreForm [name="robot_name"]')).to_have_value('已经保存的名称')
        elif action == "delete":
            expect(page.locator('#coreDetail h2')).to_have_text('机器人配置')
        else:
            expect(page.locator('#coreDetail h2')).to_have_text('已经重命名的模板' if action == 'rename' else '默认模板')
            if action == 'update':
                expect(page.locator('#coreForm [name="template_description"]')).to_have_value('已经保存的说明')
        page.screenshot(path=str(tmp_path / f'core-{action}-read-recovered.png'), animations='disabled')
    finally:
        page.close()
