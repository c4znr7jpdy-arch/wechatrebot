"""Verify retained chat configuration views using synthetic preview data only."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("resume", ["focus", "navigation"])
def test_chat_configuration_keeps_paging_and_unsaved_form(browser, manager_url, tmp_path, resume):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api, groups = api.get_groups;
          window.directoryReads = {groups: 0, contacts: 0};
          api.get_groups = async (...args) => {
            window.directoryReads.groups += 1;
            return groups(...args);
          };
          api.get_contacts = async () => {
            window.directoryReads.contacts += 1;
            if (window.deferContactRefresh) await new Promise(resolve => { window.finishContactRefresh = resolve; });
            return {ok: true, data: {account_id: 'wxid_demo_account', updated_at: '2026-10-29T00:00:00Z',
              items: Array.from({length: 205}, (_, i) => ({user_id: `cache_person_${i}`, name: `缓存联系人 ${i}`}))}};
          };
        }""")
        page.locator('.nav-item[data-page="groups"]').click()
        expect(page.locator("#groupRows .group-row").first).to_be_visible()
        page.wait_for_function("() => !document.querySelector('#refreshGroups').disabled")
        page.evaluate("window.retainedGroupRow = document.querySelector('#groupRows').firstElementChild")
        before_groups = page.evaluate("window.directoryReads.groups")
        page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("count => window.directoryReads.groups > count", arg=before_groups)
        assert page.evaluate("window.retainedGroupRow === document.querySelector('#groupRows').firstElementChild")

        page.locator("#privateManagementTab").click()
        expect(page.locator("#contactRows .contact-row")).to_have_count(100)
        page.locator("#contactSearch").fill("缓存")
        expect(page.locator("#contactCount")).to_have_text("205 / 205 位联系人")
        page.locator("#contactNext").click()
        expect(page.locator("#contactRange")).to_contain_text("第 101–200 项")
        page.locator('#contactRows .contact-row[data-contact-id="cache_person_100"]').click()
        expect(page.locator("#privatePolicyForm")).to_be_visible()
        expect(page.locator('button[form="privatePolicyForm"]')).to_be_enabled()
        page.locator("#privatePolicyForm details").first.locator("summary").click()
        page.locator('#privatePolicyForm input[name="daily_limit"]').fill("17")
        page.evaluate("""() => {
          document.querySelector('.contact-table-scroll').scrollTop = 180;
          window.retainedContactScroll = document.querySelector('.contact-table-scroll').scrollTop;
          window.retainedPrivateForm = document.querySelector('#privatePolicyForm');
          window.deferContactRefresh = true;
        }""")
        if resume == "navigation":
            page.locator('.nav-item[data-page="home"]').click()
            page.locator('.nav-item[data-page="groups"]').click()
        else:
            page.evaluate("window.dispatchEvent(new Event('focus'))")
        page.wait_for_function("() => typeof window.finishContactRefresh === 'function'")
        assert page.evaluate("window.retainedPrivateForm === document.querySelector('#privatePolicyForm')")
        page.evaluate("window.deferContactRefresh = false; window.finishContactRefresh()")
        expect(page.locator("#contactRange")).to_contain_text("第 101–200 项")
        expect(page.locator("#contactSearch")).to_have_value("缓存")
        expect(page.locator('#privatePolicyForm input[name="daily_limit"]')).to_have_value("17")
        expect(page.locator("#privatePolicyForm details").first).to_have_attribute("open", "")
        assert page.evaluate("window.retainedPrivateForm === document.querySelector('#privatePolicyForm')")
        assert page.evaluate("window.retainedContactScroll === document.querySelector('.contact-table-scroll').scrollTop")
        expect(page.locator('#contactRows .contact-row.selected')).to_have_attribute("data-contact-id", "cache_person_100")
        page.screenshot(path=str(tmp_path / f"chat-configuration-retained-{resume}.png"), animations="disabled")
        assert page.url == manager_url
        assert "群聊&私聊配置" in page.title()
        assert not errors, errors
    finally:
        page.close()
