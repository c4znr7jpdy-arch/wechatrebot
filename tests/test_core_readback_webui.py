"""A confirmed core write must not be replayed when its follow-up read fails."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_settings_webui import _open_core_draft_page


@pytest.mark.parametrize("action", ["global", "create", "update", "rename", "delete"])
def test_confirmed_core_write_recovers_with_reads_only(browser, manager_url, tmp_path, action):
    page = _open_core_draft_page(browser, manager_url)
    errors = []
    page.on('pageerror', lambda error: errors.append(str(error)))
    try:
        if action == 'global':
            page.set_viewport_size({'width': 600, 'height': 920})
        if action in {"update", "rename", "delete"}:
            page.locator('.template-create > summary').click()
            page.locator('#newCoreTemplate').click()
            page.locator('#modalConfirm').click()
            expect(page.locator('#coreDetail h2')).to_have_text('默认模板')
            page.wait_for_function('!window.coreDraftController.busy')
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_robot_core;
          const request = api.request_robot_core, save = api.save_robot_core;
          window.coreConfirmedWrites = 0;
          window.coreFailReadback = true;
          window.coreWritten = false;
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
        }""")
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
        page.locator('#coreNote').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f'core-{action}-confirmed-read-failed.png'), animations='disabled')
        # Failed read-only retries must keep the lock and the receipt visible.
        page.locator('#refreshCore').click()
        page.wait_for_function('!window.coreDraftController.busy')
        expect(page.locator('#saveCore')).to_be_disabled()
        assert page.evaluate('window.coreConfirmedWrites') == 1
        page.evaluate('window.coreFailReadback = false')
        if action == 'delete':
            # Deletion already switched to global, but its template readback is
            # still required before another template can be created.
            page.evaluate("""() => {
              const api = window.pywebview.api, read = api.get_robot_core;
              window.coreOriginalRead = read;
              api.get_robot_core = async (...args) => {
                const result = await read(...args); result.data.engine_online = false; return result;
              };
            }""")
            page.locator('#refreshCore').click()
            page.wait_for_function('!window.coreDraftController.busy')
            expect(page.locator('#saveCore')).to_be_disabled()
            page.evaluate('() => { window.pywebview.api.get_robot_core = window.coreOriginalRead; }')
        elif action == 'global':
            # An unrelated template-list outage must not prevent recovering a
            # confirmed global write after the core detail itself is available.
            page.evaluate("""() => {
              const api = window.pywebview.api, request = api.request_robot_core;
              api.request_robot_core = (...args) => args[0] === 'GET' && args[1] === 'profiles'
                ? {ok:false,error:{message:'模板接口暂不可用',recorded:true}} : request(...args);
              window.coreOriginalRequest = request;
            }""")
        page.locator('#refreshCore').click()
        page.wait_for_function('!window.coreDraftController.busy')
        expect(page.locator('#saveCore')).to_be_enabled()
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        assert page.evaluate('window.coreConfirmedWrites') == 1
        if action == 'global':
            page.evaluate('() => { window.pywebview.api.request_robot_core = window.coreOriginalRequest; }')
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
        assert page.evaluate('document.documentElement.scrollWidth <= innerWidth')
        assert not errors, errors
        page.locator('#coreNote').scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f'core-{action}-read-recovered.png'), animations='disabled')
    finally:
        page.close()


def test_template_removed_by_another_window_during_write_readback_returns_to_global(browser, manager_url):
    page = _open_core_draft_page(browser, manager_url)
    try:
        page.evaluate("""() => {
          const api = window.pywebview.api, request = api.request_robot_core;
          window.createdCoreTemplate = '';
          api.request_robot_core = async (...args) => {
            const result = await request(...args);
            if (args[1] === 'profiles/create') {
              window.createdCoreTemplate = result.data.id;
              // Another window deletes the just-created item before readback.
              await request('POST', 'profiles/delete', {id:result.data.id});
            }
            return result;
          };
        }""")
        page.locator('.template-create > summary').click()
        page.locator('#newCoreTemplate').click()
        page.locator('#modalConfirm').click()
        page.wait_for_function('window.createdCoreTemplate && !window.coreDraftController.busy')
        expect(page.locator('#modalBackdrop')).to_be_hidden()
        expect(page.locator('#coreDetail h2')).to_have_text('机器人配置')
        expect(page.locator('#coreNote')).to_contain_text('该模板已不在当前列表中')
        page.locator('#coreForm [name="robot_name"]').fill('回到全局后仍可保存')
        page.locator('#saveCore').click()
        page.wait_for_function('!window.coreDraftController.busy')
        expect(page.locator('#coreNote')).to_contain_text('已更新')
    finally:
        page.close()
