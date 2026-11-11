"""Model discovery stays scoped to the current editor, using preview data only."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("change", ["preset", "kind", "endpoint", "key", "editor", "page", "section"])
@pytest.mark.parametrize("old_fails", [False, True])
def test_discovery_ignores_replaced_requests(browser, manager_url, tmp_path, change, old_fails):
    page = browser.new_page(viewport={"width": 760 if change == "key" else 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator("[data-provider-create]").click()
        page.evaluate("""() => {
          window.discoveryReplies = [];
          window.pywebview.api.discover_provider_models = () =>
            new Promise(resolve => window.discoveryReplies.push(resolve));
        }""")
        button = page.locator("#discoverProviderModels")
        button.click()
        expect(button).to_be_disabled()
        if change == "preset":
            page.locator("#providerPreset").select_option("qwen")
        elif change == "kind":
            page.locator("#providerServiceKind").select_option("embedding")
        elif change == "endpoint":
            page.locator('#providerForm [name="api_base"]').fill("https://review.example/v1")
        elif change == "key":
            page.locator("#providerApiKey").fill("preview-only-new-key")
        elif change == "editor":
            page.locator("#closeProviderEditor").click()
            page.locator("[data-provider-create]").click()
        elif change == "page":
            page.locator('.nav-item[data-page="home"]').click()
            page.locator('.nav-item[data-page="settings"]').click()
        else:
            page.locator('[data-settings-tab="skin"]').click()
            page.locator('[data-settings-tab="model"]').click()
        expect(button).to_be_enabled()
        button.click()
        expect(button).to_be_disabled()
        page.evaluate("""oldFails => window.discoveryReplies[0](oldFails
          ? {ok:false, error:{message:'过期的模型发现错误', code:'16007', recorded:true}}
          : {ok:true, data:{items:['stale-model']}})
        """, old_fails)
        # Completing an earlier request must not unlock the active request.
        expect(button).to_be_disabled()
        expect(page.locator("#providerModelHint")).to_have_text("正在验证地址和密钥，并读取模型列表…")
        expect(page.locator("#providerModelOptions")).not_to_contain_text("stale-model")
        expect(page.locator("#toastRegion")).not_to_contain_text("过期的模型发现错误")
        page.evaluate("""() => window.discoveryReplies[1]({ok:true,
          data:{items:['current-model', 'current-model', 'current-vision']}})""")
        expect(button).to_be_enabled()
        expect(page.locator("#providerModelHint")).to_contain_text("已读取 2 个模型")
        expect(page.locator('#providerModelOptions [role="option"]')).to_have_count(2)
        page.locator('#providerModelOptions [role="option"]').filter(has_text="current-model").click()
        expect(page.locator("#providerModelInput")).to_have_value("current-model")
        expect(page.locator("#providerModelOptions")).to_be_hidden()
        assert page.evaluate("window.discoveryReplies.length") == 2
        expect(button).to_have_text("获取可用模型")
        assert page.url == manager_url
        expect(page).to_have_title(re.compile("机器人总设置"))
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"discovery-{change}-{old_fails}.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()


def test_discovery_accepts_normalized_url_and_keeps_models_selectable(browser, manager_url, tmp_path):
    page = browser.new_page(viewport={"width": 760, "height": 920})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator("[data-provider-create]").click()
        page.locator("#providerPreset").select_option("custom")
        endpoint = page.locator('#providerForm [name="api_base"]')
        endpoint.fill("https://relay.example/v1")
        page.locator("#providerModelInput").fill("saved-model")
        page.locator("#providerApiKey").fill("preview-only-discovery")
        page.locator("#saveProvider").click()
        expect(page.locator("#providerOverview")).to_be_visible()
        page.locator('.provider-service footer [data-provider-edit]').first.click()
        endpoint.fill("https://relay.example")
        expect(page.locator("#providerEditorStatus")).to_have_text("有未保存修改")
        page.evaluate("""() => {
          window.pywebview.api.discover_provider_models = values => {
            window.discoverySubmittedUrl = values.api_base;
            return new Promise(resolve => { window.finishNormalizedDiscovery = resolve; });
          };
        }""")
        page.locator("#discoverProviderModels").click()
        expect(page.locator("#discoverProviderModels")).to_be_disabled()
        page.evaluate("""() => window.finishNormalizedDiscovery({ok:true, data:{
          api_base:'https://relay.example/v1', items:['saved-model', 'new-model']}})""")
        expect(page.locator("#providerModelHint")).to_contain_text("已读取 2 个模型")
        expect(page.locator("#discoverProviderModels")).to_be_enabled()
        expect(page.locator("#discoverProviderModels")).to_have_text("获取可用模型")
        expect(endpoint).to_have_value("https://relay.example/v1")
        expect(page.locator("#providerEditorStatus")).to_have_text("已保存")
        assert page.evaluate("window.discoverySubmittedUrl") == "https://relay.example"
        expect(page.locator('#providerModelOptions [role="option"]')).to_have_count(2)
        page.locator('#providerModelOptions [role="option"]').filter(has_text="new-model").click()
        expect(page.locator("#providerModelInput")).to_have_value("new-model")
        expect(page.locator("#providerEditorStatus")).to_have_text("有未保存修改")
        page.screenshot(path=str(tmp_path / "discovery-normalized-url.png"), animations="disabled")
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("change", ["draft", "editor"])
@pytest.mark.parametrize("phase", ["save", "readback"])
def test_provider_save_preserves_newer_draft(browser, manager_url, tmp_path, change, phase):
    page = browser.new_page(viewport={"width": 1380, "height": 860})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator("[data-provider-create]").click()
        page.locator("#providerApiKey").fill("preview-only-submitted")
        page.evaluate("""phase => {
          const api = window.pywebview.api, save = api.save_provider, read = api.get_providers;
          window.providerSaveCalls = [];
          api.save_provider = async values => {
            window.providerSaveCalls.push(structuredClone(values));
            const result = await save(values);
            window.savedProviderId = result.data.id;
            if (phase === 'save' && window.providerSaveCalls.length === 1) {
              return new Promise(resolve => { window.finishPendingProviderSave = () => resolve(result); });
            }
            return result;
          };
          api.get_providers = async () => {
            const result = await read();
            if (phase === 'readback' && window.providerSaveCalls.length === 1) {
              return new Promise(resolve => { window.finishPendingProviderSave = () => resolve(result); });
            }
            return result;
          };
        }""", phase)
        page.locator("#saveProvider").click()
        expect(page.locator("#saveProvider")).to_be_disabled()
        assert page.evaluate("typeof window.finishPendingProviderSave") == "function"
        if change == "editor":
            page.locator("#closeProviderEditor").click()
            if phase == "save":
                page.locator("#modalConfirm").click()
            page.locator("[data-provider-create]").click()
            page.locator("#providerPreset").select_option("custom")
            page.locator("#providerServiceKind").select_option("image")
            page.locator('#providerForm [name="api_base"]').fill("https://review.example/v1")
        page.locator("#providerModelInput").fill("newer-draft-model")
        page.locator("#providerApiKey").fill("preview-only-newer-draft")
        page.evaluate("window.finishPendingProviderSave()")
        expect(page.locator("#saveProvider")).to_be_enabled()
        expect(page.locator("#providerEditorPage")).to_be_visible()
        expect(page.locator("#providerModelInput")).to_have_value("newer-draft-model")
        expect(page.locator("#providerApiKey")).to_have_value("preview-only-newer-draft")
        expect(page.locator("#providerEditorStatus")).to_have_text("有未保存修改")
        if change == "editor":
            expect(page.locator("#saveProvider")).to_have_text("保存图片模型")
            expect(page.locator('#providerForm [name="provider_id"]')).to_have_value("")
        else:
            expect(page.locator("#providerNote")).to_contain_text("之后的修改仍未保存")
            expect(page.locator('#providerForm [name="provider_id"]')).to_have_value(page.evaluate("window.savedProviderId"))
        page.screenshot(path=str(tmp_path / f"provider-save-{phase}-{change}.png"), animations="disabled")
        page.locator("#closeProviderEditor").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        page.locator("#saveProvider").click()
        expect(page.locator("#providerOverview")).to_be_visible()
        assert page.evaluate("window.providerSaveCalls.length") == 2
        assert not errors, errors
    finally:
        page.close()
