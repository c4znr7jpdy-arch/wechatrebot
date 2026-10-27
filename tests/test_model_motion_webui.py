"""Model card movement through shipped preview assets, without a robot."""
import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_model_layout_webui import mock_model_cards


def open_models(page, url):
    mock_model_cards(page)
    page.add_init_script("""(() => {
      window.gridAnimations = [];
      const animate = Element.prototype.animate;
      Element.prototype.animate = function(...args) {
        const animation = animate.apply(this, args);
        if (this.hasAttribute('data-motion-key')) {
          animation.pause();
          window.gridAnimations.push(animation);
        }
        return animation;
      };
    })();""")
    page.goto(url)
    expect(page.locator("#versionText")).not_to_have_text("v0.0.0")
    page.locator('.nav-item[data-page="settings"]').click()
    expect(page.locator(".provider-service")).to_have_count(7)
    settle_frames(page)
    assert page.evaluate("window.gridAnimations.length") == 0


def settle_frames(page):
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")


def finish_motion(page):
    page.evaluate("""() => {
      window.gridAnimations.forEach(animation => {
        if (animation.playState !== 'idle') animation.finish();
      });
      window.gridAnimations = [];
    }""")
    settle_frames(page)
    assert page.locator("[data-motion-key]").evaluate_all(
        "nodes => nodes.every(node => getComputedStyle(node).translate === 'none')"
    )


@pytest.mark.parametrize("skin", ["default", "soft"])
def test_model_reflow_moves_smoothly_and_repeated_resize_keeps_one_animation(browser, manager_url, skin):
    page = browser.new_page(viewport={"width": 1504, "height": 861})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_models(page, manager_url + f"&style={skin}")
        page.set_viewport_size({"width": 1380, "height": 861})
        page.wait_for_function("() => window.gridAnimations.length > 0")
        sample = page.evaluate("""() => {
          const animation = window.gridAnimations.find(a => a.playState !== 'idle');
          animation.currentTime = animation.effect.getTiming().duration / 3;
          return {id: animation.id, translate: getComputedStyle(animation.effect.target).translate};
        }""")
        assert sample["id"] == "manager-grid-move"
        assert sample["translate"] not in {"none", "0px", "0px 0px"}
        # Repeated resize replaces each in-flight movement, rather than stacking it.
        for width in (1600, 1080, 1504):
            page.set_viewport_size({"width": width, "height": 861})
            settle_frames(page)
            assert page.locator("[data-motion-key]").evaluate_all("""nodes => nodes.every(node =>
              node.getAnimations().filter(a => a.id === 'manager-grid-move').length <= 1)
            """)
        finish_motion(page)
        geometry = page.locator(".provider-service-cards").evaluate("""grid => {
          const cards = [...grid.children].map(card => card.getBoundingClientRect());
          return cards.filter(card => Math.abs(card.top - cards[0].top) < 1).length;
        }""")
        assert geometry == 3
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        assert not errors, errors
    finally:
        page.close()


def test_model_reorder_delete_and_hidden_pages_do_not_keep_motion(browser, manager_url):
    page = browser.new_page(viewport={"width": 1504, "height": 861})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    try:
        open_models(page, manager_url)
        page.locator('.provider-service footer [data-provider-default="custom/3"]').click()
        expect(page.locator(".provider-service.active")).to_have_attribute("data-motion-key", "provider:custom/3")
        page.wait_for_function("() => window.gridAnimations.length > 0")
        assert page.evaluate("window.gridAnimations.some(a => a.effect.target.dataset.motionKey === 'provider:custom/3')")
        finish_motion(page)

        page.locator('.provider-service footer [data-provider-delete="custom/1"]').click()
        page.locator("#modalConfirm").click()
        expect(page.locator(".provider-service")).to_have_count(6)
        page.wait_for_function("() => window.gridAnimations.length > 0")
        assert page.evaluate("window.gridAnimations.some(a => a.effect.target.dataset.motionKey === 'provider-add')")
        # Leaving during a paused move cancels it and does not replay on return.
        page.locator('.nav-item[data-page="home"]').click()
        settle_frames(page)
        assert page.evaluate("window.gridAnimations.every(a => a.playState === 'idle')")
        page.evaluate("window.gridAnimations = []")
        page.locator('.nav-item[data-page="settings"]').click()
        settle_frames(page)
        assert page.evaluate("window.gridAnimations.length") == 0
        page.locator('.provider-service footer [data-provider-edit]').first.click()
        page.set_viewport_size({"width": 1380, "height": 861})
        page.locator("#closeProviderEditor").click()
        settle_frames(page)
        assert page.evaluate("window.gridAnimations.length") == 0
        assert not errors, errors
    finally:
        page.close()


def test_model_motion_obeys_reduced_motion_and_cancels_an_existing_move(browser, manager_url):
    page = browser.new_page(viewport={"width": 1504, "height": 861}, reduced_motion="reduce")
    try:
        open_models(page, manager_url + "&style=soft")
        page.set_viewport_size({"width": 1380, "height": 861})
        page.locator('.provider-service footer [data-provider-default="custom/3"]').click()
        expect(page.locator(".provider-service.active")).to_have_attribute("data-motion-key", "provider:custom/3")
        settle_frames(page)
        assert page.evaluate("window.gridAnimations.length") == 0
        page.emulate_media(reduced_motion="no-preference")
        page.set_viewport_size({"width": 1504, "height": 861})
        page.wait_for_function("() => window.gridAnimations.length > 0")
        page.emulate_media(reduced_motion="reduce")
        settle_frames(page)
        assert page.evaluate("window.gridAnimations.every(a => a.playState === 'idle')")
        assert page.locator("[data-motion-key]").evaluate_all(
            "nodes => nodes.every(node => getComputedStyle(node).translate === 'none')"
        )
    finally:
        page.close()
