"""Pointer lifecycle regressions in the manager preview; no robot is started."""
import re

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401


@pytest.mark.parametrize("finish", ["release", "blur", "cancel", "detach", "lost"])
def test_panel_edge_resize_always_restores_pointer(browser, manager_url, finish):
    page = browser.new_page(viewport={"width": 1440, "height": 1000})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.locator('.nav-item[data-page="groups"]').click()
        page.locator("#groupRows tr").first.click()
        panel = page.locator("#groupInspector")
        edge = panel.locator('[data-resize-axis="width"]')
        expect(edge).to_be_attached()
        assert edge.evaluate("e => getComputedStyle(e).backgroundImage") == "none"
        assert edge.evaluate("e => getComputedStyle(e).cursor") == "ew-resize"
        rect = edge.bounding_box()
        before = panel.bounding_box()["width"]
        page.mouse.move(rect["x"] + rect["width"] / 2, rect["y"] + 70)
        page.mouse.down()
        page.mouse.move(rect["x"] - 55, rect["y"] + 70, steps=5)
        expect(page.locator("html")).to_have_class(re.compile("panel-resizing"))
        assert panel.bounding_box()["width"] < before - 30
        if finish == "blur":
            page.evaluate("window.dispatchEvent(new Event('blur'))")
        elif finish == "cancel":
            edge.dispatch_event("pointercancel", {"pointerId": 1})
        elif finish == "detach":
            edge.evaluate("e => e.remove()")
        elif finish == "lost":
            edge.evaluate("e => e.releasePointerCapture(1)")
            page.mouse.move(rect["x"] - 56, rect["y"] + 70)
        else:
            page.mouse.up()
        expect(page.locator("html")).not_to_have_class(re.compile("panel-resizing"))
        page.mouse.up()
        page.mouse.move(300, 30)
        assert page.locator("html").evaluate("e => getComputedStyle(e).cursor") != "ew-resize"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
    finally:
        page.close()


@pytest.mark.parametrize("width,height,theme", [(1380, 860, "light"), (1380, 860, "dark"), (1080, 680, "dark")])
def test_feature_savebar_covers_panel_bottom_while_scrolling(browser, manager_url, tmp_path, width, height, theme):
    page = browser.new_page(viewport={"width": width, "height": height})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        page.goto(manager_url)
        expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        if page.locator("html").get_attribute("data-theme") != theme:
            page.locator("#themeToggle").click()
        page.locator('.nav-item[data-page="features"]').click()
        page.locator('[data-feature-id="feature-group-analysis"]').first.click()
        expect(page.locator("#saveFeature")).to_be_enabled()
        panel = page.locator("#featureDetail")
        for fraction in [0, 0.5, 1]:
            geometry = panel.evaluate("""(element, fraction) => {
              element.scrollTop = (element.scrollHeight - element.clientHeight) * fraction;
              const panel = element.getBoundingClientRect();
              const bar = element.querySelector('.feature-savebar').getBoundingClientRect();
              return {
                gap: panel.top + element.clientTop + element.clientHeight - bar.bottom,
                scrollTop: element.scrollTop,
                overflow: getComputedStyle(element).overflowY,
              };
            }""", fraction)
            if geometry["overflow"] == "auto":
                assert abs(geometry["gap"]) <= 1, geometry
            if fraction == 0:
                page.screenshot(path=str(tmp_path / f"feature-savebar-{width}-{theme}.png"))
        expect(page.locator("#featureForm .config-input").last).to_be_in_viewport()
        expect(page.locator("#saveFeature")).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
    finally:
        page.close()
