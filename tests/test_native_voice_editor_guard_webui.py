"""Native voice drafts and pending actions reach the manager's real guards."""
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_native_voice_settings_webui import native_voice  # noqa: F401


@pytest.fixture
def capture_voice_controllers(browser, monkeypatch):
    new_page = browser.new_page

    def captured_page(*args, **kwargs):
        page = new_page(*args, **kwargs)
        page.add_init_script("""(() => {
          for (const name of ['createFeatureController', 'createUpdateNotifications']) {
            Object.defineProperty(window, name, {configurable:true, set(create) {
              Object.defineProperty(window, name, {value: helpers => {
                window.voiceGuardState = helpers.state;
                const controller = create(helpers);
                if (name === 'createUpdateNotifications') window.voiceGuardUpdates = controller;
                return controller;
              }});
            }});
          }
        })();""")
        return page

    monkeypatch.setattr(browser, "new_page", captured_page)


@pytest.fixture
def guarded_voice(capture_voice_controllers, native_voice):
    view = native_voice
    view.outer = view.page.frame_locator("#featurePageFrame")
    view.page.evaluate("""() => {
      window.addEventListener('message', event => {
        const frame = document.querySelector('#featurePageFrame'), message = event.data;
        if (!frame || event.source !== frame.contentWindow || event.origin !== location.origin
          || message?.channel !== 'astrbot-plugin-page' || message.kind !== 'editor-state') return;
        document.body.dataset.voiceEditorDirty = String(window.voiceGuardState.featurePageDirty);
        document.body.dataset.voiceEditorBusy = String(window.voiceGuardState.featurePageBusy);
        document.body.dataset.voiceEditorLastSession = String(message.session || 'missing');
      });
    }""")
    return view


def _expect_editor(view, *, dirty, busy):
    expect(view.page.locator("body")).to_have_attribute("data-voice-editor-dirty", str(dirty).lower())
    expect(view.page.locator("body")).to_have_attribute("data-voice-editor-busy", str(busy).lower())


def _expect_close_guard(view, blocked):
    dispatch = """() => {
      const event = new Event('beforeunload', {cancelable:true});
      dispatchEvent(event); return event.defaultPrevented;
    }"""
    assert view.page.evaluate(dispatch) is blocked
    assert view.outer.locator("html").evaluate(dispatch) is blocked
    assert view.frame.locator("html").evaluate(dispatch) is blocked


@pytest.mark.parametrize("target,width", [("feature", 1380), ("navigation", 430), ("refresh", 1380)])
def test_voice_draft_requires_discard_and_cancel_preserves_frame(guarded_voice, tmp_path, target, width):
    view = guarded_voice
    view.page.set_viewport_size({"width": width, "height": 920})
    view.field.fill("不能静默丢失的语音草稿")
    _expect_editor(view, dirty=True, busy=False)
    _expect_close_guard(view, True)
    view.page.evaluate("window.oldVoiceFrame = document.querySelector('#featurePageFrame').contentWindow")

    def leave():
        if target == "feature":
            view.page.locator('.feature-item[data-feature-id="feature-commands"]').click()
        elif target == "navigation":
            view.page.locator('.nav-item[data-page="settings"]').click()
        else:
            view.page.locator('#refreshFeatures').click()

    leave()
    expect(view.page.locator("#modalTitle")).to_have_text("离开前丢弃修改？")
    view.page.screenshot(path=str(tmp_path / f"voice-draft-leave-{width}.png"), animations="disabled")
    assert view.page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    view.page.locator("#modalCancel").click()
    expect(view.field).to_have_value("不能静默丢失的语音草稿")
    assert view.page.evaluate("oldVoiceFrame === document.querySelector('#featurePageFrame').contentWindow")
    leave()
    view.page.locator("#modalConfirm").click()
    expect(view.page.locator("#modalBackdrop")).to_be_hidden()
    assert not view.page.evaluate("window.voiceGuardState.featurePageDirty")
    if target == "feature":
        expect(view.page.locator("#featureDetail h2")).to_have_text("常用指令")
    elif target == "navigation":
        expect(view.page.locator("#page-settings")).to_be_visible()


