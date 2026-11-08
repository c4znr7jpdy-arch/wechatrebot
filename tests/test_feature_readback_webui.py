"""A failed fresh feature read stays read-only until a successful retry."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_settings_webui import _open_feature_draft_page


@pytest.mark.parametrize("mode,width", [("refresh", 1380), ("saved", 430)])
@pytest.mark.parametrize("trigger", ["tabs", "reselect", "return"])
def test_failed_feature_read_remains_readonly_until_verified(browser, manager_url, tmp_path, mode, width, trigger):
    page = _open_feature_draft_page(browser, manager_url)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.set_viewport_size({"width": width, "height": 920})
        page.evaluate("""mode => {
          const api = window.pywebview.api, read = api.get_feature, save = api.save_feature;
          window.featureReadbackWrites = 0; window.featureReadbackReads = 0;
          window.failFeatureReadback = mode === 'refresh';
          api.save_feature = async (...args) => {
            window.featureReadbackWrites++;
            const result = await save(...args);
            window.failFeatureReadback = true;
            return result;
          };
          api.get_feature = async (...args) => {
            if (args[0] !== 'feature-group-analysis') return read(...args);
            window.featureReadbackReads++;
            if (window.failFeatureReadback) return {ok:false, error:{code:'19000',
              title:'读取失败',message:'模拟最新配置读取中断',recorded:true}};
            const result = await read(...args);
            result.data.revision = 'verified-revision';
            return result;
          };
        }""", mode)
        field = page.locator("#config-basic--analysis_days")
        if mode == "saved":
            field.fill("7")
            page.locator("#saveFeature").click()
        else:
            page.locator("#refreshFeature").click()
        note = page.locator("#featureNote")
        expect(note).to_contain_text("当前仅显示上次内容")
        expect(field).to_be_disabled()
        expect(page.locator("#saveFeature")).to_be_disabled()
        reads = page.evaluate("window.featureReadbackReads")
        if trigger == "tabs":
            page.locator('.config-tabs button[data-tab="advanced"]').click()
            expect(page.locator("#featureForm .config-input").first).to_be_disabled()
            expect(note).to_contain_text("请重新读取后再修改")
            page.locator('.config-tabs button[data-tab="basic"]').click()
            assert page.evaluate("window.featureReadbackReads") == reads
        else:
            if trigger == "return":
                page.locator('.feature-item[data-feature-id="feature-commands"]').click()
                expect(page.locator("#featureDetail h2")).to_have_text("常用指令")
            page.locator('.feature-item[data-feature-id="feature-group-analysis"]').click()
            page.wait_for_function("reads => window.featureReadbackReads > reads", arg=reads, timeout=5000)
        expect(field).to_have_value("7" if mode == "saved" else "1")
        expect(field).to_be_disabled()
        expect(page.locator("#saveFeature")).to_be_disabled()
        expect(note).to_contain_text("请重新读取后再修改")
        page.locator("#featureForm").dispatch_event("submit")
        assert page.evaluate("window.featureReadbackWrites") == int(mode == "saved")
        assert page.evaluate("featureDraftState.featureDirty") is False
        expect(page.locator("#toastRegion .toast")).to_have_count(0, timeout=8000)
        note.scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f"feature-readonly-{mode}-{trigger}-{width}.png"), animations="disabled")
        page.evaluate("window.failFeatureReadback = false")
        page.locator("#refreshFeature").click()
        expect(field).to_be_enabled()
        expect(page.locator("#saveFeature")).to_be_enabled()
        expect(note).not_to_contain_text("当前仅显示上次内容")
        page.wait_for_function("featureDraftState.featureDetail.revision === 'verified-revision'")
        assert page.evaluate("window.featureReadbackWrites") == int(mode == "saved")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.url == manager_url and page.title().startswith("功能中心")
        assert not errors, errors
    finally:
        page.close()
