"""Read-only contact lookup through delivered UI assets; no running robot."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def open_manager(page, manager_url, *, skin="default", theme="light"):
    page.goto(manager_url + f"&style={skin}")
    expect(page).to_have_title(re.compile("机器人管理$"))
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    if page.locator("html").get_attribute("data-theme") != theme:
        page.locator("#themeToggle").click()
    page.evaluate("""() => {
      window.profileCalls = [];
      const original = window.pywebview.api.get_contact_network_profile;
      window.pywebview.api.get_contact_network_profile = async (...args) => {
        window.profileCalls.push(args); return original(...args);
      };
    }""")


def open_private_management(page):
    page.locator('.nav-item[data-page="groups"]').click()
    page.locator("#privateManagementTab").click()
    page.locator('.contact-row[data-contact-id="wxid_zhangsan"]').click()
    button = page.locator("#contactInspector [data-contact-profile]")
    expect(button).to_be_visible()
    return button


def open_private_history(page):
    page.locator('.nav-item[data-page="history"]').click()
    expect(page.locator("#conversationInspector [data-contact-profile]")).to_be_visible()
    page.get_by_role("button", name="个人聊天", exact=True).click()
    button = page.locator("#conversationInspector [data-contact-profile]")
    expect(button).to_be_visible()
    return button


@pytest.mark.parametrize("skin,theme,width", [
    ("default", "light", 1440), ("default", "dark", 1440),
    ("soft", "light", 1440), ("soft", "dark", 600),
])
def test_private_entries_share_profile_dialog_and_styles(browser, manager_url, tmp_path, skin, theme, width):
    page = browser.new_page(viewport={"width": width, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        open_manager(page, manager_url, skin=skin, theme=theme)
        entry = open_private_management(page)
        buttons = page.locator(".contact-inspector-actions button")
        expect(buttons).to_have_text(["查询好友资料", "该联系人的定时任务"])
        assert buttons.evaluate_all("""items => {
          const a = getComputedStyle(items[0]), b = getComputedStyle(items[1]);
          return ['color','fontSize','fontWeight','borderRadius'].every(key => a[key] === b[key]);
        }""")
        entry.click()
        dialog = page.get_by_role("dialog", name="好友资料", exact=True)
        expect(dialog).to_be_visible()
        expect(dialog.locator(".contact-facts")).to_contain_text("山川与海")
        expect(dialog.locator(".contact-facts")).to_contain_text("周末去爬山")
        assert page.evaluate("window.profileCalls") == [["wxid_demo_account", "wxid_zhangsan"]]
        assert dialog.evaluate("e => e.scrollWidth <= e.clientWidth")
        assert dialog.evaluate("e => getComputedStyle(e).backgroundColor") != "rgba(0, 0, 0, 0)"
        page.screenshot(path=str(tmp_path / f"profile-{skin}-{theme}-{width}.png"))
        dialog.get_by_role("button", name="关闭好友资料").click()
        expect(entry).to_be_focused()

        if width == 600:
            # Existing compact layout hides the whole history inspector.
            page.set_viewport_size({"width": 1440, "height": 1000})
        history_entry = open_private_history(page)
        copy = page.locator("#copySenderId")
        # Read both controls from one inspector snapshot. History polling can
        # replace the inspector between two locator evaluations, which compares
        # controls from different render generations on slower Windows runners.
        border_radii = page.locator("#conversationInspector").evaluate("""root => {
          const entry = root.querySelector('[data-contact-profile]');
          const copy = root.querySelector('#copySenderId');
          return [getComputedStyle(entry).borderRadius, getComputedStyle(copy).borderRadius];
        }""")
        assert border_radii[0] == border_radii[1]
        history_entry.click()
        expect(dialog).to_be_visible()
        expect(page.locator(".contact-profile-dialog")).to_have_count(1)
        expect(dialog).to_contain_text("zhangsan_88")
        assert page.evaluate("window.profileCalls") == [["wxid_demo_account", "wxid_zhangsan"]] * 2
        page.keyboard.press("Escape")
        expect(dialog).not_to_be_visible()
        expect(history_entry).to_be_focused()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
        print(f"Contact profile screenshot: {tmp_path}")
    finally:
        page.close()


def test_profile_pending_close_reopen_discards_old_response_and_escapes_content(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    try:
        open_manager(page, manager_url)
        entry = open_private_management(page)
        page.evaluate("""() => {
          window.pendingProfiles = [];
          window.pywebview.api.get_contact_network_profile = (account, user) => new Promise(resolve => {
            window.pendingProfiles.push(value => resolve({ok:true, data:{account_id:account,
              item:{user_id:user, nickname:value, signature:'<script>window.injected=1</script>'}}}));
          });
        }""")
        entry.click()
        entry.evaluate("e => {e.click(); e.click();}")
        dialog = page.get_by_role("dialog", name="好友资料", exact=True)
        expect(dialog).to_contain_text("正在查询好友资料")
        assert page.evaluate("window.pendingProfiles.length") == 1
        page.evaluate("""() => {
          document.querySelector('.contact-profile-dialog [data-close]').click();
          const other = document.querySelector('.contact-row[data-contact-id="wxid_lisi"]');
          other.click();
          document.querySelector('#contactInspector [data-contact-profile]').click();
        }""")
        expect(dialog).to_contain_text("正在查询好友资料")
        assert page.evaluate("window.pendingProfiles.length") == 2
        page.evaluate("window.pendingProfiles[0]('STALE_PROFILE')")
        expect(dialog).not_to_contain_text("STALE_PROFILE")
        expect(dialog).to_contain_text("正在查询好友资料")
        page.evaluate("window.pendingProfiles[1]('<img src=x onerror=window.injected=1>')")
        expect(dialog).to_contain_text("<img src=x onerror=window.injected=1>")
        expect(dialog).to_contain_text("<script>window.injected=1</script>")
        assert page.evaluate("window.injected") is None
        expect(dialog).to_contain_text("wxid_lisi")
    finally:
        page.close()


@pytest.mark.parametrize("account_state", ["changed", "unknown"])
def test_profile_failure_retries_and_account_change_closes_pending_dialog(browser, manager_url, account_state):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    try:
        open_manager(page, manager_url)
        entry = open_private_management(page)
        page.evaluate("""() => {
          window.pywebview.api.get_contact_network_profile = async () => ({ok:false,
            error:{code:'19000',title:'资料暂不可用',message:'查询暂时失败',recorded:true}});
        }""")
        entry.click()
        dialog = page.get_by_role("dialog", name="好友资料", exact=True)
        expect(dialog.get_by_role("alert")).to_contain_text("查询暂时失败")
        page.evaluate("""() => {
          window.pywebview.api.get_contact_network_profile = (account, user) => new Promise(resolve => {
            window.resolveProfile = () => resolve({ok:true,data:{account_id:account,item:{user_id:user,nickname:'OLD_ACCOUNT_DATA'}}});
          });
        }""")
        dialog.get_by_role("button", name="重新查询").click()
        expect(dialog).to_contain_text("正在查询好友资料")
        page.evaluate("""state => {
          const original = window.pywebview.api.get_status;
          window.pywebview.api.get_status = async () => {
            const result = await original();
            if (state === 'changed') result.data.runtime.wechat_identity = 'wxid_other';
            else result.data.runtime.account_status_available = false;
            return result;
          };
        }""", account_state)
        expect(dialog).not_to_be_visible()
        page.evaluate("window.resolveProfile()")
        expect(page.locator(".contact-profile-dialog")).not_to_contain_text("OLD_ACCOUNT_DATA")
        expect(page.locator(".contact-profile-content")).to_be_empty()
    finally:
        page.close()


def test_group_lookup_uses_selected_sender_identifier_and_copy_keeps_raw_id(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    try:
        open_manager(page, manager_url)
        page.evaluate("""() => {
          Object.defineProperty(navigator, 'clipboard', {configurable:true, value:{
            writeText: async value => {window.copiedContact = value;}
          }});
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        for user, label in [("wxid_zhangsan", "张三｜3栋"), ("wxid_lisi", "李四｜摄影")]:
            page.get_by_role("button", name=f"选中消息发送者：{label}", exact=True).click()
            entry = page.locator("#conversationInspector [data-contact-profile]")
            expect(entry).to_have_attribute("data-contact-profile", user)
            entry.click()
            dialog = page.get_by_role("dialog", name="好友资料", exact=True)
            expect(dialog).to_contain_text(user)
            dialog.get_by_role("button", name="复制微信标识", exact=True).click()
            assert page.evaluate("window.copiedContact") == user
            dialog.get_by_role("button", name="关闭好友资料").click()
        assert page.evaluate("window.profileCalls") == [["wxid_demo_account", "wxid_zhangsan"], ["wxid_demo_account", "wxid_lisi"]]
    finally:
        page.close()
