"""Reject invalid chat limits before a policy request without losing the draft."""

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def open_policy(page, manager_url, kind):
    page.goto(manager_url)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.evaluate("""() => {
      window.limitCalls = [];
      const api = window.pywebview.api;
      for (const name of ['preview_group_policy', 'save_group_policy', 'save_private_policy']) {
        const original = api[name];
        api[name] = async (...args) => {
          window.limitCalls.push({name, value:args[2].daily_limit});
          return original(...args);
        };
      }
    }""")
    page.locator('.nav-item[data-page="groups"]').click()
    if kind == "group":
        page.locator("#groupRows .group-row").first.click()
    else:
        page.locator("#privateManagementTab").click()
        page.locator("#contactRows .contact-row").first.click()
    form = page.locator(f"#{kind}PolicyForm")
    expect(form).to_be_visible()
    return form, form.locator('[name="daily_limit"]'), page.locator(f'button[form="{kind}PolicyForm"]')


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("value", ["", "1.5", "-1", "101"])
def test_invalid_daily_limit_reveals_field_and_never_calls_api(browser, manager_url, kind, value):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        form, limit, save = open_policy(page, manager_url, kind)
        details = limit.locator("xpath=ancestor::details[1]")
        details.locator("summary").click()
        limit.fill(value)
        details.locator("summary").click()
        save.click()
        assert page.evaluate("window.limitCalls") == []
        expect(details).to_have_attribute("open", "")
        expect(limit).to_be_focused()
        expect(limit).to_have_value(value)
        assert limit.evaluate("input => !input.validity.valid")
        expect(save).to_be_enabled()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("kind", ["group", "private"])
@pytest.mark.parametrize("value", ["0", "100"])
def test_valid_daily_limit_boundaries_can_save(browser, manager_url, kind, value):
    page = browser.new_page()
    try:
        form, limit, save = open_policy(page, manager_url, kind)
        limit.locator("xpath=ancestor::details[1]").locator("summary").click()
        limit.fill(value)
        save.click()
        if kind == "group":
            expect(page.locator("#modalBackdrop")).to_be_visible()
            page.locator("#modalConfirm").click()
            expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(save).to_be_enabled()
        expect(limit).to_have_value(value)
        calls = page.evaluate("window.limitCalls")
        assert calls == ([{"name":"preview_group_policy", "value":int(value)},
                          {"name":"save_group_policy", "value":int(value)}] if kind == "group" else
                         [{"name":"save_private_policy", "value":int(value)}])
    finally:
        page.close()
