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
from wechatrebot_manager.plugin_center import _public_plugin_id


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
    feature_id = _public_plugin_id(module)
    detail = {"id": feature_id, "display_name": "情感与关系",
              "pages": ["dashboard"], "enabled": True, "engine_online": True,
              "configurable": False, "config": {}, "revision": "relationship-test", "schema": {}}
    commands = {"id": "feature-commands", "display_name": "常用指令", "enabled": True,
                "engine_online": True, "configurable": False, "config": {}, "schema": {}}
    view = SimpleNamespace(writes=[], posts=[], reads=[], read_count=0, held_count=0, hold_read=False,
                           reloads=[], feature_id=feature_id, config={"a": "已保存甲", "b": "已保存乙"})
    page = browser.new_page(viewport={"width": 1380, "height": 920})
    page.add_init_script("""(() => {
      for (const name of ['createFeatureController', 'createUpdateNotifications']) {
        Object.defineProperty(window, name, {configurable:true, set(create) {
          Object.defineProperty(window, name, {value: helpers => {
            window.relationshipGuardState = helpers.state;
            const controller = create(helpers);
            if (name === 'createUpdateNotifications') window.relationshipGuardUpdates = controller;
            if (name === 'createFeatureController') window.addEventListener('message', event => {
              const frame = document.querySelector('#featurePageFrame'), message = event.data;
              if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin
                || message?.channel !== 'astrbot-plugin-page' || message.kind !== 'editor-state') return;
              document.body.dataset.relationshipEditorDirty = String(helpers.state.featurePageDirty);
              document.body.dataset.relationshipEditorBusy = String(helpers.state.featurePageBusy);
            });
            return controller;
          }});
        }});
      }
      window.addEventListener('message', event => {
        const message = event.data;
        if (event.origin !== location.origin || message?.channel !== 'astrbot-plugin-page'
          || message.kind !== 'editor-state') return;
        const child = document.querySelector('#plugin-frame');
        if (child && event.source === child.contentWindow) {
          document.body.dataset.relationshipChildSession = message.session;
          document.body.dataset.relationshipChildCount = String(Number(document.body.dataset.relationshipChildCount || 0) + 1);
          document.body.dataset.relationshipChildDirty = String(message.dirty);
          document.body.dataset.relationshipChildBusy = String(message.busy);
        }
      });
    })();""")
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
            route.fulfill(json={"ok": True, "data": {"plugins": [detail, commands], "engine_online": True}})
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
        elif path.endswith("/reload"):
            view.reloads.append(route.request.url)
            route.fulfill(json={"ok": True, "data": {}})
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
        page.evaluate("""({detail, commands}) => {
          window.pywebview.api.get_features = async () => ({ok: true, data: {plugins: [detail, commands], engine_online: true}});
          window.pywebview.api.get_feature = async id => ({ok: true, data: id === detail.id ? detail : commands});
          window.pywebview.api.get_feature_page = async () => ({ok: true, data: {
            url: location.origin + '/plugin-center?feature=' + encodeURIComponent(detail.id) + '&embedded=1', origin: location.origin}});
        }""", {"detail": detail, "commands": commands})
        page.locator('.nav-item[data-page="features"]').click()
        page.locator(f'.feature-item[data-feature-id="{feature_id}"]').click()
        view.page = page
        view.outer = page.frame_locator("#featurePageFrame")
        view.frame = view.outer.frame_locator("#plugin-frame")
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


def expect_editor(view, *, dirty, busy):
    expect(view.page.locator("body")).to_have_attribute("data-relationship-editor-dirty", str(dirty).lower())
    expect(view.page.locator("body")).to_have_attribute("data-relationship-editor-busy", str(busy).lower())


def expect_close_guard(view, blocked):
    dispatch = """() => {
      const event = new Event('beforeunload', {cancelable:true});
      dispatchEvent(event); return event.defaultPrevented;
    }"""
    assert view.page.evaluate(dispatch) is blocked
    assert view.outer.locator("html").evaluate(dispatch) is blocked
    assert view.frame.locator("html").evaluate(dispatch) is blocked


