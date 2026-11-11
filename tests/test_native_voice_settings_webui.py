"""Exercise assembled native voice settings through the real manager frames."""
from __future__ import annotations

import shutil
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url
from test_plugin_portability import PROJECT_PLUGINS, _patched
from wechatrebot_manager.plugin_center import _public_plugin_id


# Model the AstrBot page SDK transport; the manager and plugin-center listeners
# and their HTTP bridge run unchanged. No robot, TTS service or live writes run.
_PAGE_SDK = """(() => {
  const calls = new Map(); let sequence = 0;
  function request(action, endpoint, body, params) {
    const requestId = 'voice-test-' + (++sequence);
    return new Promise((resolve, reject) => {
      calls.set(requestId, {resolve, reject});
      parent.postMessage({channel: 'astrbot-plugin-page', kind: 'request',
        requestId, action, endpoint, body, params}, location.origin);
    });
  }
  window.addEventListener('message', event => {
    const message = event.data;
    if (event.source !== parent || event.origin !== location.origin ||
        message?.channel !== 'astrbot-plugin-page' || message.kind !== 'response') return;
    const call = calls.get(message.requestId);
    if (!call) return;
    calls.delete(message.requestId);
    message.ok ? call.resolve(message.data) : call.reject(new Error(message.error));
  });
  window.AstrBotPluginPage = {ready: async () => ({}),
    apiGet: (endpoint, params) => request('api:get', endpoint, undefined, params),
    apiPost: (endpoint, body) => request('api:post', endpoint, body),
    upload: async () => { throw new Error('Upload is outside this test'); }};
  parent.postMessage({channel: 'astrbot-plugin-page', kind: 'ready'}, location.origin);
})();"""


@pytest.fixture
def native_voice(browser, manager_url, tmp_path):
    module = "astrbot_plugin_mimo_tts_clone"
    source = PROJECT_PLUGINS / module / "pages/settings"
    assembled = tmp_path / "assembled"
    target = assembled / "pages/settings/app.js"
    target.parent.mkdir(parents=True)
    shutil.copy2(source / "app.js", target)
    script = _patched(assembled, module, "pages/settings/app.js", all_patches=True)
    html = (source / "index.html").read_text("utf-8").replace(
        "</body>", '<script src="./test-page-sdk.js"></script></body>'
    )
    feature_id = _public_plugin_id(module)
    detail = {"id": feature_id, "display_name": "语音克隆",
              "pages": ["settings"], "enabled": True, "engine_online": True,
              "configurable": False, "config": {}, "revision": "voice-test", "schema": {}}
    payload = {"success": True, "config": {"default_context": "已保存的风格"},
               "voices": [], "defaults": {}, "emotions": ["neutral"],
               "readiness": {}, "queue_status": {}, "tasks": []}
    view = SimpleNamespace(writes=[], posts=[], reads=[], hold_read=False, payload=payload,
                           feature_id=feature_id)
    page = browser.new_page(viewport={"width": 1380, "height": 920})
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))

    def control(route):
        path = urlsplit(route.request.url).path
        if path == "/control/catalog":
            route.fulfill(json={"ok": True, "data": {"plugins": [detail], "engine_online": True}})
        elif path.endswith("/pages/settings/entry"):
            route.fulfill(json={"ok": True, "data": {"content_path": "/__native_voice/index.html"}})
        elif path.endswith("/extension/save_config"):
            view.writes.append(route.request.post_data_json)
            view.posts.append(route)
            page.locator("body").evaluate("(body, count) => { body.dataset.nativeVoiceWrites = String(count); }", len(view.writes))
        elif path.endswith("/extension/get_config"):
            if view.hold_read:
                view.reads.append(route)
                page.locator("body").evaluate("body => { body.dataset.heldVoiceReads = String(Number(body.dataset.heldVoiceReads || 0) + 1); }")
            else:
                route.fulfill(json=view.payload)
        elif "/extension/" in path:
            route.fulfill(json={"success": True, "providers": []})
        else:
            route.fulfill(json={"ok": True, "data": detail})

    def assets(route):
        name = urlsplit(route.request.url).path.rsplit("/", 1)[-1]
        if name == "index.html":
            route.fulfill(content_type="text/html; charset=utf-8", body=html)
        elif name in {"app.js", "test-page-sdk.js"}:
            route.fulfill(content_type="text/javascript", body=script if name == "app.js" else _PAGE_SDK)
        elif name == "style.css":
            route.fulfill(content_type="text/css", path=str(source / name))
        else:
            route.abort()

    page.route("**/control/**", control)
    page.route("**/__native_voice/**", assets)
    try:
        page.goto(manager_url)
        expect(page.locator("#stateTitle")).to_have_text("机器人可用")
        if page.locator("#onboardingRecommended").is_visible():
            page.locator("#onboardingRecommended").click()
        page.evaluate("""detail => {
          const api = window.pywebview.api, catalog = api.get_features, feature = api.get_feature;
          api.get_features = async () => {
            const result = structuredClone(await catalog());
            result.data.plugins = result.data.plugins.map(item => item.id === 'feature-voice-clone' ? detail : item);
            return result;
          };
          api.get_feature = id => id === detail.id ? Promise.resolve({ok:true,data:detail}) : feature(id);
          api.get_feature_page = async () => ({ok: true, data: {
            url: location.origin + '/plugin-center?feature=' + encodeURIComponent(detail.id) + '&embedded=1',
            origin: location.origin}});
        }""", detail)
        page.locator('.nav-item[data-page="features"]').click()
        page.locator(f'.feature-item[data-feature-id="{feature_id}"]').click()
        view.page = page
        view.frame = page.frame_locator("#featurePageFrame").frame_locator("#plugin-frame")
        view.field = view.frame.locator("#default-context")
        expect(view.field).to_have_value("已保存的风格")
        yield view
        assert not errors, errors
    finally:
        for route in [*view.posts, *view.reads]:
            route.abort()
        page.close()


