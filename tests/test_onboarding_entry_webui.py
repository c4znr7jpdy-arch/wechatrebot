"""Configuration shortcuts reach models without discarding other drafts."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def _open_page(browser, manager_url, *, width=1080, first_install=False, errors=None):
    page = browser.new_page(viewport={"width": width, "height": 900 if width < 600 else 680})
    if errors is not None:
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
    page.add_init_script("""(() => {
      const firstInstall=FIRST_INSTALL;
      Object.defineProperty(window, 'createPreviewApi', {configurable:true, set(create) {
        Object.defineProperty(window, 'createPreviewApi', {value:helpers => {
          const controller=create(helpers), factory=controller.previewApi;
          controller.previewApi=() => {
            const api=factory(), bootstrap=api.bootstrap, read=api.get_onboarding_readiness;
            const missingProvider=result => {
              result.data.complete=false;
              result.data.items=result.data.items.map(item => item.id === 'provider'
                ? {...item, status:'needs_config', detail:'还没有配置可用的聊天模型。', next_step:'配置聊天模型'} : item);
              return result;
            };
            api.get_onboarding_readiness=async () => missingProvider(await read());
            api.bootstrap=async () => {
              const result=await bootstrap();
              result.data.experience.onboarding_complete=!firstInstall;
              result.data.provider=null; result.data.providers=[];
              result.data.readiness=missingProvider({data:result.data.readiness}).data;
              return result;
            };
            return api;
          };
          return controller;
        }});
      }});
      Object.defineProperty(window, 'createManagerSettings', {configurable:true, set(create) {
        Object.defineProperty(window, 'createManagerSettings', {value:helpers => {
          const controller=create(helpers);
          window.onboardingEntryController=controller;
          return controller;
        }});
      }});
    })();""".replace("FIRST_INSTALL", str(first_install).lower()))
    page.goto(manager_url)
    expect(page.locator("#stateTitle")).to_have_text("机器人可用")
    return page


@pytest.mark.parametrize("width", [1080, 430])
@pytest.mark.parametrize("entry", ["onboarding", "home", "help"])
def test_configuration_entries_reach_models_from_system(browser, manager_url, tmp_path, width, entry):
    errors = []
    page = _open_page(browser, manager_url, width=width, errors=errors)
    try:
        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="system"]').click()
        expect(page.locator("#page-settings")).to_have_attribute("data-settings-section", "system")
        if entry == "onboarding":
            page.locator("#openOnboarding").click()
            expect(page.locator("#onboardingChecklist")).to_contain_text("还没有配置可用的聊天模型")
            page.locator("#onboardingConfigure").click()
            expect(page.locator("#onboardingBackdrop")).to_be_hidden()
        else:
            page.locator(f'.nav-item[data-page="{entry}"]').click()
            selector = '[data-home-page="settings"]' if entry == "home" else '[data-help-target="settings"]'
            page.locator(selector).click()
        expect(page.locator("#page-settings")).to_be_visible()
        expect(page.locator("#page-settings")).to_have_attribute("data-settings-section", "model")
        expect(page.locator("#providerOverview")).to_be_visible()
        expect(page.locator("[data-provider-create]")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert page.url == manager_url and page.title().startswith("机器人总设置")
        # Opening help also builds the existing hidden feature iframe. Its
        # empty preview URL has the same CSP warning covered by settings QA.
        unexpected = [error for error in errors if not (
            entry == "help"
            and error.startswith(("Refused to frame '' because it violates", "Framing '' violates"))
            and '"frame-src \'self\'"' in error
        )]
        assert not unexpected, unexpected
        page.screenshot(path=str(tmp_path / f"{entry}-model-entry-{width}.png"))
    finally:
        page.close()


def test_first_install_configure_reaches_models(browser, manager_url):
    page = _open_page(browser, manager_url, first_install=True)
    try:
        expect(page.locator("#onboardingBackdrop")).to_be_visible()
        expect(page.locator("#onboardingChecklist")).to_contain_text("还没有配置可用的聊天模型")
        page.locator("#onboardingConfigure").click()
        expect(page.locator("#onboardingBackdrop")).to_be_hidden()
        expect(page.locator("#page-settings")).to_be_visible()
        expect(page.locator("#providerOverview")).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("draft", ["core", "model"])
def test_onboarding_configuration_preserves_unsaved_drafts(browser, manager_url, draft):
    page = _open_page(browser, manager_url)
    try:
        page.locator('.nav-item[data-page="settings"]').click()
        if draft == "core":
            page.locator('[data-settings-tab="core"]').click()
            field = page.locator('#coreForm [name="robot_name"]')
        else:
            page.locator("[data-provider-create]").click()
            field = page.locator('#providerForm [name="model"]')
        field.fill("未保存配置")
        page.evaluate("window.onboardingEntryController.showOnboarding(true)")
        page.locator("#onboardingConfigure").click()
        expect(page.locator("#onboardingBackdrop")).to_be_hidden()
        if draft == "core":
            expect(page.locator("#modalBackdrop")).to_be_visible()
            page.locator("#modalCancel").click()
            expect(page.locator("#page-settings")).to_have_attribute("data-settings-section", "core")
        else:
            expect(page.locator("#providerEditorPage")).to_be_visible()
            expect(page.locator("#modalBackdrop")).to_be_hidden()
        expect(field).to_have_value("未保存配置")
        page.locator('.nav-item[data-page="home"]').click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(field).to_have_value("未保存配置")
        expect(page.locator("#page-settings")).to_be_visible()
    finally:
        page.close()