def test_relationship_save_blocks_duplicate_and_clears_only_submitted_drafts(relationship_settings):
    view = relationship_settings
    a = view.frame.locator('[data-config-field="a"]')
    a.fill("已提交甲")
    a.dispatch_event("change")
    submit(view, 1)
    expect_editor(view, dirty=True, busy=True)
    expect(view.frame.locator("#config-save-button")).to_be_disabled()
    view.frame.locator("#config-save-button").dispatch_event("click")
    assert len(view.writes) == 1
    assert view.writes[0]["config"] == {"a": "已提交甲"}
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    expect_editor(view, dirty=False, busy=False)
    expect_close_guard(view, False)
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
def test_relationship_save_preserves_unblurred_drafts_across_groups(relationship_settings, tmp_path, phase):
    view = relationship_settings
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
    expect_editor(view, dirty=True, busy=True)
    if phase == "post":
        accept(view)
    else:
        view.reads.pop().fulfill(json=view.payload())
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    expect_editor(view, dirty=True, busy=False)
    expect(b).to_have_value("后续乙")
    view.frame.locator('[data-settings-group="a"]').click()
    expect(a).to_have_value("后续甲")
    assert len(view.writes) == 1
    view.page.screenshot(path=str(tmp_path / f"relationship-drafts-{phase}.png"), animations="disabled")
    view.hold_read = False
    submit(view, 2)
    assert view.writes[1]["config"] == {"a": "后续甲", "b": "后续乙"}
    accept(view)
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    expect_editor(view, dirty=False, busy=False)


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
    expect_editor(view, dirty=True, busy=False)
    expect_close_guard(view, True)
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
    expect_editor(view, dirty=True, busy=False)
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
    expect_editor(view, dirty=False, busy=False)
    expect_close_guard(view, False)
    assert len(view.writes) == 1


@pytest.mark.parametrize("target", ["feature", "navigation", "refresh"])
def test_relationship_draft_requires_discard_and_cancel_keeps_frame(relationship_settings, target):
    view = relationship_settings
    field = view.frame.locator('[data-config-field="a"]')
    field.fill("离开前还未保存的设置")
    expect_editor(view, dirty=True, busy=False)
    expect_close_guard(view, True)
    view.page.evaluate("window.previousRelationshipFrame = document.querySelector('#featurePageFrame').contentWindow")

    def leave():
        if target == "feature":
            view.page.locator('.feature-item[data-feature-id="feature-commands"]').click()
        elif target == "navigation":
            view.page.locator('.nav-item[data-page="settings"]').click()
        else:
            view.page.locator("#refreshFeatures").click()

    leave()
    expect(view.page.locator("#modalTitle")).to_have_text("离开前丢弃修改？")
    view.page.locator("#modalCancel").click()
    expect(field).to_have_value("离开前还未保存的设置")
    assert view.page.evaluate("previousRelationshipFrame === document.querySelector('#featurePageFrame').contentWindow")
    leave()
    view.page.locator("#modalConfirm").click()
    expect(view.page.locator("#modalBackdrop")).to_be_hidden()
    assert not view.page.evaluate("window.relationshipGuardState.featurePageDirty")
    if target == "feature":
        expect(view.page.locator("#featureDetail h2")).to_have_text("常用指令")
    elif target == "navigation":
        expect(view.page.locator("#page-settings")).to_be_visible()


