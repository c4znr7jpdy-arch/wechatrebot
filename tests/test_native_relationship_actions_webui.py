"""Confirm destructive relationship actions through the delivered native page."""
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect

from test_manager_startup import browser, manager_url  # noqa: F401
from test_native_voice_settings_webui import _PAGE_SDK
from test_plugin_portability import PROJECT_PLUGINS, _patched
from wechatrebot_manager.plugin_center import _public_plugin_id


TARGET = '目标 <img src=x onerror="window.actionInjected=true"> & "示例"'
CASES = [
    pytest.param("persona-learning", '[data-persona-action="delete"]', "删除人格？",
                 {"action": "delete", "persona_id": "persona-1"}, id="persona-delete"),
    pytest.param("persona-learning", '[data-persona-action="backup_delete"]', "删除人格备份？",
                 {"action": "backup_delete", "id": "7", "group_id": "group-a"}, id="backup-delete"),
    pytest.param("persona-learning", '[data-persona-action="backup_restore"]', "恢复人格备份？",
                 {"action": "backup_restore", "id": "7", "group_id": "group-a"}, id="backup-restore"),
    pytest.param("content", '[data-content-action="delete_content"]', "删除学习内容？",
                 {"action": "delete_content", "bucket": "dialogues", "id": "11"}, id="content-delete"),
    pytest.param("content", '[data-content-action="delete_batch"]', "删除学习批次记录？",
                 {"action": "delete_batch", "id": "13"}, id="batch-delete"),
]


@pytest.fixture
def relationship_actions(browser, manager_url, tmp_path):
    module = "astrbot_plugin_self_learning"
    source = PROJECT_PLUGINS / module / "pages/dashboard"
    assembled = tmp_path / "assembled"
    assembled.mkdir()
    script = _patched(assembled, module, "pages/dashboard/app.js", all_patches=True)
    html = (source / "index.html").read_text("utf-8").replace(
        "</body>", '<script src="./test-page-sdk.js"></script></body>',
    )
    feature_id = _public_plugin_id(module)
    detail = {"id": feature_id, "display_name": "情感与关系", "pages": ["dashboard"],
              "enabled": True, "engine_online": True, "configurable": False,
              "config": {}, "revision": "relationship-actions", "schema": {}}
    persona = {"personas": [{"persona_id": "persona-1", "name": TARGET, "prompt": "已保存人格"}],
               "current": {}, "backups": {"backups": [{"id": 7, "backup_name": TARGET,
                                                        "group_id": "group-a"}]}}
    content = {"content": {"dialogues": [{"id": 11, "title": TARGET}],
                           "analysis": [{"id": 12, "title": "另一个分析结果"}]},
               "batches": {"batches": [{"id": 13, "batch_name": TARGET}]}}
    page = browser.new_page(viewport={"width": 1380, "height": 920})
    view = SimpleNamespace(page=page, posts=[], writes=[], reads=0, errors=[])
    page.on("pageerror", lambda error: view.errors.append(str(error)))

    def control(route):
        path = urlsplit(route.request.url).path
        if path == "/control/catalog":
            route.fulfill(json={"ok": True, "data": {"plugins": [detail], "engine_online": True}})
        elif path.endswith("/pages/dashboard/entry"):
            route.fulfill(json={"ok": True, "data": {
                "content_path": "/__relationship_actions/index.html#/persona-learning"}})
        elif path.endswith("/extension/page/persona") or path.endswith("/extension/page/content"):
            view.reads += 1
            page.locator("body").evaluate("(body, count) => { body.dataset.actionReads = String(count); }", view.reads)
            route.fulfill(json={"status": "ok", "success": True,
                                "data": persona if path.endswith("/persona") else content})
        elif path.endswith("/action"):
            body = route.request.post_data_json
            view.writes.append(body)
            page.locator("body").evaluate("(body, count) => { body.dataset.actionWrites = String(count); }", len(view.writes))
            if body["action"] in {"export", "backup_detail"}:
                route.fulfill(json={"status": "ok", "success": True, "data": {
                    "success": True, "persona": {"prompt": "导出内容"}, "backup": {"backup_name": TARGET}}})
            else:
                view.posts.append(route)
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
    page.route("**/__relationship_actions/**", assets)
    origin = urlsplit(manager_url)
    try:
        page.goto(f"{origin.scheme}://{origin.netloc}/plugin-center?feature={feature_id}")
        view.frame = page.frame_locator("#plugin-frame")
        expect(view.frame.locator('[data-persona-action="delete"]')).to_be_visible()
        yield view
        assert not view.errors, view.errors
    finally:
        for route in view.posts:
            route.abort()
        page.close()


def _open(view, page_name):
    view.frame.locator(f'.nav-item[data-page="{page_name}"]').click()


