"""Model actions retain confirmed results when a subsequent list read fails."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_model_settings_webui import _mock_embedding_deletion


@pytest.mark.parametrize("referenced,width", [(False, 430), (True, 1380)])
def test_deleted_model_is_removed_before_failed_list_read(browser, manager_url, tmp_path, referenced, width):
    page = browser.new_page(viewport={"width": width, "height": 920})
    _mock_embedding_deletion(page)
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        page.locator('.nav-item[data-page="settings"]').click()
        page.evaluate("""() => {
          const api = window.pywebview.api, read = api.get_providers;
          window.failDeletion = false;
          window.failProviderRead = true;
          api.get_providers = async () => window.failProviderRead
            ? {ok:false,error:{message:'模拟删除后列表读取失败'}} : await read();
        }""")
        provider_id = "custom/qwen" if referenced else "custom/gemini"
        page.locator(f'footer [data-provider-delete="{provider_id}"]').click()
        if referenced:
            expect(page.locator("#modalInput")).to_have_value("custom/gemini")
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(page.locator(f'footer [data-provider-edit="{provider_id}"]')).to_have_count(0)
        expect(page.locator("#providerReadbackMessage")).to_contain_text("模型服务已删除")
        expect(page.locator("#toastRegion .toast.error").last).to_contain_text("模型服务已删除")
        expect(page.locator("#reloadProviderServices")).to_be_enabled()
        page.locator("#providerReadbackFeedback").scroll_into_view_if_needed()
        page.screenshot(path=str(tmp_path / f"model-deleted-list-failed-{width}.png"), animations="disabled")
        page.locator("#reloadProviderServices").click()
        expect(page.locator("#reloadProviderServices")).to_be_enabled()
        assert page.evaluate("window.deleteCalls") == [[provider_id, "custom/gemini" if referenced else ""]]
        page.evaluate("window.failProviderRead = false")
        page.locator("#reloadProviderServices").click()
        expect(page.locator("#providerReadbackFeedback")).to_be_hidden()
        expect(page.locator(f'footer [data-provider-edit="{provider_id}"]')).to_have_count(0)
        assert len(page.evaluate("window.deleteCalls")) == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    finally:
        page.close()


@pytest.mark.parametrize("action", ["default", "thinking", "delete_default"])
def test_model_action_is_visible_after_failed_list_read(browser, manager_url, tmp_path, action):
    page = browser.new_page(viewport={"width": 1380, "height": 920})
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        for model in ["first-model", "alternate-model"]:
            page.locator("[data-provider-create]").click()
            page.locator("#providerModelInput").fill(model)
            page.locator("#providerApiKey").fill("preview-only")
            page.locator("#saveProvider").click()
            expect(page.locator("#providerOverview")).to_be_visible()
        page.evaluate("""action => {
          const api = window.pywebview.api, read = api.get_providers;
          const method = action === 'default' ? 'set_default_provider'
            : action === 'thinking' ? 'set_provider_thinking' : 'delete_provider';
          const mutate = api[method];
          const setDefault = api.set_default_provider;
          window.modelActionCalls = [];
          window.failProviderRead = true;
          api[method] = async (...args) => {
            window.modelActionCalls.push(args);
            // Match the backend's default replacement; preview deletion only removes the card.
            if (action === 'delete_default') await setDefault(args[1]);
            return await mutate(...args);
          };
          api.get_providers = async () => window.failProviderRead
            ? {ok:false,error:{message:'模拟操作后列表读取失败'}} : await read();
        }""", action)
        alternate_id = "deepseek/alternate-model"
        if action == "default":
            page.locator(f'footer [data-provider-default="{alternate_id}"]').click()
            expect(page.locator('.provider-service.active footer [data-provider-edit]')).to_have_attribute("data-provider-edit", alternate_id)
            message = "回复模型已切换"
        elif action == "thinking":
            page.locator(f'[data-provider-thinking="{alternate_id}"]').click()
            expect(page.locator(f'[data-provider-thinking="{alternate_id}"]')).to_have_attribute("aria-checked", "false")
            message = "思考模式已保存"
        else:
            page.locator('footer [data-provider-delete="deepseek/first-model"]').click()
            expect(page.locator("#modalInput")).to_have_value(alternate_id)
            page.locator("#modalConfirm").click()
            expect(page.locator("#modalBackdrop")).to_be_hidden()
            expect(page.locator('footer [data-provider-edit="deepseek/first-model"]')).to_have_count(0)
            expect(page.locator('.provider-service.active footer [data-provider-edit]')).to_have_attribute("data-provider-edit", alternate_id)
            message = "模型服务已删除"
        expect(page.locator("#providerReadbackMessage")).to_contain_text(message)
        expect(page.locator("#reloadProviderServices")).to_be_enabled()
        page.screenshot(path=str(tmp_path / f"model-{action}-list-failed.png"), animations="disabled")
        page.locator("#reloadProviderServices").click()
        expect(page.locator("#reloadProviderServices")).to_be_enabled()
        assert len(page.evaluate("window.modelActionCalls")) == 1
        page.evaluate("window.failProviderRead = false")
        page.locator("#reloadProviderServices").click()
        expect(page.locator("#providerReadbackFeedback")).to_be_hidden()
        if action in {"default", "delete_default"}:
            expect(page.locator('.provider-service.active footer [data-provider-edit]')).to_have_attribute("data-provider-edit", alternate_id)
        else:
            expect(page.locator(f'[data-provider-thinking="{alternate_id}"]')).to_have_attribute("aria-checked", "false")
        assert len(page.evaluate("window.modelActionCalls")) == 1
    finally:
        page.close()
