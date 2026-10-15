"""Exercise delivered diagnostic UI state and error-capture behaviour."""
from pathlib import Path
import shutil
import subprocess

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def test_activity_ui_behaviour():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for Web UI behaviour checks")
    result = subprocess.run(
        [node, str(Path(__file__).with_name("activity_webui.cjs"))],
        capture_output=True, text=True, encoding="utf-8", timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "ACTIVITY_WEBUI_COMPLETE" in result.stdout


def test_activity_dropdowns_filter_list_and_summary(browser, manager_url, tmp_path: Path):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        expect(page.locator('.nav-item[data-page="home"]')).to_be_enabled()
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""() => {
          const now = Date.now();
          const entries = [
            ['近期管理器错误', 'error', 'manager', '管理器', 1],
            ['较早管理器错误', 'error', 'manager', '管理器', 30],
            ['近期引擎警告', 'warning', 'engine-runtime', '回复引擎', 2],
            ['近期引擎提示', 'info', 'engine-runtime', '回复引擎', 3],
          ].map(([title, level, source, source_name, hours], index) => ({
            id: `filter-${index}`, title, level, source, source_name,
            time: new Date(now - hours * 3600000).toISOString(),
            timestamp: now / 1000 - hours * 3600,
            code: '', message: title, report: title, count: 1,
          }));
          window.pywebview.api.get_activity = async () => ({ok: true, data: entries});
        }""")
        page.locator('.nav-item[data-page="activity"]').click()
        cards = page.locator("#activityList .diagnostic-card")
        summary = page.locator("#activitySummary")
        expect(cards).to_have_count(3)
        expect(summary).to_contain_text("最近 48 小时）：3 条记录 · 2 条错误 · 1 条警告")
        page.locator("#activityRange").select_option("24")
        expect(cards).to_have_count(2)
        expect(page.locator("#activityList")).not_to_contain_text("较早管理器错误")
        expect(summary).to_contain_text("最近 24 小时）：2 条记录 · 1 条错误 · 1 条警告")
        page.locator("#activityLevel").select_option("warning")
        page.locator("#activitySource").select_option("manager")
        expect(cards).to_have_count(0)
        expect(summary).to_contain_text("0 条记录 · 0 条错误 · 0 条警告")
        expect(page.locator("#copyLatestActivity")).to_be_disabled()
        page.locator("#activitySource").select_option("engine-runtime")
        expect(cards).to_have_count(1)
        expect(cards).to_contain_text("近期引擎警告")
        expect(summary).to_contain_text("最近 24 小时）：1 条记录 · 0 条错误 · 1 条警告")
        page.locator("#refreshActivity").click()
        expect(page.locator("#refreshActivity")).to_be_enabled()
        expect(page.locator("#activityRange")).to_have_value("24")
        expect(page.locator("#activityLevel")).to_have_value("warning")
        expect(page.locator("#activitySource")).to_have_value("engine-runtime")
        expect(cards).to_have_count(1)
        page.screenshot(path=str(tmp_path / "activity-filtered.png"), animations="disabled")
        page.locator("#activityLevel").select_option("all")
        page.locator("#activitySource").select_option("")
        page.locator("#activityRange").select_option("0")
        expect(cards).to_have_count(4)
        expect(summary).to_contain_text("最近 48 小时）：4 条记录 · 2 条错误 · 1 条警告")
        assert page.url == manager_url
        assert page.title().startswith("运行记录")
        assert not errors, errors
        print(f"Activity filter UI evidence: {tmp_path}")
    finally:
        page.close()
