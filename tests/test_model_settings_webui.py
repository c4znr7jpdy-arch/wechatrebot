"""Model editor behavior through the delivered asset routes, without a robot."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def _mock_embedding_deletion(page):
    """Keep the delivered UI while isolating model mutations from an installation."""
    preview_source = (Path(__file__).resolve().parents[1] / "src/wechatrebot_manager/app_webui/preview.js").read_text(encoding="utf-8")
    def preview_route(route):
        # Use the shipped asset without a second request to the temporary server.
        route.fulfill(status=200, content_type="application/javascript", body=preview_source + """
        const originalPreview = window.createPreviewApi;
        window.createPreviewApi = (helpers) => {
          const controller = originalPreview(helpers), originalApi = controller.previewApi;
          controller.previewApi = () => {
            const api = originalApi(), bootstrap = api.bootstrap;
            let providers = [
              {id: 'custom/qwen', name: 'qwen3-embedding-8b', model: 'qwen3-embedding-8b',
                service_kind: 'embedding', configured: true, references: ['长期记忆']},
              {id: 'custom/gemini', name: 'gemini-embedding-2-preview', model: 'gemini-embedding-2-preview',
                service_kind: 'embedding', configured: true, references: []},
              {id: 'custom/chat', name: 'chat-model', model: 'chat-model',
                service_kind: 'chat', configured: true, is_default: true, references: []}
            ];
            window.deleteCalls = [];
            window.failDeletion = true;
            api.bootstrap = async () => {
              const result = await bootstrap();
              result.data.providers = structuredClone(providers);
              result.data.experience.onboarding_complete = true;
              return result;
            };
            api.get_providers = () => ({ok: true, data: structuredClone(providers)});
            api.delete_provider = (id, replacement) => {
              window.deleteCalls.push([id, replacement]);
              if (window.failDeletion) return {ok: false, error: {message: '模拟删除失败，请重试'}};
              providers = providers.filter(item => item.id !== id);
              return {ok: true, data: {id}};
            };
            return api;
          };
          return controller;
        };
        """)
    page.route("**/preview.js*", preview_route)


@pytest.mark.parametrize("theme,width,height,entry", [
    ("light", 1440, 1024, "footer"),
    ("dark", 1080, 680, "menu"),
    ("light", 760, 800, "editor"),
])
def test_embedding_delete_identifies_target_and_keeps_replacement(browser, manager_url, tmp_path, theme, width, height, entry):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    _mock_embedding_deletion(page)
    try:
        page.add_init_script(f"localStorage.setItem('wechatrebot-theme', '{theme}')")
        page.goto(manager_url)
        expect(page).to_have_title(re.compile("机器人管理$"))
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        page.locator('.nav-item[data-page="settings"]').click()
        target = page.locator(".provider-service").filter(has_text="qwen3-embedding-8b")
        if entry == "menu":
            target.locator("summary").click()
            target.locator('[role="menuitem"][data-provider-delete]').click()
        elif entry == "editor":
            target.locator("footer [data-provider-edit]").click()
            page.locator("#deleteProvider").click()
        else:
            target.locator("footer [data-provider-delete]").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        expect(page.locator("#modalSubjectLabel")).to_have_text("将删除的模型")
        expect(page.locator("#modalSubjectValue")).to_have_text("qwen3-embedding-8b")
        expect(page.locator("#modalField label")).to_contain_text("删除后，长期记忆改用")
        expect(page.locator("#modalInput option")).to_have_text(["gemini-embedding-2-preview"])
        expect(page.locator("#modalIcon")).to_have_count(0)
        expect(page.locator("#modalConfirm")).to_have_text("切换并删除")
        # All controls, including the replacement select, participate in focus trapping.
        page.locator("#modalConfirm").focus()
        page.keyboard.press("Tab")
        expect(page.locator("#modalInput")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator("#modalConfirm")).to_be_focused()
        bounds = page.get_by_role("dialog").bounding_box()
        assert bounds and 0 <= bounds["x"] and bounds["x"] + bounds["width"] <= width
        page.screenshot(path=str(tmp_path / f"model-delete-{theme}-{entry}.png"))
        page.locator("#modalConfirm").click()
        expect(page.locator("#modalError")).to_have_text("模拟删除失败，请重试")
        expect(page.locator("#modalSubjectValue")).to_have_text("qwen3-embedding-8b")
        expect(page.locator("#modalCancel")).to_be_enabled()
        expect(page.locator("#modalConfirm")).to_be_enabled()
        page.evaluate("window.failDeletion = false")
        page.locator("#modalConfirm").click()
        expect(page.get_by_role("dialog")).to_be_hidden()
        expect(target).to_have_count(0)
        replacement = page.locator(".provider-service").filter(has_text="gemini-embedding-2-preview")
        expect(replacement).to_be_visible()
        assert page.evaluate("window.deleteCalls") == [["custom/qwen", "custom/gemini"]] * 2
        # A later unreferenced deletion must show its own target and no stale replacement.
        replacement.locator("footer [data-provider-delete]").click()
        expect(page.locator("#modalSubjectValue")).to_have_text("gemini-embedding-2-preview")
        expect(page.locator("#modalInput")).to_have_count(0)
        expect(page.locator("#modalConfirm")).to_have_text("确认删除")
        page.locator("#modalCancel").click()
        expect(replacement).to_be_visible()
        replacement.locator("footer [data-provider-edit]").click()
        page.locator("#providerApiKey").fill("preview-only")
        page.locator("#closeProviderEditor").click()
        expect(page.get_by_role("dialog")).to_be_visible()
        expect(page.locator("#modalSubject")).to_be_hidden()
        page.locator("#modalCancel").click()
        assert page.url == manager_url
        assert not errors, errors
        assert not failures, failures
    finally:
        page.close()


@pytest.mark.parametrize("theme,width,height", [("light", 1440, 1024), ("dark", 1080, 680)])
def test_provider_icons_default_save_and_editor_navigation(browser, manager_url, tmp_path, theme, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    try:
        page.add_init_script(f"localStorage.setItem('wechatrebot-theme', '{theme}')")
        page.goto(manager_url)
        expect(page).to_have_title(re.compile("机器人管理$"))
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        expect(page.locator("#settingsTabs small")).to_have_count(0)
        page.locator("[data-provider-create]").click()
        expect(page.locator("#saveProvider")).to_have_text("保存并设为回复模型")
        expect(page.locator("#providerSaveIntentTitle, .provider-form-note")).to_have_count(0)
        for preset in ["glm", "xiaomi_mimo", "qwen", "volcengine", "hunyuan", "gemini_proxy", "deepseek"]:
            page.locator("#providerPreset").select_option(preset)
            expect(page.locator("#providerPresetIcon")).to_have_attribute("data-preset", preset)
            expect(page.locator("#providerPresetIcon img")).to_be_visible()
            # Visibility plus the response-failure guard below proves the
            # bundled asset loaded without CSP-unsafe wait_for_function eval.
            expect(page.locator("#providerPresetIcon img")).to_have_attribute("src", re.compile(r"assets/provider-"))
        page.locator('[name="api_base"]').fill("https://api.deepseek.com.relay.example/v1")
        expect(page.locator("#providerPresetIcon")).to_have_attribute("data-preset", "custom")
        expect(page.locator("#providerPresetIcon svg")).to_be_visible()
        page.locator("#providerPreset").select_option("deepseek")
        page.locator("#closeProviderEditor").hover()
        colors = page.locator("#closeProviderEditor").evaluate("""el => {
          const style = getComputedStyle(el);
          return {color: style.color, background: style.backgroundColor};
        }""")
        assert colors["color"] == ("rgb(166, 198, 255)" if theme == "dark" else "rgb(43, 69, 57)")
        assert colors["background"] != "rgba(0, 0, 0, 0)"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.screenshot(path=str(tmp_path / f"model-editor-{theme}.png"))
        page.locator("#providerApiKey").fill("preview-only")
        page.locator("#closeProviderEditor").click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(page.locator("#providerEditorPage")).to_be_visible()
        page.locator("#saveProvider").click()
        expect(page.locator("#providerOverview")).to_be_visible()
        expect(page.locator('.provider-service.active .provider-logo img')).to_have_attribute("src", "assets/provider-deepseek.ico")
        page.locator("[data-provider-create]").click()
        expect(page.locator("#saveProvider")).to_have_text("保存模型服务")
        page.locator("#providerPreset").select_option("custom")
        page.locator('[name="api_base"]').fill("https://relay.example/v1")
        page.locator("#providerModelInput").fill("deepseek-flash")
        page.locator("#providerApiKey").fill("preview-only")
        page.locator("#saveProvider").click()
        relay = page.locator(".provider-service").filter(has_text="其他兼容服务")
        expect(relay.locator(".provider-logo svg")).to_be_visible()
        expect(relay.locator(".provider-logo img")).to_have_count(0)
        relay.locator(".provider-service-actions [data-provider-edit]").click()
        expect(page.locator("#providerPreset")).to_have_value("custom")
        expect(page.locator("#providerPresetIcon")).to_have_attribute("data-preset", "custom")
        assert not failures, failures
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("theme,width,height", [("light", 1440, 1000), ("dark", 760, 850)])
def test_model_test_shows_real_answer_failure_and_ignores_stale_result(browser, manager_url, tmp_path, theme, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    try:
        page.add_init_script(f"localStorage.setItem('wechatrebot-theme', '{theme}')")
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator("[data-provider-create]").click()
        page.locator("#providerApiKey").fill("preview-only")
        page.locator("#saveProvider").click()
        expect(page.locator("#providerOverview")).to_be_visible()
        page.locator('.provider-service footer [data-provider-edit]').first.click()
        expect(page.locator("#testProvider")).to_have_text("测试模型")
        original_model = page.locator("#providerModelInput").input_value()
        page.evaluate("""() => {
          window.modelTestCalls = [];
          window.pywebview.api.test_provider = id => {
            window.modelTestCalls.push(id);
            return new Promise(resolve => { window.finishModelTest = resolve; });
          };
        }""")
        page.locator("#providerModelInput").fill("unsaved-model")
        page.locator("#testProvider").click()
        expect(page.locator("#providerNote")).to_have_text("请先保存修改，再测试当前模型。")
        assert page.evaluate("window.modelTestCalls.length") == 0
        page.locator("#providerModelInput").fill(original_model)
        page.locator("#testProvider").click()
        expect(page.locator("#providerTestTitle")).to_have_text("正在请求模型…")
        expect(page.locator("#testProvider")).to_be_disabled()
        page.evaluate("""() => window.finishModelTest({ok: true, data: {
          id: window.modelTestCalls[0], model: 'saved-model', test_kind: 'chat',
          status: 'available', response_text: 'PONG <script>literal</script>', elapsed_ms: 1250
        }})""")
        expect(page.locator("#providerTestTitle")).to_have_text("本次模型测试成功")
        expect(page.locator("#providerTestMeta")).to_have_text("saved-model · 1.3 秒")
        expect(page.locator("#providerTestOutput")).to_have_text("PONG <script>literal</script>")
        expect(page.locator("#providerTestOutput script")).to_have_count(0)
        page.screenshot(path=str(tmp_path / f"model-test-success-{theme}.png"))
        page.locator("#testProvider").click()
        page.evaluate("""() => window.finishModelTest({ok:false, error:{
          title:'模型响应超时', message:'未收到完整回答', detail:'模型 saved-model：请求超时',
          code:'16007', recorded:true
        }})""")
        expect(page.locator("#providerTestTitle")).to_have_text("模型响应超时")
        expect(page.locator("#providerTestOutput")).to_have_text("模型 saved-model：请求超时")
        expect(page.locator("#providerTestMeta")).to_contain_text("16007")
        page.screenshot(path=str(tmp_path / f"model-test-timeout-{theme}.png"))
        page.locator("#testProvider").click()
        page.locator("#closeProviderEditor").click()
        page.locator('.provider-service footer [data-provider-edit]').first.click()
        expect(page.locator("#providerTestResult")).to_be_hidden()
        page.evaluate("""() => window.finishModelTest({ok:true, data:{
          id:window.modelTestCalls[0], model:'old', status:'available', test_kind:'chat',
          response_text:'stale response', elapsed_ms:12
        }})""")
        expect(page.locator("#testProvider")).to_be_enabled()
        expect(page.locator("#providerTestResult")).to_be_hidden()
        assert page.url == manager_url and page.title().startswith("机器人总设置")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
    finally:
        page.close()