@pytest.mark.parametrize("result", ["saved", "failed", "new-draft"])
def test_voice_pending_save_blocks_navigation_until_outcome(guarded_voice, result):
    view = guarded_voice
    view.field.fill("这次保存的风格")
    view.frame.locator("#save-config").click()
    expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "1")
    _expect_editor(view, dirty=True, busy=True)
    _expect_close_guard(view, True)
    view.page.locator('.feature-item[data-feature-id="feature-commands"]').click()
    view.page.locator('.nav-item[data-page="settings"]').click()
    expect(view.page.locator("#page-features")).to_be_visible()
    expect(view.page.locator("#featureDetail h2")).to_have_text("语音克隆")
    expect(view.page.locator("#modalBackdrop")).to_be_hidden()
    if result == "new-draft":
        view.field.fill("请求提交后的新风格")
    view.payload["config"] = view.writes[0]
    view.payload["success"] = False
    view.payload["error"] = "模拟读取失败"
    view.posts.pop().fulfill(json={"success": result != "failed", "config": view.writes[0],
                                  "error": "模拟保存失败"})
    expect(view.frame.locator("#save-config")).to_be_enabled()
    dirty = result != "saved"
    _expect_editor(view, dirty=dirty, busy=False)
    _expect_close_guard(view, dirty)
    view.page.locator('.feature-item[data-feature-id="feature-commands"]').click()
    if dirty:
        expect(view.page.locator("#modalTitle")).to_have_text("离开前丢弃修改？")
        view.page.locator("#modalCancel").click()
    else:
        expect(view.page.locator("#featureDetail h2")).to_have_text("常用指令")
    assert len(view.writes) == 1


def test_voice_page_reselection_keeps_current_draft(guarded_voice):
    view = guarded_voice
    view.field.fill("再次选择专属设置仍需保留")
    _expect_editor(view, dirty=True, busy=False)
    view.outer.locator('.page-button[data-page-name="settings"]').click()
    expect(view.field).to_have_value("再次选择专属设置仍需保留")
    _expect_close_guard(view, True)


@pytest.mark.parametrize("phase", ["draft", "saving", "clean-save"])
def test_voice_editor_protects_restore_and_update_install(guarded_voice, phase):
    view = guarded_voice
    pending = phase != "draft"
    dirty = phase != "clean-save"
    if dirty:
        view.field.fill("维护操作前的草稿")
    if pending:
        view.frame.locator("#save-config").click()
        expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "1")
    _expect_editor(view, dirty=dirty, busy=pending)
    _expect_close_guard(view, True)
    view.page.evaluate("""() => {
      const api = window.pywebview.api;
      window.voiceMaintenanceWrites = [];
      api.restore_backup = async () => { window.voiceMaintenanceWrites.push('restore'); return {ok:true,data:{}}; };
      api.install_update = async () => { window.voiceMaintenanceWrites.push('install'); return {ok:true,data:{accepted:true}}; };
      document.querySelector('#restoreBackup').disabled = false;
      document.querySelector('#restoreBackup').click();
    }""")
    expect(view.page.locator("#toastRegion")).to_contain_text("尚未保存" if dirty else "正在处理其他操作")
    expect(view.page.locator("#modalBackdrop")).to_be_hidden()
    view.page.evaluate("""() => {
      const release = {version:'1.0.1',release_id:'voice-guard-release',package_size:1000};
      window.voiceGuardState.availableUpdate = release;
      window.pywebview.api.get_update_status = async () => ({ok:true,data:{
        state:'ready',phase:'ready_to_install',version:release.version,release_id:release.release_id,
        confirmation_token:'voice-guard-consent'}});
      return window.voiceGuardUpdates.refreshUpdate(true);
    }""")
    expect(view.page.locator("#modalTitle")).to_have_text("安装 v1.0.1？")
    view.page.locator("#modalConfirm").click()
    expect(view.page.locator("#modalError")).to_contain_text("处理其他操作" if pending else "尚未保存")
    assert view.page.evaluate("window.voiceMaintenanceWrites") == []
    view.page.locator("#modalCancel").click()


