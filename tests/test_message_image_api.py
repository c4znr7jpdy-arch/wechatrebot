"""On-demand history image reads, using local databases and a mocked bridge."""
import base64
from io import BytesIO
import json
import sqlite3
from types import SimpleNamespace
from unittest.mock import Mock

from PIL import Image
import pytest

from test_app_shell import _api
from test_chat_history import _bridge_source, _source
from test_group_directory import service
from wechatrebot_manager import app_shell, group_directory as directory
from wechatrebot_manager.chat_history import ChatHistoryService


@pytest.fixture
def image_source():
    output = BytesIO()
    Image.new("RGB", (64, 48), "red").save(output, format="WEBP")
    return "data:image/webp;base64," + base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def image_history(tmp_path, image_source):
    _source(tmp_path)
    history = ChatHistoryService(tmp_path)
    assert history.sync() == 3
    source = _bridge_source(tmp_path)
    with sqlite3.connect(source) as connection:
        connection.execute(
            "UPDATE messages SET content = ?, metadata = ? WHERE id = 1",
            ("[图片]", json.dumps({"media_previews": [image_source]})),
        )
    assert history.sync() == 1
    message = history.messages("456@chatroom", account_id="wxid_bot")[0]
    assert message["id"] != 1, "The manager and bridge must have different IDs here"
    return history, message["id"]


def test_image_source_id_resolves_imported_row_without_copying_original(image_history, image_source):
    history, local_id = image_history
    assert history.image_source_id("456@chatroom", local_id, 0, account_id="wxid_bot") == 1
    assert history.image_source_id("456@chatroom", str(local_id), 0, account_id="wxid_bot") == 1
    with history._connect() as connection:
        row = connection.execute("SELECT media_previews FROM messages WHERE id = ?", (local_id,)).fetchone()
    assert json.loads(row[0]) == [image_source]


@pytest.mark.parametrize("change", ["account", "conversation", "legacy", "deleted", "missing_preview"])
def test_image_source_id_cannot_cross_history_scope(image_history, change):
    history, local_id = image_history
    account, conversation = "wxid_bot", "456@chatroom"
    if change == "account":
        account = "wxid_other"
    elif change == "conversation":
        conversation = "another@chatroom"
    elif change == "deleted":
        assert history.delete_conversation(conversation, account_id=account) == 1
    else:
        with history._connect() as connection:
            if change == "legacy":
                connection.execute(
                    "UPDATE messages SET source_key = ?, source_message_id = 99 WHERE id = ?",
                    (str(history.source.resolve()).casefold(), local_id),
                )
            else:
                connection.execute("UPDATE messages SET media_previews = '[]' WHERE id = ?", (local_id,))
    with pytest.raises(ValueError, match="原图"):
        history.image_source_id(conversation, local_id, 0, account_id=account)


@pytest.mark.parametrize("message_id,image_index", [
    (True, 0), (1.5, 0), ("1 OR 1=1", 0), ("١", 0), ("9" * 100, 0),
    (0, 0), (1, True), (1, "0"), (1, 1.5), (1, -1), (1, 4),
])
def test_image_source_id_rejects_invalid_identifiers(image_history, message_id, image_index):
    history, _ = image_history
    with pytest.raises(ValueError):
        history.image_source_id("456@chatroom", message_id, image_index, account_id="wxid_bot")


def test_message_image_api_maps_local_id_and_returns_only_display_data(tmp_path, image_history, image_source):
    history, local_id = image_history
    api = _api(tmp_path)
    api._history = history
    api._history_data_identity = lambda: ("wxid_bot", False)
    read = Mock(return_value={"image_data_url": image_source, "quality": "original"})
    api._groups = SimpleNamespace(message_image=read)
    assert "get_message_image" in app_shell.PUBLIC_API_METHODS
    result = api.get_message_image("456@chatroom", str(local_id), 0, "wxid_bot")
    assert result["ok"] is True
    assert result["data"] == {"account_id": "wxid_bot", "src": image_source, "quality": "original"}
    read.assert_called_once_with("wxid_bot", "456@chatroom", 1, 0)


