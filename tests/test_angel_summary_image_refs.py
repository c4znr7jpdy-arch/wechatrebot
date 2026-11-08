"""Retain actual managed images across production ledger compaction."""
import __future__
import ast
import importlib.util
import io
import logging
from pathlib import Path
import shutil
import threading
import time
from types import SimpleNamespace

from PIL import Image
import pytest

from test_plugin_portability import _patched


ROOT = Path(__file__).parents[1]
PLUGIN = ROOT / "vendor/data/plugins/astrbot_plugin_angel_heart"
CHAT = "wechat:GroupMessage:room"


@pytest.fixture
def ledger(tmp_path):
    plugin = tmp_path / "plugin"
    (plugin / "core").mkdir(parents=True)
    shutil.copy2(PLUGIN / "core/conversation_ledger.py", plugin / "core/conversation_ledger.py")
    source = _patched(plugin, "astrbot_plugin_angel_heart", "core/conversation_ledger.py", all_patches=True)
    selected = next(node for node in ast.parse(source).body
                    if isinstance(node, ast.ClassDef) and node.name == "ConversationLedger")
    namespace = {"__name__": __name__, "Path": Path, "time": time, "threading": threading,
                 "logger": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=[selected], type_ignores=[]), "production-ledger", "exec",
                 flags=__future__.annotations.compiler_flag), namespace)
    result = object.__new__(namespace["ConversationLedger"])
    result._ledgers = {}
    result._compression_locks = {}
    result._last_compression_time = {}
    result._lock = threading.Lock()
    result.MIN_RETAIN_COUNT = 7
    result.config_manager = SimpleNamespace(context_content_retain_tokens=80, context_tool_retain_tokens=40)
    spec = importlib.util.spec_from_file_location("summary_image_cache", PLUGIN / "core/image_cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    result.image_cache = module.ImageCache(tmp_path / "data")
    return result


def _image(ledger):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "red").save(buffer, format="PNG")
    dhash = ledger.image_cache.put(CHAT, buffer.getvalue())
    path = Path(ledger.image_cache.get_cached_path(CHAT, dhash))
    assert path.is_file()
    return path, {"role": "user", "sender_name": "用户", "chat_id": CHAT, "timestamp": 1.0,
                  "content": [{"type": "text", "text": "看这张图"}, {
                      "type": "image_url", "image_url": {"url": str(path)}, "cache_path": str(path),
                      "local_file_path": str(path), "original_url": str(path), "cache_dhash": dhash}]}


def _text(timestamp, content="继续聊"):
    return {"role": "user", "sender_name": "用户", "chat_id": CHAT, "timestamp": timestamp, "content": content}


def _messages(ledger):
    return ledger._get_or_create_ledger(CHAT)["messages"]


def _assert_retained_image(ledger, path):
    summary = _messages(ledger)[0]
    assert summary["kind"] == "context_summary"
    assert str(path) in summary["content"]
    assert summary["image_refs"] == [str(path)]
    assert ledger._collect_referenced_cache_paths(CHAT) == {str(path)}
    ledger._cleanup_unreferenced_media_cache(CHAT)
    with Image.open(path) as image:
        assert image.size == (8, 8)


