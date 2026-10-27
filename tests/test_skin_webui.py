"""Skin selection through shipped manager routes, without starting a robot."""
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


def open_skin_settings(page, url):
    page.goto(url)
    expect(page).to_have_title(re.compile("机器人管理$"))
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    if page.locator("#onboardingRecommended").is_visible():
        page.locator("#onboardingRecommended").click()
    page.locator('.nav-item[data-page="settings"]').click()
    page.locator('[data-settings-tab="skin"]').click()
    expect(page.locator("#skinOptions")).to_be_visible()


@pytest.mark.parametrize("width,height", [(1440, 900), (1080, 680), (600, 860)])
def test_skins_switch_independently_of_mode_and_survive_reload(browser, manager_url, tmp_path, width, height):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors, failures = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)
    page.on("response", lambda response: failures.append(response.url) if response.status >= 400 else None)
    try:
        open_skin_settings(page, manager_url)
        for skin in ("soft", "default"):
            page.locator(f'[data-skin-option][value="{skin}"]').check()
            expect(page.locator("#skinStatus")).to_contain_text("已应用")
            expect(page.locator(f'[data-skin-option][value="{skin}"]')).to_be_checked()
            for theme in ("dark", "light"):
                if page.locator("html").get_attribute("data-theme") != theme:
                    page.locator("#themeToggle").click()
                expect(page.locator("html")).to_have_attribute("data-theme", theme)
                assert page.locator("html").get_attribute("data-skin") == ("soft" if skin == "soft" else None)
                assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
                page.screenshot(path=str(tmp_path / f"skin-{skin}-{theme}-{width}.png"), animations="disabled")
            open_skin_settings(page, manager_url)
            expect(page.locator(f'[data-skin-option][value="{skin}"]')).to_be_checked()
        assert not errors, errors
        assert not failures, failures
    finally:
        page.close()


def test_skin_save_failure_keeps_previous_skin_and_allows_retry(browser, manager_url):
    page = browser.new_page()
    try:
        open_skin_settings(page, manager_url)
        page.evaluate("""() => {
          window.originalSkinSave = window.pywebview.api.save_skin;
          window.pywebview.api.save_skin = async () => ({ok: false,
            error: {code:'15001', title:'保存失败', message:'测试写入失败', recorded:true}});
        }""")
        page.locator('[data-skin-option][value="soft"]').click()
        expect(page.locator("#skinStatus")).to_contain_text("未能保存")
        expect(page.locator('[data-skin-option][value="default"]')).to_be_checked()
        assert page.locator("html").get_attribute("data-skin") is None
        page.evaluate("window.pywebview.api.save_skin = window.originalSkinSave")
        page.locator('[data-skin-option][value="soft"]').check()
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        expect(page.locator("html")).to_have_attribute("data-skin", "soft")
    finally:
        page.close()


def test_native_caption_receives_current_palette_at_startup_and_after_skin_or_mode_changes(browser, manager_url):
    page = browser.new_page()
    preview_source = (Path(__file__).resolve().parents[1] / "src/wechatrebot_manager/app_webui/preview.js").read_text(encoding="utf-8")

    def track_caption(route):
        # Use the shipped asset without a second request to the temporary server.
        route.fulfill(status=200, content_type="application/javascript", body=preview_source + """
        const createCaptionPreview = window.createPreviewApi;
        window.createPreviewApi = (helpers) => {
          const controller = createCaptionPreview(helpers), createApi = controller.previewApi;
          controller.previewApi = () => {
            const api = createApi();
            window.captionUpdates = [];
            api.sync_window_theme = (...palette) => {
              window.captionUpdates.push(palette);
              return {ok: true, data: {applied: true}};
            };
            return api;
          };
          return controller;
        };
        """)

    def assert_current_palette():
        page.wait_for_function("""() => {
          const root = document.documentElement, css = getComputedStyle(root);
          const expected = [root.dataset.theme, css.getPropertyValue('--surface').trim(),
            css.getPropertyValue('--ink').trim()];
          return JSON.stringify(window.captionUpdates.at(-1)) === JSON.stringify(expected);
        }""")

    page.route("**/preview.js*", track_caption)
    try:
        open_skin_settings(page, manager_url)
        assert_current_palette()
        for skin in ("soft", "default"):
            previous = page.evaluate("captionUpdates.length")
            page.locator(f'[data-skin-option][value="{skin}"]').check()
            expect(page.locator("#skinStatus")).to_contain_text("已应用")
            assert_current_palette()
            assert page.evaluate("captionUpdates.length") > previous
            page.locator("#themeToggle").click()
            assert_current_palette()
            assert page.evaluate("captionUpdates.length") > previous + 1
    finally:
        page.close()