@pytest.mark.parametrize("phase", ["post", "read"])
def test_relationship_pending_save_blocks_navigation_reload_and_maintenance(relationship_settings, phase):
    view = relationship_settings
    view.frame.locator('[data-config-field="a"]').fill("本次需要保存的设置")
    submit(view, 1)
    if phase == "read":
        view.hold_read = True
        accept(view)
        expect(view.page.locator("body")).to_have_attribute("data-relationship-held", "1")
    dirty = phase == "post"
    expect_editor(view, dirty=dirty, busy=True)
    expect_close_guard(view, True)
    view.page.locator('.feature-item[data-feature-id="feature-commands"]').click()
    view.page.locator('.nav-item[data-page="settings"]').click()
    view.page.locator("#refreshFeatures").click()
    view.outer.locator("#reload-plugin").dispatch_event("click")
    expect(view.page.locator("#page-features")).to_be_visible()
    expect(view.page.locator("#featureDetail h2")).to_have_text("情感与关系")
    expect(view.frame.locator('[data-config-field="a"]')).to_have_value("本次需要保存的设置")
    assert view.reloads == []
    view.page.evaluate("""() => {
      const api = window.pywebview.api;
      window.relationshipMaintenanceWrites = [];
      api.restore_backup = async () => { window.relationshipMaintenanceWrites.push('restore'); return {ok:true,data:{}}; };
      api.install_update = async () => { window.relationshipMaintenanceWrites.push('install'); return {ok:true,data:{accepted:true}}; };
      document.querySelector('#restoreBackup').disabled = false;
      document.querySelector('#restoreBackup').click();
    }""")
    expect(view.page.locator("#toastRegion")).to_contain_text("尚未保存" if dirty else "正在处理其他操作")
    expect(view.page.locator("#modalBackdrop")).to_be_hidden()
    view.page.evaluate("""() => {
      const release = {version:'1.0.1',release_id:'relationship-guard-release',package_size:1000};
      window.relationshipGuardState.availableUpdate = release;
      window.pywebview.api.get_update_status = async () => ({ok:true,data:{
        state:'ready',phase:'ready_to_install',version:release.version,release_id:release.release_id,
        confirmation_token:'relationship-guard-consent'}});
      return window.relationshipGuardUpdates.refreshUpdate(true);
    }""")
    expect(view.page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    view.page.locator("#modalConfirm").click()
    expect(view.page.locator("#modalError")).to_contain_text("处理其他操作")
    assert view.page.evaluate("window.relationshipMaintenanceWrites") == []
    view.page.locator("#modalCancel").click()
    if phase == "post":
        accept(view)
    else:
        view.reads.pop().fulfill(json=view.payload())
    expect(view.frame.locator("#config-save-button")).to_be_enabled()
    expect_editor(view, dirty=False, busy=False)
    expect_close_guard(view, False)
    assert len(view.writes) == 1


def test_relationship_child_rejects_untrusted_or_invalid_editor_context(relationship_settings):
    view = relationship_settings
    field = view.frame.locator('[data-config-field="a"]')
    field.fill("当前可信会话的草稿")
    expect_editor(view, dirty=True, busy=False)
    session = view.outer.locator("body").get_attribute("data-relationship-child-session")
    assert session
    count = view.outer.locator("body").get_attribute("data-relationship-child-count")
    view.frame.locator("html").evaluate("""() => {
      for (const [origin,source,session] of [
        ['https://invalid.example',parent,'wrong-origin'],
        [location.origin,window,'wrong-source'],
        [location.origin,parent,''], [location.origin,parent,'x'.repeat(81)],
        [location.origin,parent,undefined], [location.origin,parent,123]]) {
        dispatchEvent(new MessageEvent('message', {origin,source,data:{
          channel:'astrbot-plugin-page',kind:'editor-context',session}}));
      }
    }""")
    field.fill("拒绝非法上下文后继续编辑")
    expect(view.outer.locator("body")).not_to_have_attribute("data-relationship-child-count", count)
    expect(view.outer.locator("body")).to_have_attribute("data-relationship-child-session", session)
    expect_editor(view, dirty=True, busy=False)
    expect_close_guard(view, True)


@pytest.mark.parametrize("pending", [False, True])
def test_relationship_standalone_shell_protects_settings(relationship_settings, pending):
    view = relationship_settings
    address = urlsplit(view.page.url)._replace(path="", query="").geturl()
    view.page.goto(address + f"/plugin-center?feature={view.feature_id}")
    frame = view.page.frame_locator("#plugin-frame")
    field = frame.locator('[data-config-field="a"]')
    expect(field).to_have_value("已保存甲")
    field.fill("独立配置页未保存的设置")
    if pending:
        frame.locator("#config-save-button").click()
        expect(frame.locator("#config-save-button")).to_be_disabled()
    expect(view.page.locator("body")).to_have_attribute("data-relationship-child-dirty", "true")
    expect(view.page.locator("body")).to_have_attribute("data-relationship-child-busy", str(pending).lower())
    assert view.page.evaluate("""() => {
      const event = new Event('beforeunload', {cancelable:true});
      dispatchEvent(event); return event.defaultPrevented;
    }""")
    view.page.locator("#reload-plugin").dispatch_event("click")
    view.page.locator('.page-button[data-page-name="dashboard"]').click()
    expect(field).to_have_value("独立配置页未保存的设置")
    assert view.reloads == []
    assert len(view.writes) == int(pending)
