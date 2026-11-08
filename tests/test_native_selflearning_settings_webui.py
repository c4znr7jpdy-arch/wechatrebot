"""Keep per-field drafts and save receipts in the assembled relationship page."""
from __future__ import annotations

import copy
import shutil
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url
from test_native_voice_settings_webui import _PAGE_SDK
from test_plugin_portability import PROJECT_PLUGINS, _patched


@pytest.fixture
def relationship_settings(browser, manager_url, tmp_path):
    module = "astrbot_plugin_self_learning"
    source = PROJECT_PLUGINS / module / "pages/dashboard"
    assembled = tmp_path / "assembled"
    target = assembled / "pages/dashboard/app.js"
    target.parent.mkdir(parents=True)
    shutil.copy2(source / "app.js", target)
    script = _patched(assembled, module, "pages/dashboard/app.js", all_patches=True)
    html = (source / "index.html").read_text("utf-8").replace(
        "</body>", '<script src="./test-page-sdk.js"></script></body>'
    )
    detail = {"id": "feature-self-learning", "display_name": "情感与关系",
              "pages": ["dashboard"], "enabled": True, "engine_online": True,
              "configurable": False, "config": {}, "revision": "relationship-test", "schema": {}}
    view = SimpleNamespace(writes=[], posts=[], reads=[], read_count=0, held_count=0, hold_read=False,
                           config={"a": "已保存甲", "b": "已保存乙"})
    page = browser.new_page(viewport={"width": 1380, "height": 920})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def payload():
        return {"config": copy.deepcopy(view.config), "schema": {"groups": [
            {"key": key, "title": f"分组{key}", "fields": [
                {"key": key, "label": key, "value": value, "type": "string", "editable": True},
                {"key": "readonly", "label": "只读", "value": "不能提交", "type": "string", "editable": False},
            ]} for key, value in view.config.items()
        ]}}

    view.payload = payload

    def control(route):
        path = urlsplit(route.request.url).path
        if path == "/control/catalog":
            route.fulfill(json={"ok": True, "data": {"plugins": [detail], "engine_online": True}})
        elif path.endswith("/pages/dashboard/entry"):
            route.fulfill(json={"ok": True, "data": {"content_path": "/__native_relationship/index.html#/settings"}})
        elif path.endswith("/extension/page/settings/action"):
            view.writes.append(route.request.post_data_json)
            view.posts.append(route)
            page.locator("body").evaluate("(body, count) => { body.dataset.relationshipWrites = String(count); }", len(view.writes))
        elif path.endswith("/extension/page/settings"):
            view.read_count += 1
            page.locator("body").evaluate("(body, count) => { body.dataset.relationshipReads = String(count); }", view.read_count)
            if view.hold_read:
                view.reads.append(route)
                view.held_count += 1
                page.locator("body").evaluate("(body, count) => { body.dataset.relationshipHeld = String(count); }", view.held_count)
            else:
                route.fulfill(json=payload())
        elif "/extension/" in path:
            route.fulfill(json={"overview": {"runtime": {}, "errors": {}}, "errors": {}})
        else:
            route.fulfill(json={"ok": True, "data": detail})

    def assets(route):
        name = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
        if name == "index.html":
            route.fulfill(content_type="text/html; charset=utf-8", body=html)
        elif name in {"app.js", "test-page-sdk.js"}:
            route.fulfill(content_type="text/javascript", body=script if name == "app.js" else _PAGE_SDK)
        elif name == "styles.css":
            route.fulfill(content_type="text/css", path=str(source / name))
        else:
            route.abort()

    page.route("**/control/**", control)
    page.route("**/__native_relationship/**", assets)
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""detail => {
          window.pywebview.api.get_features = async () => ({ok: true, data: {plugins: [detail], engine_online: true}});
          window.pywebview.api.get_feature = async () => ({ok: true, data: detail});
          window.pywebview.api.get_feature_page = async () => ({ok: true, data: {
            url: location.origin + '/plugin-center?feature=feature-self-learning&embedded=1', origin: location.origin}});
        }""", detail)
        page.locator('.nav-item[data-page="features"]').click()
        page.locator('.feature-item[data-feature-id="feature-self-learning"]').click()
        view.page = page
        view.frame = page.frame_locator("#featurePageFrame").frame_locator("#plugin-frame")
        expect(view.frame.locator('[data-config-field="a"]')).to_have_value("已保存甲")
        yield view
        assert not errors, errors
    finally:
        for route in [*view.posts, *view.reads]:
            route.abort()
        page.close()


def submit(view, count):
    view.frame.locator("#config-save-button").click()
    expect(view.page.locator("body")).to_have_attribute("data-relationship-writes", str(count))


def accept(view):
    view.config.update(view.writes[-1]["config"])
    view.posts.pop().fulfill(json={"success": True, "message": "设置已保存", "config": copy.deepcopy(view.config)})


def test_relationship_save_blocks_duplicate_and_clears_only_submitted_drafts(relationship_settings):
    view = relationship_settings
    a = view.frame.locator('[data-config-field="a"]')
    a.fill("已提交甲")
    a.dispatch_event("change")
    submit(view, 1)
    expect(view.frame.locator("#config-save-button")).to_be_disabled()
    view.frame.locator("#config-save-button").dispatch_event("click")
    assert len(view.writes) == 1
    assert view.writes[0]["config"] == {"a": "已提交甲"}
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    view.config["a"] = "后来更新的甲"
    view.frame.locator("#refresh-button").click()
    expect(a).to_have_value("后来更新的甲")
    view.frame.locator('[data-settings-group="b"]').click()
    view.frame.locator('[data-config-field="b"]').fill("新乙")
    submit(view, 2)
    assert view.writes[1]["config"] == {"b": "新乙"}
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()


@pytest.mark.parametrize("phase", ["post", "read"])
@pytest.mark.parametrize("width", [1380, 430])
def test_relationship_save_preserves_unblurred_drafts_across_groups(relationship_settings, tmp_path, phase, width):
    view = relationship_settings
    view.page.set_viewport_size({"width": width, "height": 920})
    a = view.frame.locator('[data-config-field="a"]')
    a.fill("本次甲")
    submit(view, 1)
    view.hold_read = phase == "read"
    if phase == "read":
        accept(view)
        expect(view.page.locator("body")).to_have_attribute("data-relationship-held", "1")
    a.fill("后续甲")
    view.frame.locator('[data-settings-group="b"]').click()
    b = view.frame.locator('[data-config-field="b"]')
    b.fill("后续乙")
    if phase == "post":
        accept(view)
    else:
        view.reads.pop().fulfill(json=view.payload())
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    expect(b).to_have_value("后续乙")
    view.frame.locator('[data-settings-group="a"]').click()
    expect(a).to_have_value("后续甲")
    assert len(view.writes) == 1
    view.page.screenshot(path=str(tmp_path / f"relationship-drafts-{phase}-{width}.png"), animations="disabled")
    view.hold_read = False
    submit(view, 2)
    assert view.writes[1]["config"] == {"a": "后续甲", "b": "后续乙"}
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()


def test_relationship_write_failure_keeps_draft_and_can_retry(relationship_settings):
    view = relationship_settings
    field = view.frame.locator('[data-config-field="a"]')
    field.fill("未保存甲")
    reads = view.read_count
    submit(view, 1)
    view.posts.pop().fulfill(json={"success": False, "message": "模拟保存失败"})
    expect(view.frame.locator("#toast-region")).to_contain_text("模拟保存失败")
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    expect(field).to_have_value("未保存甲")
    assert view.read_count == reads
    submit(view, 2)
    assert view.writes[1]["config"] == {"a": "未保存甲"}
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()


def test_relationship_saved_readback_failure_only_retries_reads(relationship_settings):
    view = relationship_settings
    a = view.frame.locator('[data-config-field="a"]')
    a.fill("本次甲")
    submit(view, 1)
    view.hold_read = True
    accept(view)
    expect(view.page.locator("body")).to_have_attribute("data-relationship-held", "1")
    a.fill("后续甲")
    view.reads.pop().fulfill(json={"success": False, "message": "模拟回读失败"})
    expect(view.frame.locator("#toast-region")).to_contain_text("设置已保存")
    expect(a).to_have_value("后续甲")
    expect(a).to_be_disabled()
    expect(view.frame.locator("#config-save-button")).to_be_disabled()
    view.frame.locator('[data-settings-group="b"]').click()
    expect(view.frame.locator('[data-config-field="b"]')).to_be_disabled()
    view.frame.locator("#config-save-button").dispatch_event("click")
    assert len(view.writes) == 1
    view.frame.locator('.nav-item[data-page="settings"]').click()
    expect(view.page.locator("body")).to_have_attribute("data-relationship-held", "2")
    view.reads.pop().fulfill(json={"success": False, "message": "仍无法读取"})
    expect(view.frame.locator("#settings-warnings")).to_contain_text("读取失败")
    expect(view.frame.locator("#config-save-button")).to_be_disabled()
    assert len(view.writes) == 1
    view.hold_read = False
    view.frame.locator("#refresh-button").click()
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    view.frame.locator('[data-settings-group="a"]').click()
    expect(a).to_have_value("后续甲")
    expect(a).to_be_enabled()
    assert len(view.writes) == 1


def test_relationship_old_refresh_cannot_replace_successful_save_read(relationship_settings):
    view = relationship_settings
    old = view.payload()
    view.hold_read = True
    view.frame.locator("#refresh-button").click()
    expect(view.page.locator("body")).to_have_attribute("data-relationship-held", "1")
    view.frame.locator('[data-config-field="a"]').fill("这次保存的甲")
    submit(view, 1)
    view.hold_read = False
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    with view.page.expect_response(lambda response: urlsplit(response.url).path.endswith("/extension/page/settings")) as response:
        view.reads.pop().fulfill(json=old)
    response.value.finished()
    view.frame.locator("html").evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    view.frame.locator('[data-settings-group="b"]').click()
    view.frame.locator('[data-settings-group="a"]').click()
    expect(view.frame.locator('[data-config-field="a"]')).to_have_value("这次保存的甲")
    assert len(view.writes) == 1


def test_relationship_readback_failure_shows_canonical_save_receipt(relationship_settings):
    view = relationship_settings
    field = view.frame.locator('[data-config-field="a"]')
    field.fill("  提交前的甲  ")
    submit(view, 1)
    view.hold_read = True
    view.config["a"] = "保存收据中的甲"
    view.posts.pop().fulfill(json={"success": True, "config": copy.deepcopy(view.config)})
    expect(view.page.locator("body")).to_have_attribute("data-relationship-held", "1")
    view.reads.pop().fulfill(json={"success": False, "message": "读取失败"})
    expect(field).to_have_value("保存收据中的甲")
    expect(field).to_be_disabled()
    expect(view.frame.locator("#toast-region")).to_contain_text("设置已保存")
    assert len(view.writes) == 1
