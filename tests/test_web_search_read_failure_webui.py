"""Failed search reads cannot turn an unknown or stale form into a write."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def _settings(page, manager_url):
    page.goto(manager_url)
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.locator('.nav-item[data-page="settings"]').click()
    page.evaluate("""() => {
      const api=window.pywebview.api, read=api.get_web_search_settings,
        save=api.save_web_search_settings;
      window.searchReadFails=true; window.searchWrites=[]; window.searchReads=0;
      api.get_web_search_settings=(...args)=>{
        window.searchReads++;
        return window.searchReadFails ? {ok:false,error:{code:'15003',
          message:'模拟搜索设置读取失败',recorded:true}} : read(...args);
      };
      api.save_web_search_settings=(values)=>{
        window.searchWrites.push(structuredClone(values));return save(values);
      };
    }""")


@pytest.mark.parametrize("width", [1380, 430])
def test_initial_search_read_failure_blocks_write_until_read_recovers(browser, manager_url, tmp_path, width):
    page=browser.new_page(viewport={"width":width,"height":860})
    try:
        _settings(page, manager_url)
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchNote")).to_contain_text("模拟搜索设置读取失败")
        save=page.locator('#webSearchForm button[type="submit"]')
        expect(save).to_be_disabled()
        expect(page.locator("#webSearchStatus")).to_have_text("设置待确认")
        expect(page.locator("#reloadWebSearch")).to_be_enabled()
        page.locator("#webSearchForm").evaluate("form=>form.dispatchEvent(new Event('submit',{cancelable:true}))")
        assert page.evaluate("window.searchWrites")==[]
        page.screenshot(path=str(tmp_path/f"search-read-failure-{width}.png"))
        page.evaluate("window.searchReadFails=false")
        page.locator("#reloadWebSearch").click()
        expect(page.locator("#webSearchProvider option")).not_to_have_count(0)
        expect(save).to_be_enabled()
        assert page.evaluate("window.searchWrites")==[]
        assert page.evaluate("document.documentElement.scrollWidth<=innerWidth")
        save.click()
        expect(page.locator("#webSearchNote")).to_contain_text("已热更新")
        assert len(page.evaluate("window.searchWrites"))==1
    finally:
        page.close()


def test_failed_reload_preserves_stale_draft_without_allowing_save_after_tab_switch(browser, manager_url):
    page=browser.new_page()
    try:
        _settings(page, manager_url)
        page.evaluate("window.searchReadFails=false")
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchProvider option")).not_to_have_count(0)
        page.evaluate("window.searchReadFails=true")
        page.locator("#reloadWebSearch").click()
        expect(page.locator("#webSearchNote")).to_contain_text("模拟搜索设置读取失败")
        save=page.locator('#webSearchForm button[type="submit"]')
        expect(save).to_be_disabled()
        page.locator('#webSearchForm [name="enabled"]').check(force=True)
        page.locator("#webSearchKey").fill("preview-kept-after-read-failure")
        page.locator('[data-settings-tab="skin"]').click()
        page.locator('[data-settings-tab="search"]').click()
        expect(page.locator("#webSearchKey")).to_have_value("preview-kept-after-read-failure")
        expect(save).to_be_disabled()
        expect(page.locator("#webSearchNote")).to_contain_text("重新读取")
        page.locator("#webSearchForm").evaluate("form=>form.dispatchEvent(new Event('submit',{cancelable:true}))")
        assert page.evaluate("window.searchWrites")==[]
        page.locator("#reloadWebSearch").click()
        expect(page.locator("#modalTitle")).to_have_text("重新读取联网搜索设置？")
        page.locator("#modalCancel").click()
        expect(save).to_be_disabled()
        expect(page.locator("#webSearchKey")).to_have_value("preview-kept-after-read-failure")
        page.evaluate("window.searchReadFails=false")
        page.locator("#reloadWebSearch").click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(save).to_be_enabled()
        expect(page.locator("#webSearchKey")).to_have_value("")
        assert page.evaluate("window.searchWrites")==[]
    finally:
        page.close()
