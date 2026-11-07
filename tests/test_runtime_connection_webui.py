"""Connection uncertainty and core exits stay distinct in the manager shell."""

from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def test_message_center_navigation_keeps_connection_and_exit_feedback(browser, manager_url, tmp_path):
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
          const api = window.pywebview.api, read = api.get_status;
          window.connectionPhase = 'online';
          api.get_status = async () => {
            const result = await read(), status = result.data;
            const unknown = window.connectionPhase === 'unknown';
            const exited = window.connectionPhase === 'exited';
            status.online = !unknown && !exited;
            status.state = unknown ? 'VERIFYING_CONNECTION' : exited ? 'ERROR' : 'RUNNING';
            status.title = unknown ? '正在确认连接' : exited ? '机器人服务意外退出' : '机器人可用';
            status.message = exited
              ? '机器人核心服务已停止，微信连接仍保留；点击启动可重新运行机器人。' : '';
            status.error = exited ? {code:'13004', title:status.title, message:status.message} : null;
            status.can_start = exited;
            status.can_stop = true;
            status.runtime = {...status.runtime, engine_ready:!exited, connection_ready:true,
              account_status_available:!unknown, hook_ready:!unknown,
              upstream_connected:!exited, heartbeat_fresh:!unknown && !exited,
              wechat_identity:unknown ? '' : 'wxid_demo_account'};
            return result;
          };
        }""")
        page.locator('.nav-item[data-page="history"]').click()
        expect(page.locator("#page-history")).to_be_visible()
        page.evaluate("window.connectionPhase = 'unknown'")
        expect(page.locator("#sideHealthText")).to_have_text("正在确认连接")
        page.locator('.nav-item[data-page="home"]').click()
        expect(page.locator("#stateTitle")).to_have_text("正在确认连接")
        expect(page.locator("#stateMessage")).to_have_text("微信连接状态暂时无法确认，正在重新检测。")
        expect(page.locator("#accountText")).to_have_text("正在确认微信连接…")
        expect(page.locator("#globalStatus")).to_contain_text("正在确认连接")
        page.screenshot(path=str(tmp_path / "connection-verifying.png"), animations="disabled")

        page.evaluate("window.connectionPhase = 'online'")
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        expect(page.locator("#sideHealthText")).to_have_text("机器人在线")
        page.evaluate("window.connectionPhase = 'exited'")
        expect(page.locator("#stateTitle")).to_have_text("机器人服务意外退出")
        expect(page.locator("#stateMessage")).to_contain_text("微信连接仍保留")
        expect(page.locator("#accountText")).to_have_text("已登录：wxid_demo_account")
        expect(page.locator("#sideHealthText")).to_have_text("需要处理")
        page.screenshot(path=str(tmp_path / "core-exited.png"), animations="disabled")
        assert page.url == manager_url and page.title()
        assert not errors, errors
    finally:
        page.close()