@pytest.mark.parametrize("account,offline,expected", [
    ("", False, "wxid_bot"), ("wxid_other", False, "wxid_bot"),
    ("wxid_bot", False, ""), ("wxid_bot", True, "wxid_bot"),
])
def test_image_api_requires_matching_live_account_before_reading(tmp_path, account, offline, expected):
    api = _api(tmp_path)
    api._history_data_identity = lambda: (account, offline)
    api._history = SimpleNamespace(image_source_id=Mock())
    api._groups = SimpleNamespace(message_image=Mock())
    result = api.get_message_image("456@chatroom", 4, 0, expected)
    assert result["ok"] is False
    api._history.image_source_id.assert_not_called()
    api._groups.message_image.assert_not_called()


def test_deleted_history_record_never_reaches_bridge(tmp_path, image_history):
    history, local_id = image_history
    api = _api(tmp_path)
    api._history = history
    api._history_data_identity = lambda: ("wxid_bot", False)
    api._groups = SimpleNamespace(message_image=Mock())
    history.delete_conversation("456@chatroom", account_id="wxid_bot")
    assert api.get_message_image("456@chatroom", local_id, 0, "wxid_bot")["ok"] is False
    api._groups.message_image.assert_not_called()


def test_image_api_database_failure_is_safe_and_does_not_disclose_paths(tmp_path):
    api = _api(tmp_path)
    api._history_data_identity = lambda: ("wxid_bot", False)
    api._history = SimpleNamespace(image_source_id=Mock(side_effect=sqlite3.DatabaseError("private/database/path")))
    api._groups = SimpleNamespace(message_image=Mock())
    result = api.get_message_image("456@chatroom", 4, 0, "wxid_bot")
    assert result["ok"] is False
    assert "private" not in str(result)
    api._groups.message_image.assert_not_called()


def test_original_image_reads_release_directory_lock_and_do_not_cache(tmp_path, monkeypatch, image_source):
    obj, account, _ = service(tmp_path, monkeypatch)
    reads = []

    def read(port, token, identity, path, **options):
        assert obj._lock.acquire(blocking=False), "Image downloads must not block directory reads"
        obj._lock.release()
        reads.append((identity, path, options))
        return {"account_id": identity, "items": [{
            "message_id": 73, "image_index": 2, "image_data_url": image_source, "quality": "original",
        }]}

    monkeypatch.setattr(obj, "_request", read)
    first = obj.message_image(account[0], "room@chatroom", "73", 2)
    first["image_data_url"] = "changed"
    second = obj.message_image(account[0], "room@chatroom", 73, 2)
    assert second["image_data_url"] == image_source
    assert len(reads) == 2
    assert reads[0] == (account[0], "/api/manager/history/73/images/2?conversation_id=room%40chatroom",
                        {"max_response_bytes": 28 * 1024 * 1024})
    assert obj._qrcodes == {}
    assert obj._snapshot is None


@pytest.mark.parametrize("message_id,index,conversation,expected", [
    (True, 0, "friend", "test-account"), (1.1, 0, "friend", "test-account"),
    (1, False, "friend", "test-account"), (1, 4, "friend", "test-account"),
    (1, 0, "../../file", "test-account"), (1, 0, "friend?other=1", "test-account"),
    (1, 0, "friend", "another-account"),
])
def test_bridge_image_request_rejects_invalid_scope_before_network(
    tmp_path, monkeypatch, message_id, index, conversation, expected,
):
    obj, _, calls = service(tmp_path, monkeypatch)
    with pytest.raises(directory.GroupDirectoryError):
        obj.message_image(expected, conversation, message_id, index)
    assert calls == []


def _use_mock_http(obj, monkeypatch, data, *, after_read=None):
    """Keep production transport/account checks while replacing only the socket."""
    reads = []

    def open_request(request, **options):
        reads.append((request, options))
        if after_read:
            after_read()
        return BytesIO(json.dumps({"ok": True, "data": data}).encode())

    monkeypatch.setattr(obj, "_request", directory.GroupDirectoryService._request.__get__(obj))
    monkeypatch.setattr(directory, "build_opener", lambda *_args: SimpleNamespace(open=open_request))
    return reads