def test_voice_editor_rejects_messages_from_wrong_origin_source_or_session(guarded_voice):
    view = guarded_voice
    view.field.fill("拒绝伪造清空状态")
    _expect_editor(view, dirty=True, busy=False)
    assert view.frame.locator("html").evaluate("""() => {
      const previous = editorSession;
      for (const [origin,source] of [['https://invalid.example',parent],[location.origin,window]]) {
        dispatchEvent(new MessageEvent('message',{origin,source,data:{
          channel:'astrbot-plugin-page',kind:'editor-context',session:'untrusted-context'}}));
      }
      return editorSession === previous;
    }""")
    view.outer.locator("html").evaluate("""() => {
      const frame = document.querySelector('#plugin-frame');
      const previousParent = parentEditorSession;
      for (const [origin,source] of [['https://invalid.example',parent],[location.origin,window]]) {
        dispatchEvent(new MessageEvent('message',{origin,source,data:{
          channel:'astrbot-plugin-page',kind:'editor-context',session:'untrusted-context'}}));
      }
      if (parentEditorSession !== previousParent) throw new Error('Untrusted manager context accepted');
      for (const [origin, source, session] of [
        ['https://invalid.example',frame.contentWindow,editorSession],
        [location.origin,window,editorSession],
        [location.origin,frame.contentWindow,'old-document-session']]) {
        dispatchEvent(new MessageEvent('message',{origin,source,data:{
          channel:'astrbot-plugin-page',kind:'editor-state',session,dirty:false,busy:false}}));
      }
    }""")
    parent_session = view.outer.locator("html").evaluate("() => parentEditorSession")
    view.page.evaluate("""session => {
      const frame = document.querySelector('#featurePageFrame');
      for (const [origin, source, featureId, dirty] of [
        ['https://invalid.example',frame.contentWindow,'feature-voice-clone',false],
        [location.origin,window,'feature-voice-clone',false],
        [location.origin,frame.contentWindow,'feature-commands',false],
        [location.origin,frame.contentWindow,'feature-voice-clone','false']]) {
        dispatchEvent(new MessageEvent('message',{origin,source,data:{
          channel:'astrbot-plugin-page',kind:'editor-state',session,featureId,dirty,busy:false}}));
      }
    }""", parent_session)
    _expect_close_guard(view, True)
    assert view.page.evaluate("window.voiceGuardState.featurePageDirty")


@pytest.mark.parametrize("pending", [False, True])
def test_outer_reload_rejects_previous_document_editor_state(guarded_voice, pending):
    view = guarded_voice
    old_session = view.outer.locator("html").evaluate("() => parentEditorSession")
    assert old_session
    outer_document = view.page.locator("#featurePageFrame").element_handle().content_frame()
    view.page.evaluate("window.previousOuterFrame = document.querySelector('#featurePageFrame').contentWindow")
    with view.page.expect_event("framenavigated", predicate=lambda frame: frame == outer_document):
        view.page.locator("#featurePageFrame").evaluate("frame => { frame.src = frame.src; }")
    outer_document.wait_for_load_state("load")
    expect(view.field).to_have_value("已保存的风格")
    assert view.page.evaluate("previousOuterFrame === document.querySelector('#featurePageFrame').contentWindow")
    assert view.outer.locator("html").evaluate("() => parentEditorSession") != old_session

    view.field.fill("新载入页面的草稿")
    if pending:
        view.frame.locator("#save-config").click()
        expect(view.frame.locator("#save-config")).to_be_disabled()
    _expect_editor(view, dirty=True, busy=pending)
    # Keep the real WindowProxy while delivering a previous document's state.
    # A missing token must also fail closed; only current-session updates apply.
    view.outer.locator("html").evaluate("""previous => {
      for (const session of [previous, undefined]) {
        parent.postMessage({channel:'astrbot-plugin-page',kind:'editor-state',
          featureId:'feature-voice-clone',session,dirty:false,busy:false},location.origin);
      }
    }""", old_session)
    expect(view.page.locator("body")).to_have_attribute("data-voice-editor-last-session", "missing")
    _expect_editor(view, dirty=True, busy=pending)
    _expect_close_guard(view, True)
    assert view.page.evaluate("window.voiceGuardState.featurePageDirty")
    assert view.page.evaluate("window.voiceGuardState.featurePageBusy") is pending


@pytest.mark.parametrize("pending", [False, True])
def test_voice_standalone_shell_keeps_dirty_or_pending_page(native_voice, pending):
    view = native_voice
    address = urlsplit(view.page.url)._replace(path="", query="").geturl()
    view.page.goto(address + "/plugin-center?feature=feature-voice-clone")
    frame = view.page.frame_locator("#plugin-frame")
    field = frame.locator("#default-context")
    expect(field).to_have_value("已保存的风格")
    writes = []
    view.page.route("**/control/plugins/*/reload", lambda route: (
        writes.append(route.request.url), route.fulfill(json={"ok": True, "data": {}})))
    field.fill("独立设置页的草稿")
    if pending:
        frame.locator("#save-config").click()
        expect(frame.locator("#save-config")).to_be_disabled()
    assert view.page.evaluate("""() => {
      const event = new Event('beforeunload', {cancelable:true});
      dispatchEvent(event); return event.defaultPrevented;
    }""")
    # Native-page headers hide this fallback button; its handler must also
    # reject a programmatic event while the editor still owns a draft/action.
    view.page.locator("#reload-plugin").dispatch_event("click")
    assert writes == []
    view.page.locator('.page-button[data-page-name="settings"]').click()
    expect(field).to_have_value("独立设置页的草稿")
    assert len(view.writes) == int(pending)
