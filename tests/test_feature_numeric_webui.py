"""Rendered numeric configuration checks; preview data only, no robot."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_settings_webui import _open_feature_draft_page


def _open_numeric_page(browser, manager_url, width=1380):
    page = _open_feature_draft_page(browser, manager_url)
    page.set_viewport_size({"width": width, "height": 860})
    page.evaluate("""() => {
      const api=window.pywebview.api, read=api.get_feature, save=api.save_feature;
      window.numericSaveCalls=[];
      api.save_feature=(...args) => {window.numericSaveCalls.push(args); return save(...args);};
      api.get_feature=async id => {
        const result=await read(id);
        if (id !== 'feature-group-analysis') return result;
        const schema={type:'object',description:'数字设置',items:{
          count:{type:'int',description:'返回数量',default:3,_nullable:false},
          ratio:{type:'float',description:'采样比例',default:0.5,_nullable:false},
          optional:{type:'int',description:'可选上限',default:null,_nullable:true},
          optional_ratio:{type:'float',description:'可选比例',default:null,_nullable:true}
        }};
        for (const key of ['basic_schema','schema','advanced_schema']) result.data[key].numbers=schema;
        for (const key of ['config','advanced_config']) {
          result.data[key].numbers ??= {count:3,ratio:0.5,optional:1,optional_ratio:0.5};
        }
        return result;
      };
    }""")
    page.locator("#refreshFeature").click()
    expect(page.locator("#config-numbers--count")).to_have_value("3")
    return page


@pytest.mark.parametrize("key,label,width", [("count", "返回数量", 1380), ("ratio", "采样比例", 1080)])
def test_required_numeric_blank_focuses_field_and_zero_can_be_saved(browser, manager_url, tmp_path, key, label, width):
    page = _open_numeric_page(browser, manager_url, width)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        field = page.locator(f"#config-numbers--{key}")
        field.fill("")
        page.locator("#saveFeature").click()
        expect(page.locator("#featureNote")).to_contain_text(f"{label}不能为空，请填写数字")
        expect(field).to_be_focused()
        expect(field).to_have_attribute("aria-invalid", "true")
        expect(page.locator(f"#config-numbers--{key}-error")).to_have_text(f"{label}不能为空，请填写数字。")
        assert page.evaluate("window.numericSaveCalls.length") == 0
        page.screenshot(path=str(tmp_path / f"numeric-{key}-empty.png"))

        field.fill("0")
        page.locator("#saveFeature").click()
        page.wait_for_function("window.numericSaveCalls.length === 1 && !featureDraftState.featureSaving")
        assert page.evaluate("window.numericSaveCalls[0][1].numbers")[key] == 0
        expect(page.locator(f"#config-numbers--{key}")).to_have_value("0")
        expect(page.locator(".config-input[aria-invalid]")).to_have_count(0)
        assert not errors, errors
        assert page.url == manager_url and page.title().startswith("功能中心")
    finally:
        page.close()


def test_explicit_optional_numeric_fields_can_be_cleared(browser, manager_url):
    page = _open_numeric_page(browser, manager_url)
    try:
        for key in ["optional", "optional_ratio"]:
            page.locator(f"#config-numbers--{key}").fill("")
        page.locator("#saveFeature").click()
        page.wait_for_function("window.numericSaveCalls.length === 1 && !featureDraftState.featureSaving")
        saved = page.evaluate("window.numericSaveCalls[0][1].numbers")
        assert saved["optional"] is None and saved["optional_ratio"] is None
        expect(page.locator(".config-input[aria-invalid]")).to_have_count(0)
        for key in ["optional", "optional_ratio"]:
            expect(page.locator(f"#config-numbers--{key}")).to_have_value("")
    finally:
        page.close()