@pytest.mark.parametrize("storage", ["unavailable", "invalid", "legacy"])
def test_skin_storage_fallback_and_legacy_migration(browser, manager_url, storage):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        if storage == "unavailable":
            page.add_init_script("Object.defineProperty(window, 'localStorage', {get() {throw new Error('blocked')}})")
        else:
            value = "soft" if storage == "legacy" else "unknown-skin"
            page.add_init_script(f"localStorage.setItem('wxrebot-skin', '{value}')")
        open_skin_settings(page, manager_url)
        expected = "soft" if storage == "legacy" else "default"
        expect(page.locator(f'[data-skin-option][value="{expected}"]')).to_be_checked()
        page.locator('[data-skin-option][value="soft"]').check()
        expect(page.locator("html")).to_have_attribute("data-skin", "soft")
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        assert not errors, errors
    finally:
        page.close()


def test_unavailable_skin_styles_fall_back_to_default(browser, manager_url):
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.route("**/soft-theme.css", lambda route: route.abort())
    try:
        open_skin_settings(page, manager_url + "&style=soft")
        expect(page.locator('[data-skin-option][value="default"]')).to_be_checked()
        expect(page.locator('[data-skin-option][value="soft"]')).to_be_disabled()
        expect(page.locator("#skinStatus")).to_contain_text("默认皮肤")
        assert page.locator("html").get_attribute("data-skin") is None
        assert not errors, errors
    finally:
        page.close()


def test_legacy_url_does_not_override_later_selection_on_reload(browser, manager_url):
    page = browser.new_page()
    try:
        open_skin_settings(page, manager_url + "&style=soft")
        expect(page.locator('[data-skin-option][value="soft"]')).to_be_checked()
        page.locator('[data-skin-option][value="default"]').check()
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        assert "style=" not in page.url
        assert "preview=1" in page.url
        open_skin_settings(page, page.url)
        expect(page.locator('[data-skin-option][value="default"]')).to_be_checked()
        assert page.locator("html").get_attribute("data-skin") is None
    finally:
        page.close()


