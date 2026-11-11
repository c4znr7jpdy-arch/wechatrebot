"""Multiple configured administrators survive identity edits and readback."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_settings_webui import _open_core_draft_page


@pytest.mark.parametrize("width", [1380, 600])
def test_multiple_administrators_are_saved_as_deduplicated_ids(browser, manager_url, tmp_path, width):
    page = _open_core_draft_page(browser, manager_url)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.set_viewport_size({"width": width, "height": 920})
        admins = page.locator('#coreForm textarea[name="admin_ids"]')
        expect(admins).to_have_value("wxid_admin")
        expect(page.locator("#adminPluginControlHint")).to_contain_text("只调整当前群")
        page.evaluate("""() => {
          const api = window.pywebview.api, save = api.save_robot_core;
          window.adminIdentityWrites = [];
          api.save_robot_core = (...args) => {
            window.adminIdentityWrites.push(structuredClone(args[2]));
            return save(...args);
          };
        }""")
        admins.fill("wxid_admin\n wxid_second \n\nwxid_admin")
        page.locator('#coreForm [name="robot_name"]').fill("多管理员机器人")
        page.locator("#saveCore").click()
        page.wait_for_function("window.adminIdentityWrites.length === 1 && !window.coreDraftController.busy")
        identity = page.evaluate("window.adminIdentityWrites[0]")
        assert identity["admin_ids"] == ["wxid_admin", "wxid_second"]
        assert identity["admin_id"] == "wxid_admin"
        expect(admins).to_have_value("wxid_admin\nwxid_second")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        admins.evaluate("element => element.scrollIntoView({block: 'center'})")
        page.screenshot(path=str(tmp_path / f"multiple-administrators-{width}.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()
