"""Edit and confirm release text against the local admin preview and fake GitHub."""
from pathlib import Path

from playwright.sync_api import expect
import pytest

from services.activation_service.admin.auth import totp
from test_admin_upload_webui import upload_page, upload_server  # noqa: F401
from test_manager_startup import browser  # noqa: F401


@pytest.mark.parametrize("width,height", [(1366, 900), (390, 844)])
def test_update_text_draft_preview_and_explicit_publication(upload_page, upload_server, tmp_path: Path, width, height):
    page, server = upload_page, upload_server
    page.set_viewport_size({"width": width, "height": height})
    editor = page.locator("#release-editor")
    expect(editor.get_by_label("弹窗标题", exact=True)).to_be_enabled()
    title = "🎉 新版本来了"
    notes = ("✨ 新增功能\n\n🛠️ 修复连接提示\n1. 保留你填写的编号和空行\n"
             '<img src="missing" onerror="window.notesExecuted=true">\n'
             + "\n".join(f"第 {index + 1} 项：完善更新体验。" for index in range(50)))
    editor.get_by_label("弹窗标题", exact=True).fill(title)
    editor.get_by_label("更新说明", exact=True).fill(notes)
    editor.get_by_role("button", name="保存草稿", exact=True).click()
    expect(page.locator(".message")).to_contain_text("草稿已保存，尚未发布")
    expect(editor.get_by_label("弹窗标题", exact=True)).to_have_value(title)
    expect(editor.get_by_label("更新说明", exact=True)).to_have_value(notes)
    assert not server.github.publications and not server.github.calls
    editor.get_by_role("button", name="预览并发布", exact=True).click()
    expect(page.get_by_role("heading", name="确认发布到线上", exact=True)).to_be_visible()
    preview = page.locator(".release-preview")
    expect(preview.get_by_role("heading", name=title, exact=True)).to_be_visible()
    assert preview.locator(".release-notes").text_content() == notes
    assert preview.locator(".release-notes").evaluate("node => getComputedStyle(node).whiteSpace") == "pre-wrap"
    expect(preview.locator("img, script")).to_have_count(0)
    assert not page.evaluate("Boolean(window.notesExecuted)")
    assert not server.github.publications and not server.github.calls
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(tmp_path / f"release-text-preview-{width}.png"), full_page=True)
    # Returning to the editor keeps the saved draft and has no publication effect.
    page.get_by_role("link", name="返回修改", exact=True).click()
    expect(editor.get_by_label("更新说明", exact=True)).to_have_value(notes)
    assert not server.github.publications and not server.github.calls
    editor.get_by_role("button", name="预览并发布", exact=True).click()
    final = page.locator('form[action$="/releases/publish"]')
    server.clock[0] += 30
    final.get_by_label("当前 6 位动态码", exact=True).fill(totp(server.secret, server.clock[0] // 30))
    final.get_by_role("button", name="确认一键发布线上", exact=True).click()
    assert final.evaluate("form => !form.checkValidity()")
    assert not server.github.publications and not server.github.calls
    final.get_by_role("checkbox").check()
    final.get_by_role("button", name="确认一键发布线上", exact=True).click()
    expect(page.locator(".message")).to_contain_text("已发布 1.0.1")
    expect(page.locator("#stable-version")).to_contain_text("1.0.1")
    assert server.github.calls == [("1.0.1", "a" * 64)]
    assert len(server.github.publications) == 1
    assert server.github.publications[0]["title"] == title
    assert server.github.publications[0]["notes"] == notes