def test_group_compaction_preserves_referenced_image_and_readable_path(ledger):
    path, image = _image(ledger)
    _messages(ledger).extend([image, _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    _assert_retained_image(ledger, path)


def test_image_only_message_is_not_lost_when_folded(ledger):
    path, image = _image(ledger)
    image["content"] = image["content"][1:]
    _messages(ledger).extend([image, _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    _assert_retained_image(ledger, path)


def test_repeated_compaction_keeps_one_reference_without_repeating_summary(ledger):
    path, image = _image(ledger)
    _messages(ledger).extend([image, _text(2)])
    for boundary in range(2, 7):
        assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=boundary)
        _assert_retained_image(ledger, path)
        summary = _messages(ledger)[0]["content"]
        assert summary.count(str(path)) == 1
        assert summary.count("[当前摘要]") == 1
        assert summary.count("看这张图") == 1
        assert len(summary) < 600
        _messages(ledger).append(_text(boundary + 1))


@pytest.mark.parametrize("mode", ["private_llm", "private_fallback"])
def test_private_summary_paths_keep_managed_image_references(ledger, mode):
    path, image = _image(ledger)
    _messages(ledger).extend([image, _text(2)])
    assert ledger.organize_context(CHAT, mode=mode, keep_from_timestamp=2, llm_summary="用户发过一张图片")
    _assert_retained_image(ledger, path)


def test_existing_summary_is_kept_when_it_was_selected_for_retention(ledger):
    path, image = _image(ledger)
    summary = {"role": "user", "content": f"[当前摘要]\n图片位置 {path}", "kind": "context_summary",
               "timestamp": 1.0, "image_refs": [str(path)]}
    _messages(ledger).extend([summary, _text(2)])
    ledger._get_or_create_ledger(CHAT)["current_summary"] = f"图片位置 {path}"
    assert ledger.organize_context(CHAT)
    _assert_retained_image(ledger, path)


def test_unrefenced_managed_images_are_still_cleaned(ledger):
    path, _ = _image(ledger)
    _messages(ledger).extend([_text(1, "旧文字"), _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    assert not path.exists()
    assert not _messages(ledger)[0].get("image_refs")


def test_old_text_summary_contract_remains_compatible(ledger):
    _messages(ledger).extend([_text(1, "旧文字"), _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    summary = _messages(ledger)[0]
    assert summary["content"] == "[当前摘要]\n用户: 旧文字"
    assert "image_refs" not in summary
    assert ledger.get_formal_context(CHAT)[0] == summary


@pytest.mark.parametrize("reference", ["https://example.test/image.png", "data:image/png;base64,AAAA"])
def test_external_images_do_not_become_managed_cache_references(ledger, reference):
    _messages(ledger).extend([{"role": "user", "timestamp": 1, "content": [
        {"type": "text", "text": "图片"}, {"type": "image_url", "image_url": {"url": reference}}]}, _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    assert not _messages(ledger)[0].get("image_refs")
    assert reference not in _messages(ledger)[0]["content"]


def test_duplicate_metadata_and_content_paths_are_retained_once(ledger):
    path, image = _image(ledger)
    image["image_refs"] = [str(path), str(path)]
    _messages(ledger).extend([image, _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    _assert_retained_image(ledger, path)
    assert _messages(ledger)[0]["content"].count(str(path)) == 1


def test_existing_dhash_metadata_uses_authoritative_cache_path_resolution(ledger):
    path, image = _image(ledger)
    dhash = image["content"][1]["cache_dhash"]
    image["content"] = [{"type": "image_url", "cache_dhash": dhash}]
    _messages(ledger).extend([image, _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    _assert_retained_image(ledger, path)


def test_captioned_image_keeps_inherited_reference_after_image_content_is_removed(ledger):
    path, image = _image(ledger)
    _messages(ledger).extend([image, _text(2)])
    assert ledger.add_caption_to_message(CHAT, 1.0, "一张红色图片")
    assert all(item["type"] != "image_url" for item in image["content"])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    _assert_retained_image(ledger, path)


def test_repeated_private_llm_summary_inherits_previous_image_reference_once(ledger):
    path, image = _image(ledger)
    _messages(ledger).extend([image, _text(2)])
    for boundary in range(2, 7):
        assert ledger.organize_context(CHAT, mode="private_llm", keep_from_timestamp=boundary,
                                       llm_summary="用户发过一张图片")
        _assert_retained_image(ledger, path)
        assert _messages(ledger)[0]["content"].count(str(path)) == 1
        _messages(ledger).append(_text(boundary + 1))


def test_long_rule_summary_preserves_readable_image_reference_after_text_is_truncated(ledger):
    path, image = _image(ledger)
    _messages(ledger).extend([image, *[_text(index, "长消息" * 80) for index in range(2, 55)]])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=54)
    _assert_retained_image(ledger, path)


def test_managed_text_attachment_does_not_become_an_image_reference(ledger, tmp_path):
    original = tmp_path / "notes.txt"
    original.write_text("notes", "utf-8")
    cached = ledger.image_cache.put_text_file(CHAT, original)
    assert cached.is_file()
    _messages(ledger).extend([{"role": "user", "timestamp": 1, "content": [
        {"type": "text", "text": "文件", "cache_path": str(cached)}]}, _text(2)])
    assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=2)
    assert not _messages(ledger)[0].get("image_refs")
    assert str(cached) not in _messages(ledger)[0]["content"]
    assert not cached.exists()


def test_formal_context_fallback_inherits_summary_refs(ledger):
    path, _ = _image(ledger)
    _messages(ledger).extend([_text(2), {"role": "system", "content": "[当前摘要]\n图片",
        "kind": "context_summary", "image_refs": [str(path)], "timestamp": 1}])
    ledger._get_or_create_ledger(CHAT)["current_summary"] = "图片"
    formal = ledger.get_formal_context(CHAT)
    assert formal[0]["image_refs"] == [str(path)]
    assert str(path) in formal[0]["content"]


def test_repeated_text_only_summary_does_not_duplicate_prior_summary(ledger):
    _messages(ledger).extend([_text(1, "旧文字"), _text(2, "消息2")])
    for boundary in range(2, 7):
        assert ledger.organize_on_group_enter(CHAT, keep_from_timestamp=boundary)
        summary = _messages(ledger)[0]["content"]
        assert summary.count("旧文字") == 1
        assert summary.count("[当前摘要]") == 1
        assert not _messages(ledger)[0].get("image_refs")
        _messages(ledger).append(_text(boundary + 1, f"消息{boundary + 1}"))
