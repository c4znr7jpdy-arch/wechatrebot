"""Retained main tabs stay visible while synthetic preview reads are pending."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("skin", ["default", "soft"])
@pytest.mark.parametrize("tab,content,method,refresh", [
    ("history", "#messageList .message", "get_conversations", "#refreshHistory"),
    ("schedules", "#scheduledTaskRows tr:has([data-task-action])", "get_scheduled_task_context", "#refreshSchedules"),
    ("activity", "#activityList .diagnostic-card", "get_activity", "#refreshActivity"),
])
def test_returning_tabs_do_not_reload_or_flash_and_failed_refresh_keeps_content(
    browser, manager_url, tmp_path, skin, tab, content, method, refresh,
):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const schedule = window.setInterval;
      window.setInterval = (callback, ...args) => ['refreshStatus', 'pollHistory'].includes(callback.name)
        ? 0 : schedule(callback, ...args);
    })();""")
    try:
        url = manager_url + ("&style=soft" if skin == "soft" else "")
        page.goto(url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""method => {
          const api = window.pywebview.api, original = api[method];
          window.tabDataReads = 0;
          api[method] = async (...args) => {
            window.tabDataReads++;
            const result = await original(...args);
            if (method === 'get_scheduled_task_context') result.data.items = [{
              id: 'retained-preview-task', name: '保留中的定时任务', action: 'text',
              account_id: 'wxid_demo_account', enabled: true, status: 'enabled',
              schedule: {kind: 'daily', time: '08:00', timezone: 'Asia/Shanghai'},
              targets: [{type: 'group', id: 'preview@chatroom', name: '预览群聊'}],
            }];
            return result;
          };
        }""", method)
        page.locator(f'.nav-item[data-page="{tab}"]').click()
        expect(page.locator(content).first).to_be_visible()
        expect(page.locator(refresh)).to_be_enabled()
        before = page.evaluate("""content => {
          window.retainedTabNode = document.querySelector(content);
          window.retainedTabMarkup = window.retainedTabNode.parentElement.innerHTML;
          return window.tabDataReads;
        }""", content)
        page.locator('.nav-item[data-page="home"]').click()
        # Sample synchronously inside the click task: Playwright's visible check
        # would otherwise wait through the old 200 ms opacity-from-zero animation.
        first_frame = page.evaluate("""tab => {
          document.querySelector(`.nav-item[data-page="${tab}"]`).click();
          const panel = document.querySelector(`#page-${tab}`);
          for (const animation of panel.getAnimations()) {
            animation.pause(); animation.currentTime = 0;
          }
          return {opacity: getComputedStyle(panel).opacity, text: panel.innerText};
        }""", tab)
        assert first_frame["opacity"] == "1", first_frame
        assert first_frame["text"].strip()
        page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
        assert page.evaluate("window.tabDataReads") == before
        assert page.evaluate("content => document.querySelector(content) === window.retainedTabNode", content)
        if tab == "activity" and skin == "default":
            for _ in range(3):
                page.locator('.nav-item[data-page="home"]').click()
                page.locator('.nav-item[data-page="activity"]').click()
                expect(page.locator(content).first).to_be_visible()
                assert page.evaluate("window.tabDataReads") == before
                assert page.evaluate("content => document.querySelector(content) === window.retainedTabNode", content)

        page.evaluate("""method => {
          const api = window.pywebview.api, original = api[method];
          window.pendingTabReads = 0;
          api[method] = (...args) => {
            // The old schedule loader did a cache-only read before its live read.
            if (args[0] === true) return original(...args);
            window.pendingTabReads++;
            return new Promise(resolve => { window.finishTabRefresh = resolve; });
          };
        }""", method)
        page.locator(refresh).click()
        page.wait_for_function("typeof window.finishTabRefresh === 'function'")
        assert page.evaluate("content => document.querySelector(content) === window.retainedTabNode", content)
        assert page.evaluate("window.retainedTabNode.parentElement.innerHTML === window.retainedTabMarkup")
        expect(page.locator(content).first).to_be_visible()
        page.locator('.nav-item[data-page="home"]').click()
        page.locator(f'.nav-item[data-page="{tab}"]').click()
        expect(page.locator(content).first).to_be_visible()
        assert page.evaluate("content => document.querySelector(content) === window.retainedTabNode", content)
        assert page.evaluate("window.pendingTabReads") == 1
        page.evaluate("window.finishTabRefresh({ok: false, error: '预览读取失败，请稍后重试'})")
        expect(page.locator(refresh)).to_be_enabled()
        assert page.evaluate("content => document.querySelector(content) === window.retainedTabNode", content)
        expect(page.locator(content).first).to_be_visible()
        page.locator('.nav-item[data-page="home"]').click()
        page.locator(f'.nav-item[data-page="{tab}"]').click()
        expect(page.locator(content).first).to_be_visible()
        assert page.evaluate("content => document.querySelector(content) === window.retainedTabNode", content)
        assert page.evaluate("window.pendingTabReads") == 1
        page.screenshot(path=str(tmp_path / f"retained-{tab}-{skin}.png"))
        assert page.url == manager_url  # The skin loader consumes its query option.
        assert page.title().endswith("· WxRebot")
        assert not errors, errors
        print(f"Tab retention evidence ({tab}, {skin}): {tmp_path}")
    finally:
        page.close()