@pytest.mark.parametrize("phase", ["post", "read", "warning"])
@pytest.mark.parametrize("width", [1380])
def test_voice_save_preserves_edits_made_after_submission(native_voice, tmp_path, phase, width):
    view = native_voice
    view.page.set_viewport_size({"width": width, "height": 920})
    view.field.fill("这次提交的风格")
    view.frame.locator("#save-config").click()
    expect(view.frame.locator("#save-config")).to_have_attribute("aria-busy", "true")
    expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "1")
    assert len(view.posts) == 1
    view.payload["config"] = view.writes[0]
    view.hold_read = phase == "read"
    if phase != "read":
        view.field.fill("提交后继续编辑的新风格")
    response = {"success": True, "config": view.writes[0]}
    if phase == "warning":
        response["warning"] = "配置已保存到插件本地文件"
    if phase == "read":
        with view.page.expect_request("**/extension/get_config"):
            view.posts.pop().fulfill(json=response)
    else:
        view.posts.pop().fulfill(json=response)
    if phase == "read":
        expect(view.frame.locator("#save-config")).to_be_disabled()
        expect(view.page.locator("body")).to_have_attribute("data-held-voice-reads", "1")
        assert len(view.reads) == 1
        view.field.fill("提交后继续编辑的新风格")
        view.reads.pop().fulfill(json=view.payload)
    expect(view.frame.locator("#save-config")).to_be_enabled()
    expect(view.field).to_have_value("提交后继续编辑的新风格")
    expect(view.frame.locator("#save-state")).to_contain_text("尚未保存")
    assert len(view.writes) == 1
    view.page.screenshot(path=str(tmp_path / f"voice-new-draft-{phase}-{width}.png"), animations="disabled")
    view.hold_read = False
    view.frame.locator("#save-config").click()
    expect(view.frame.locator("#save-config")).to_be_disabled()
    expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "2")
    assert len(view.writes) == 2
    assert view.writes[1]["default_context"] == "提交后继续编辑的新风格"
    view.payload["config"] = view.writes[1]
    view.posts.pop().fulfill(json={"success": True, "config": view.writes[1]})
    expect(view.frame.locator("#save-state")).to_have_text("配置已同步")
    expect(view.field).to_have_value("提交后继续编辑的新风格")