def _accept(view):
    view.posts.pop().fulfill(json={"status": "ok", "success": True,
                                  "data": {"success": True, "message": "操作已完成"}})


@pytest.mark.parametrize("page_name,selector,title,expected", CASES)
def test_relationship_destructive_action_requires_one_explicit_confirmation(
    relationship_actions, page_name, selector, title, expected,
):
    view = relationship_actions
    _open(view, page_name)
    button = view.frame.locator(selector)
    button.click()
    expect(view.frame.locator("#modal-title")).to_have_text(title)
    expect(view.frame.locator("#modal-body")).to_contain_text(TARGET)
    expect(view.frame.locator("#modal-body img")).to_have_count(0)
    expect(button).to_be_disabled()
    expect(button).to_have_attribute("aria-busy", "true")
    button.dispatch_event("click")
    assert view.writes == []
    view.frame.locator("#modal-confirm-cancel").click()
    expect(view.frame.locator("#detail-modal")).not_to_have_attribute("open", "")
    expect(button).to_be_enabled()
    assert view.writes == []

    button.click()
    view.frame.locator("#modal-confirm-ok").click()
    expect(view.page.locator("body")).to_have_attribute("data-action-writes", "1")
    assert view.writes == [expected]
    expect(button).to_be_disabled()
    button.dispatch_event("click")
    view.frame.locator("#modal-confirm-ok").dispatch_event("click")
    assert view.writes == [expected]
    reads = view.reads
    _accept(view)
    expect(view.page.locator("body")).not_to_have_attribute("data-action-reads", str(reads))
    expect(button).to_be_enabled()
    assert view.frame.locator("html").evaluate("() => window.actionInjected") is None


def test_relationship_failed_delete_shows_error_and_allows_confirmed_retry(relationship_actions):
    view = relationship_actions
    button = view.frame.locator('[data-persona-action="delete"]')
    button.click()
    view.frame.locator("#modal-confirm-ok").click()
    expect(view.page.locator("body")).to_have_attribute("data-action-writes", "1")
    view.posts.pop().fulfill(json={"status": "ok", "success": True,
                                  "data": {"success": False, "message": "人格删除失败，请重试"}})
    expect(view.frame.locator("#toast-region")).to_contain_text("人格删除失败，请重试")
    expect(button).to_be_enabled()
    button.click()
    expect(view.frame.locator("#modal-title")).to_have_text("删除人格？")
    assert len(view.writes) == 1
    view.frame.locator("#modal-confirm-ok").click()
    expect(view.page.locator("body")).to_have_attribute("data-action-writes", "2")
    _accept(view)
    expect(button).to_be_enabled()


@pytest.mark.parametrize("dismissal", ["escape", "close"])
def test_relationship_confirmation_dismissal_releases_gate(relationship_actions, dismissal):
    view = relationship_actions
    button = view.frame.locator('[data-persona-action="delete"]')
    button.click()
    if dismissal == "escape":
        view.page.keyboard.press("Escape")
    else:
        view.frame.locator("#modal-close").click()
    expect(view.frame.locator("#detail-modal")).not_to_have_attribute("open", "")
    expect(button).to_be_enabled()
    assert view.writes == []
    button.click()
    expect(view.frame.locator("#modal-title")).to_have_text("删除人格？")
    view.frame.locator("#modal-confirm-cancel").click()
    expect(button).to_be_enabled()


def test_relationship_confirmation_keeps_original_content_target(relationship_actions):
    view = relationship_actions
    _open(view, "content")
    view.frame.locator('[data-content-action="delete_content"]').click()
    view.frame.locator('#content-tabs button[data-content-type="analysis"]').dispatch_event("click")
    other = view.frame.locator('[data-content-action="delete_content"][data-id="12"]')
    other.dispatch_event("click")
    expect(view.frame.locator("#modal-body")).to_contain_text("ID：11")
    assert view.writes == []
    view.frame.locator("#modal-confirm-ok").click()
    expect(view.page.locator("body")).to_have_attribute("data-action-writes", "1")
    assert view.writes == [{"action": "delete_content", "bucket": "dialogues", "id": "11"}]
    other.dispatch_event("click")
    assert len(view.writes) == 1
    _accept(view)
    expect(other).to_be_enabled()


@pytest.mark.parametrize("action,title", [("edit", "编辑人格"), ("export", "人格导出"),
                                          ("backup_detail", "备份详情")])
def test_relationship_nondestructive_persona_actions_keep_existing_behavior(
    relationship_actions, action, title,
):
    view = relationship_actions
    view.frame.locator(f'[data-persona-action="{action}"]').click()
    expect(view.frame.locator("#modal-title")).to_have_text(title)
    expect(view.frame.locator("#modal-confirm-ok")).to_have_count(0)
    assert [body["action"] for body in view.writes] == ([] if action == "edit" else [action])
