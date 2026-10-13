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
        expect(page.locator("#globalRefresh")).to_be_enabled()
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