@pytest.mark.parametrize("failure", ["post", "read", "read-edited"])
def test_voice_save_distinguishes_write_failure_from_readback_failure(native_voice, failure):
    view = native_voice
    view.field.fill("提交的风格")
    view.frame.locator("#save-config").click()
    expect(view.frame.locator("#save-config")).to_be_disabled()
    expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "1")
    if failure == "post":
        view.posts.pop().fulfill(json={"success": False, "error": "模拟保存失败"})
        expect(view.frame.locator("#toast")).to_have_text("模拟保存失败")
        expect(view.frame.locator("#save-state")).to_have_text("有未保存更改")
    else:
        view.hold_read = True
        with view.page.expect_request("**/extension/get_config"):
            view.posts.pop().fulfill(json={"success": True, "config": view.writes[0]})
        expect(view.page.locator("body")).to_have_attribute("data-held-voice-reads", "1")
        assert len(view.reads) == 1
        if failure == "read-edited":
            view.field.fill("回读期间的新风格")
        view.reads.pop().fulfill(json={"success": False, "error": "模拟读取失败"})
        expect(view.frame.locator("#save-state")).to_contain_text("配置已保存")
        if failure == "read-edited":
            expect(view.frame.locator("#save-state")).to_contain_text("尚未保存")
        expect(view.frame.locator("#toast")).to_contain_text("配置已保存")
        expect(view.frame.locator("#toast")).to_contain_text("模拟读取失败")
    expect(view.frame.locator("#save-config")).to_be_enabled()
    expect(view.field).to_have_value("回读期间的新风格" if failure == "read-edited" else "提交的风格")
    assert len(view.writes) == 1


def test_voice_save_tracks_transport_fields_as_new_drafts(native_voice):
    view = native_voice
    view.frame.locator("#save-config").click()
    expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "1")
    view.frame.locator("#audio-transport").select_option("path")
    values = {"shared-path-linux": "/srv/new-voice", "shared-path-windows": "D:\\new-voice",
              "delivery-segment-chars": "700", "base64-max-mb": "12"}
    for identifier, value in values.items():
        view.frame.locator(f"#{identifier}").fill(value)
    expect(view.frame.locator("#save-state")).to_have_text("有未保存更改")
    view.payload["config"] = view.writes[0]
    view.posts.pop().fulfill(json={"success": True, "config": view.writes[0]})
    expect(view.frame.locator("#save-config")).to_be_enabled()
    expect(view.frame.locator("#save-state")).to_contain_text("尚未保存")
    expect(view.frame.locator("#audio-transport")).to_have_value("path")
    for identifier, value in values.items():
        expect(view.frame.locator(f"#{identifier}")).to_have_value(value)
    view.frame.locator("#save-config").click()
    expect(view.page.locator("body")).to_have_attribute("data-native-voice-writes", "2")
    assert {key: view.writes[1][key] for key in (
        "audio_transport", "shared_path_linux", "shared_path_windows",
        "delivery_segment_chars", "base64_max_mb",
    )} == {"audio_transport": "path", "shared_path_linux": "/srv/new-voice",
          "shared_path_windows": "D:\\new-voice", "delivery_segment_chars": 700, "base64_max_mb": 12}
    view.payload["config"] = view.writes[1]
    view.posts.pop().fulfill(json={"success": True, "config": view.writes[1]})
    expect(view.frame.locator("#save-state")).to_have_text("配置已同步")
