import base64
import io
import json
import sqlite3

from PIL import Image
import pytest

from test_hook_bridge_emotion_size_patch import bridge  # noqa: F401
from wechatrebot_manager.chat_history import ChatHistoryService, _media_previews


@pytest.mark.parametrize("message_type", [3, 47])
def test_first_frame_survives_import_and_media_cleanup(bridge, tmp_path, message_type):
    image = tmp_path / "animated.gif"
    Image.new("RGB", (640, 480), "red").save(
        image, save_all=True, append_images=[Image.new("RGB", (640, 480), "blue")],
        duration=100, loop=0,
    )
    service = ChatHistoryService(tmp_path)
    store = bridge.history_store.MessageHistoryStore(service.bridge_source)
    event = bridge.models.NormalizedEvent(
        kind="message", account_id="bot", conversation_id="group@chatroom",
        sender_id="user", message_id="123", message_type=message_type,
        content="[图片]" if message_type == 3 else "[表情]", media_path=image,
    )
    assert store.record_inbound(event)
    assert not store.record_inbound(event)
    store.record_outbound("group@chatroom", True, [
        bridge.models.OutboundCommand(kind="image", target="group@chatroom", source=str(image)),
    ], sender_id="bot", media_paths=[image])
    image.unlink()
    messages = service.messages("group@chatroom", account_id="bot")
    assert len(messages) == 2
    for message in messages:
        raw = base64.b64decode(message["media_previews"][0].split(",", 1)[1])
        with Image.open(io.BytesIO(raw)) as preview:
            assert preview.n_frames == 1
            assert preview.size == (320, 240)
            red, _, blue = preview.convert("RGB").getpixel((100, 100))
            assert red > 200 and blue < 30
    assert service.messages("group@chatroom", account_id="other") == ()
    assert service.messages("group@chatroom") == ()
    assert service.messages("group@chatroom", account_id="bot") == messages


def test_unavailable_or_unsafe_media_keeps_text_history(bridge, tmp_path):
    bad = tmp_path / "bad.gif"
    bad.write_text("not an image")
    assert bridge.history_media.image_preview(bad) == ""
    assert bridge.history_media.image_preview(tmp_path / "missing") == ""
    assert bridge.history_media.image_preview(None) == ""
    assert _media_previews(json.dumps([
        "file:///secret.png", "https://example.org/a.gif", "data:image/svg+xml;base64,AAA",
        "data:image/webp;base64,broken",
    ])) == []
    assert _media_previews('{"media_previews": []}') == []
    service = ChatHistoryService(tmp_path)
    store = bridge.history_store.MessageHistoryStore(service.bridge_source)
    store.record_inbound(bridge.models.NormalizedEvent(
        kind="message", account_id="bot", conversation_id="friend", sender_id="user",
        message_type=47, content="[表情]", media_path=bad,
    ))
    message = service.messages("friend", account_id="bot")[0]
    assert message["content"] == "[表情]" and message["media_previews"] == []


def test_existing_history_database_migrates_without_losing_records(tmp_path):
    from test_chat_history import _source
    _source(tmp_path)
    service = ChatHistoryService(tmp_path)
    original = service.messages("wxid_a", include_unassigned=True)
    assert len(original) == 2
    with sqlite3.connect(service.path) as db:
        db.execute("ALTER TABLE messages DROP COLUMN media_previews")
    service = ChatHistoryService(tmp_path)
    with sqlite3.connect(service.path) as db:
        assert "media_previews" in {row[1] for row in db.execute("PRAGMA table_info(messages)")}

    assert service.messages("wxid_a", include_unassigned=True) == original