def test_skin_tab_preserves_core_unsaved_changes_guard(browser, manager_url):
    page = browser.new_page()
    try:
        open_skin_settings(page, manager_url)
        page.locator('[data-settings-tab="core"]').click()
        page.locator('#coreForm [name="robot_name"]').fill("未保存名称")
        page.locator('[data-settings-tab="skin"]').click()
        expect(page.locator("#modalBackdrop")).to_be_visible()
        page.locator("#modalCancel").click()
        expect(page.locator('#coreForm [name="robot_name"]')).to_have_value("未保存名称")
        page.locator('[data-settings-tab="skin"]').click()
        page.locator("#modalConfirm").click()
        expect(page.locator("#skinOptions")).to_be_visible()
    finally:
        page.close()


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_soft_controls_preserve_shared_behavior(browser, manager_url, tmp_path, theme):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_skin_settings(page, manager_url)
        page.locator('[data-skin-option][value="soft"]').check()
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#themeToggle").click()
        page.locator('.nav-item[data-page="home"]').click()
        for action in ("stop", "start"):
            primary = page.locator("#primaryAction")
            expect(primary).to_have_attribute("data-action", action)
            for control in (primary, page.locator("#forceStopAction")):
                if not control.is_visible():
                    continue
                shape = control.evaluate("""el => {
                  const box = el.getBoundingClientRect(), css = getComputedStyle(el);
                  return {width: box.width, height: box.height, radius: parseFloat(css.borderTopLeftRadius)};
                }""")
                assert shape["width"] > shape["height"]
                assert 0 < shape["radius"] < shape["height"] / 2, shape
            page.screenshot(path=str(tmp_path / f"soft-{theme}-{action}.png"), animations="disabled")
            if action == "stop":
                primary.click()  # Preview API only; no robot process is started or stopped.

        page.locator('.nav-item[data-page="settings"]').click()
        page.locator('[data-settings-tab="system"]').click()
        expect(page.locator("#createBackup")).to_be_enabled()
        expect(page.locator("#exportDiagnostics")).to_be_enabled()
        appearances = page.locator("#createBackup, #exportDiagnostics").evaluate_all("""buttons => buttons.map(el => {
          const css = getComputedStyle(el);
          return [css.borderRadius, css.borderColor, css.backgroundColor, css.color, css.boxShadow];
        })""")
        assert appearances[0] == appearances[1]
        # Exercise the disabled appearance without altering the shared preview fixture.
        page.locator("#restoreBackup").evaluate("el => { el.disabled = true; }")
        for button in page.locator(".maintenance-row > .button").all():
            capsule = button.evaluate("""el => {
              const css = getComputedStyle(el);
              return {radius: parseFloat(css.borderTopLeftRadius), height: el.getBoundingClientRect().height,
                border: css.borderTopColor, background: css.backgroundColor};
            }""")
            assert capsule["radius"] >= capsule["height"] / 2
            assert capsule["border"] != "rgba(0, 0, 0, 0)"
            assert capsule["background"] != "rgba(0, 0, 0, 0)"
        expect(page.locator("#restoreBackup")).to_be_disabled()
        assert page.locator("#restoreBackup").evaluate("el => getComputedStyle(el).color") != appearances[0][3]

        assert not errors, errors
        page.screenshot(path=str(tmp_path / f"soft-{theme}-maintenance.png"), animations="disabled")
    finally:
        page.close()


def test_soft_typography_matches_default_on_dynamic_pages(browser, manager_url):
    page = browser.new_page(viewport={"width": 1440, "height": 900})
    try:
        open_skin_settings(page, manager_url)
        page.locator('[data-skin-option][value="soft"]').check()
        expect(page.locator("#skinStatus")).to_contain_text("已应用")
        for section, detail in (("home", None), ("groups", "#groupRows tr"),
                                ("features", '[data-feature-id="feature-group-analysis"]'),
                                ("history", ".conversation-item"), ("settings", '[data-settings-tab="core"]')):
            page.locator(f'.nav-item[data-page="{section}"]').click()
            if detail:
                page.locator(detail).first.click()
            page.evaluate("document.fonts.ready")
            differences = page.evaluate("""() => {
              const elements = [...document.querySelectorAll('body *')].filter(el =>
                [...el.childNodes].some(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim()));
              const font = el => { const css = getComputedStyle(el); return [css.fontFamily, css.fontWeight].join('|'); };
              const soft = elements.map(font);
              delete document.documentElement.dataset.skin;
              const differences = elements.flatMap((el, index) => font(el) === soft[index] ? [] :
                [{element: el.id || el.className || el.tagName, soft: soft[index], original: font(el)}]);
              document.documentElement.dataset.skin = 'soft';
              return differences;
            }""")
            assert not differences, differences
            assert page.evaluate("document.fonts.check('700 16px \"Alimama ShuHei\"')")
            assert page.evaluate("document.fonts.check('400 14px \"Alibaba PuHuiTi 2\"')")
    finally:
        page.close()