@pytest.mark.parametrize("bad", [
    {"account_id": "another-account"}, {"message_id": 99}, {"image_index": 1},
    {"message_id": True}, {"image_index": False},
    {"image_data_url": "file:///private.png"}, {"image_data_url": "https://example.test/private.png"},
    {"image_data_url": "data:image/svg+xml;base64,PHN2Zz4="}, {"image_data_url": None},
])
def test_bridge_image_response_rejects_wrong_identity_and_unsafe_sources(tmp_path, monkeypatch, image_source, bad):
    obj, account, _ = service(tmp_path, monkeypatch)
    item = {"message_id": 1, "image_index": 0, "image_data_url": image_source, "quality": "original"}
    item.update({key: value for key, value in bad.items() if key != "account_id"})
    data = {"account_id": bad.get("account_id", account[0]), "items": [item]}
    _use_mock_http(obj, monkeypatch, data)
    with pytest.raises(directory.GroupDirectoryError):
        obj.message_image(account[0], "friend", 1, 0)


@pytest.mark.parametrize("quality", [None, "unknown", {}, []])
def test_unknown_image_quality_never_claims_original(tmp_path, monkeypatch, image_source, quality):
    obj, account, _ = service(tmp_path, monkeypatch)
    data = {"account_id": account[0], "items": [{
        "message_id": 1, "image_index": 0, "image_data_url": image_source, "quality": quality,
    }]}
    _use_mock_http(obj, monkeypatch, data)
    assert obj.message_image(account[0], "friend", 1, 0)["quality"] == "cached"


def test_late_image_response_after_account_change_is_discarded(tmp_path, monkeypatch, image_source):
    obj, account, _ = service(tmp_path, monkeypatch)
    original_account = account[0]
    data = {"account_id": original_account, "items": [{
        "message_id": 1, "image_index": 0, "image_data_url": image_source, "quality": "original",
    }]}
    _use_mock_http(obj, monkeypatch, data, after_read=lambda: account.__setitem__(0, "another-account"))
    with pytest.raises(directory.GroupDirectoryError):
        obj.message_image(original_account, "friend", 1, 0)
    assert obj._qrcodes == {}


def test_image_transport_uses_local_authenticated_uncached_request(tmp_path, monkeypatch, image_source):
    obj, account, _ = service(tmp_path, monkeypatch)
    data = {"account_id": account[0], "items": [{
        "message_id": 1, "image_index": 0, "image_data_url": image_source, "quality": "original",
    }]}
    reads = _use_mock_http(obj, monkeypatch, data)
    assert obj.message_image(account[0], "friend", 1, 0)["image_data_url"] == image_source
    request, options = reads[0]
    assert request.full_url == "http://127.0.0.1:5000/api/manager/history/1/images/0?conversation_id=friend"
    assert request.get_header("X-wechatrebot-token") == "a" * 40
    assert request.get_header("X-wechatrebot-account") == account[0]
    assert request.get_header("Cache-control") == "no-cache"
    assert options["timeout"] == 20


def test_bridge_transport_rejects_oversized_response_before_json_decode(tmp_path, monkeypatch):
    obj, account, _ = service(tmp_path, monkeypatch)
    reads = []

    class BoundedResponse(BytesIO):
        def read(self, size=-1):
            reads.append(size)
            return super().read(size)

    monkeypatch.setattr(directory, "build_opener", lambda *_args: SimpleNamespace(
        open=lambda *_args, **_kwargs: BoundedResponse(b"x" * 100),
    ))
    with pytest.raises(directory.GroupDirectoryError):
        directory.GroupDirectoryService._request(obj, 5000, "a" * 40, account[0],
                                                "/api/manager/history/1/images/0", max_response_bytes=16)
    assert reads == [17]
