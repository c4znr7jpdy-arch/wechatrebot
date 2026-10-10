"""Exercise startup through the delivered asset routes without starting a robot."""
from http.server import ThreadingHTTPServer
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest
from playwright.sync_api import expect, sync_playwright

from wechatrebot_manager.app_shell import _asset_root
from wechatrebot_manager.plugin_center import _handler


@pytest.fixture(scope="module")
def manager_url(tmp_path_factory):
    service = SimpleNamespace(install_root=tmp_path_factory.mktemp("manager-install"))
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(service, manager_assets=_asset_root()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        # The optional QR preview fixture is not part of the installed assets.
        yield f"http://127.0.0.1:{server.server_port}/?preview=1&contact=unavailable"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch()
        yield instance
        instance.close()


@pytest.mark.parametrize("viewport", [{"width": 1380, "height": 860}, {"width": 1080, "height": 680}])
def test_manager_loads_all_resources_and_opens_subscription(browser, manager_url, viewport, tmp_path: Path):
    page = browser.new_page(viewport=viewport)
    errors, failed_responses = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failed_responses.append(response.url) if response.status >= 400 else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        expect(page.locator("#stateTitle")).not_to_have_text("正在检查环境")
        expect(page.locator("#globalRefresh")).to_be_enabled()
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="subscription"]').click()
        expect(page.locator("#page-subscription")).to_be_visible()
        page.screenshot(path=str(tmp_path / "subscription.png"))
        page.locator('.nav-item[data-page="activity"]').click()
        expect(page.locator("#page-activity")).to_be_visible()
        expect(page.locator("#activityList .diagnostic-card").first).to_be_visible()
        expect(page.locator("#activityList .diagnostic-card.info")).to_have_count(0)
        page.locator("#activityLevel").select_option("warning")
        expect(page.locator("#activityList .diagnostic-card.warning").first).to_be_visible()
        expect(page.locator("#activityList .diagnostic-card.error")).to_have_count(0)
        page.locator("#activityList details").first.locator("summary").click()
        expect(page.locator("#activityList .diagnostic-report").first).to_be_visible()
        page.screenshot(path=str(tmp_path / "activity.png"))
        page.locator('.nav-item[data-page="home"]').click()
        expect(page.locator("#page-home")).to_be_visible()
        page.locator("#globalRefresh").click()
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        page.screenshot(path=str(tmp_path / "home.png"))
        assert not failed_responses, failed_responses
        assert not errors, errors
        assert page.url == manager_url
        assert page.title()
    finally:
        page.close()


@pytest.mark.parametrize("missing_script", ["subscription.js", "core-settings.js", "chat-profiles.js", "panel-layout.js", "features.js", "scheduled-tasks.js", "preview.js"])
def test_manager_reports_missing_startup_dependency(browser, manager_url, missing_script, tmp_path: Path):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route(f"**/{missing_script}*", lambda route: route.fulfill(status=404, body=""))
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("软件未能完成初始化")
        expect(page.locator("#stateMessage")).to_contain_text("重新打开软件")
        expect(page.locator("#globalStatus")).to_have_text("初始化失败")
        expect(page.locator("#sideHealthText")).to_have_text("初始化失败")
        expect(page.locator("#primaryAction")).to_be_disabled()
        assert not errors, errors
        page.screenshot(path=str(tmp_path / "startup-error.png"))
    finally:
        page.close()
