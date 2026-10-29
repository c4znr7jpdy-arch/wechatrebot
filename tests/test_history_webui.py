"""Run message-center state regressions against the delivered JavaScript."""
from pathlib import Path
import shutil
import subprocess

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def test_message_center_date_cache_and_paging_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    script = Path(__file__).with_name("history_webui.cjs")
    result = subprocess.run([node, str(script)], capture_output=True, text=True,
                            encoding="utf-8", timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "history date, cache, pagination, refresh and account isolation: passed" in result.stdout


@pytest.mark.parametrize("resume", ["focus", "visibilitychange", "navigation"])
def test_long_absence_keeps_visible_history_until_latest_is_requested(
    browser, manager_url, tmp_path, resume,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => callback.name === 'pollHistory'
        ? 0 : schedule(callback, ...args);
    })();""")
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const api = window.pywebview.api;
          window.testHistoryLatest = 40;
          api.get_conversations = async () => ({ok:true, data:{account_id:'wxid_demo_account',
            items:[{conversation_id:'retained-chat', display_name:'缓存保留测试', type:'private',
              message_count:window.testHistoryLatest, last_timestamp:window.testHistoryLatest}], summary:{}}});
          api.get_messages = async () => {
            if (window.testHistoryLatest > 40 && !window.testHistoryResolved) {
              await new Promise(resolve => { window.resolveHistoryRefresh = () => {
                window.testHistoryResolved = true; resolve();
              }; });
            }
            const last = window.testHistoryLatest;
            return {ok:true, data:{account_id:'wxid_demo_account', has_more:true,
              items:Array.from({length:20}, (_, i) => ({id:last-19+i, timestamp:1793200000+last-19+i,
                content:`测试记录 ${last-19+i}`, role:'user', sender_id:'member', sender_name:'测试联系人'}))}};
          };
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        bubbles = page.locator("#messageList .message-bubble")
        expect(bubbles).to_have_count(20)
        page.locator("#messageList .message-avatar-button").last.click()
        page.evaluate("""() => {
          const list = document.querySelector('#messageList');
          list.scrollTop = list.scrollHeight;
          window.retainedMessageNode = list.firstElementChild;
          window.retainedMessageHtml = list.innerHTML;
          window.retainedMessageScroll = list.scrollTop;
        }""")
        if resume == "navigation":
            page.locator('.nav-item[data-page="home"]').click()
        page.evaluate("window.testHistoryLatest = 500")
        if resume == "navigation":
            page.locator('.nav-item[data-page="history"]').click()
        else:
            page.evaluate("event => (event === 'focus' ? window : document).dispatchEvent(new Event(event))", resume)
        page.wait_for_function("typeof window.resolveHistoryRefresh === 'function'")
        assert page.evaluate("document.querySelector('#messageList').innerHTML === window.retainedMessageHtml")
        page.evaluate("window.resolveHistoryRefresh()")
        latest = page.locator("#newMessageButton")
        expect(latest).to_be_visible()
        expect(latest).to_contain_text("回到最新消息")
        assert page.evaluate("document.querySelector('#messageList').firstElementChild === window.retainedMessageNode")
        assert page.evaluate("document.querySelector('#messageList').scrollTop === window.retainedMessageScroll")
        expect(bubbles.first).to_have_text("测试记录 21")
        expect(page.locator("#messageList .message-avatar-button.selected").last).to_be_visible()
        page.screenshot(path=str(tmp_path / f"history-retained-{resume}.png"), animations="disabled")
        latest.click()
        expect(bubbles.first).to_have_text("测试记录 481")
        expect(bubbles).to_have_count(20)
        expect(latest).to_be_hidden()
        assert page.url == manager_url
        assert "消息中心" in page.title()
        assert not errors, errors
    finally:
        page.close()
