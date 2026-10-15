"""Verify date cleanup through the delivered UI and real account-scoped SQLite API."""
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect

from test_app_shell import _api
from test_chat_history import _dated_history
from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("cutoff,remaining", [("2026-10-12", 2), ("2026-10-10", 3)])
def test_date_cleanup_refreshes_retained_messages_from_settings(
    browser, manager_url, tmp_path: Path, cutoff: str, remaining: int,
):
    account = "wxid_demo_account"
    api = _api(tmp_path)
    api._history = _dated_history(tmp_path, account)
    api._history_account_id = lambda: account
    api._groups = SimpleNamespace(
        list=lambda *_args: {"account_id": account, "items": []},
        members=lambda *_args: {"account_id": account, "items": []},
    )
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.expose_function("cleanupHistoryApi", lambda method, args: getattr(api, method)(*args))
    # The cleanup itself must refresh the view; a later poll must not hide the bug.
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => callback.name === 'pollHistory'
        ? 0 : schedule(callback, ...args);
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          for (const method of ['get_conversations', 'get_messages', 'delete_history_before']) {
            window.pywebview.api[method] = (...args) => window.cleanupHistoryApi(method, args);
          }
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList .message-bubble")).to_have_count(3)
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="system"]').click()
        page.locator("#cleanHistory").click()
        page.locator("#modalInput").fill(cutoff)
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        assert api._history.conversations(account_id=account)["summary"]["messages"] == remaining
        # Assert while still on settings so navigation/focus refresh cannot mask stale state.
        expect(page.locator("#messageList .message-bubble")).to_have_count(remaining)
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#messageList")).to_contain_text("保留日期零点的消息")
        expect(page.locator("#messageList")).to_contain_text("保留后续日期的消息")
        expect(page.locator("#historySummary")).to_contain_text(f"{remaining} 条消息")
        if remaining == 2:
            expect(page.locator("#messageList")).not_to_contain_text("清理日期之前的消息")
        assert page.url == manager_url
        assert "消息中心" in page.title()
        assert not errors, errors
        page.screenshot(path=str(tmp_path / "date-cleanup-retained-messages.png"), animations="disabled")
    finally:
        page.close()
