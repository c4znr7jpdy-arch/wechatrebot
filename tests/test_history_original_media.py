import asyncio
import base64
import importlib
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp.test_utils import TestClient, TestServer
from PIL import Image
import pytest

from test_hook_bridge_emotion_size_patch import bridge  # noqa: F401


def _module(bridge, name):
    return importlib.import_module(f"{bridge.history_store.__package__}.{name}")


def _record(bridge, tmp_path, *, raw=None, path=None, account="bot", conversation="friend"):
    cache = tmp_path / "media"
    cache.mkdir(exist_ok=True)
    path = path or cache / "inbound-test.png"
    if not path.exists():
        Image.new("RGB", (800, 600), "red").save(path)
    store = bridge.history_store.MessageHistoryStore(tmp_path / "history.db")
    event = bridge.models.NormalizedEvent(
        kind="message", account_id=account, conversation_id=conversation,
        sender_id="user", message_id="123", message_type=3, content="[图片]",
        media_path=path, raw=raw or {},
    )
    assert store.record_inbound(event)
    return store, cache, path


def _cdn():
    return {"data": {"content": '<msg><img aeskey="secret" cdnbigimgurl="original" '
                               'cdnmidimgurl="medium" cdnthumburl="thumbnail" /></msg>'}}


def test_history_only_records_small_references_and_keeps_image_indexes(bridge, tmp_path):
    store, cache, path = _record(bridge, tmp_path, raw=_cdn())
    original = _module(bridge, "history_original")
    assert list(cache.iterdir()) == [path]
    with sqlite3.connect(store.path) as db:
        metadata = json.loads(db.execute("SELECT metadata FROM messages").fetchone()[0])
    assert len(metadata["media_previews"]) == 1
    reference = metadata["media_originals"][0]
    assert reference["cdn"] == [{"file_id": "original", "aes_key": "secret", "image_type": 1}]
    assert store.image_reference("bot", "friend", 1, 0) == reference
    records = original.history_images([cache / "missing.png", path])
    assert len(records["media_previews"]) == len(records["media_originals"]) == 1
    assert records["media_originals"][0]["path"] == str(path.resolve())
    for account, conversation, index in [("other", "friend", 0), ("bot", "other", 0), ("bot", "friend", 1)]:
        with pytest.raises(FileNotFoundError):
            store.image_reference(account, conversation, 1, index)


def test_original_download_is_lazy_deduplicated_and_reuses_bounded_cache(bridge, tmp_path):
    store, cache, _ = _record(bridge, tmp_path, raw=_cdn())
    module = _module(bridge, "history_original")
    calls = []

    async def download(destination, candidates, message_id):
        calls.append(candidates)
        await asyncio.sleep(0.01)
        Image.new("RGB", (1600, 1200), "blue").save(destination, format="PNG")
        return True

    images = module.HistoryOriginalImages(store, cache, download, module.IMAGE_MAX_BYTES)
    assert calls == [] and not images.root.exists()

    async def run():
        first, second = await asyncio.gather(images.get("bot", "friend", 1, 0), images.get("bot", "friend", 1, 0))
        assert first == second == await images.get("bot", "friend", 1, 0)
        assert first["quality"] == "original"
        with Image.open(io.BytesIO(base64.b64decode(first["image_data_url"].split(",", 1)[1]))) as picture:
            assert picture.size == (1600, 1200)
        assert len(calls) == 1 and calls[0][0].image_type == 1
        cached = next(images.root.iterdir())
        os.utime(cached, (1, 1))
        await images.get("bot", "friend", 1, 0)
        assert len(calls) == 2

    asyncio.run(run())


def test_bridge_component_assembly_and_periodic_cleanup_are_wired_without_startup(bridge, tmp_path, monkeypatch):
    # Staging copies this shared module into the bridge package.
    from wechatrebot_manager import task_store

    package = bridge.history_store.__package__
    task_store_name = f"{package}.task_store"
    sys.modules[task_store_name] = task_store
    application = _module(bridge, "app")
    original = _module(bridge, "history_original")
    store, cache, _ = _record(bridge, tmp_path)
    cleanups = []
    media = SimpleNamespace(cleanup_expired=lambda retention: cleanups.append(retention))
    app = application.BridgeApplication.__new__(application.BridgeApplication)
    app.history = store
    app.history_images = None
    app.hook, app.media, app.router, app.onebot = object(), media, object(), object()
    app.settings = SimpleNamespace(media_root=cache, media_max_bytes=original.IMAGE_MAX_BYTES, media_retention_hours=2)
    asyncio.run(app._ensure_components())
    assert isinstance(app.history_images, original.HistoryOriginalImages)
    assert app.history_images.history is store
    assert app.history_images.download == app._download_cdn_candidates
    original_cleanups = []
    monkeypatch.setattr(app.history_images, "cleanup", lambda: original_cleanups.append(True))

    async def sleep(delay):
        assert delay == 60
        raise asyncio.CancelledError()

    monkeypatch.setattr(application.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(app._periodic_media_cleanup())
    assert original_cleanups == [True] and cleanups == [7200]


@pytest.mark.parametrize("failure", ["return_false", "invalid", "exception"])
def test_download_failure_falls_back_and_removes_partial_files(bridge, tmp_path, failure):
    store, cache, _ = _record(bridge, tmp_path, raw=_cdn())
    module = _module(bridge, "history_original")

    async def download(destination, candidates, message_id):
        destination.write_bytes(b"not-an-image")
        destination.with_name(destination.stem + "-cdn-1-attempt.img").write_bytes(b"partial")
        if failure == "exception":
            raise OSError("secret-path-must-not-be-logged")
        return failure == "invalid"

    images = module.HistoryOriginalImages(store, cache, download, module.IMAGE_MAX_BYTES)
    result = asyncio.run(images.get("bot", "friend", 1, 0))
    assert result["quality"] == "cached"
    assert not list(images.root.iterdir())


def test_cached_image_path_must_stay_in_media_root_and_old_records_are_explicit(bridge, tmp_path):
    external = tmp_path / "external.png"
    Image.new("RGB", (100, 100), "red").save(external)
    store, cache, _ = _record(bridge, tmp_path, path=external)
    module = _module(bridge, "history_original")
    images = module.HistoryOriginalImages(store, cache, AsyncMock(), module.IMAGE_MAX_BYTES)
    with pytest.raises(FileNotFoundError):
        asyncio.run(images.get("bot", "friend", 1, 0))
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET metadata = '{}' WHERE id = 1")
    with pytest.raises(FileNotFoundError):
        asyncio.run(images.get("bot", "friend", 1, 0))


def test_cleanup_enforces_age_count_and_capacity_without_touching_other_media(bridge, tmp_path, monkeypatch):
    store, cache, source = _record(bridge, tmp_path)
    module = _module(bridge, "history_original")
    images = module.HistoryOriginalImages(store, cache, AsyncMock(), module.IMAGE_MAX_BYTES)
    images.root.mkdir()
    for index in range(20):
        path = images.root / f"{index:064x}.img"
        path.write_bytes(b"x" * 10)
        os.utime(path, (1000 + index, 1000 + index))
    unrelated = images.root / "do-not-delete.txt"
    unrelated.write_text("keep")
    assert images.cleanup(now=1100) == 4
    assert len(list(images.root.glob("*.img"))) == 16
    monkeypatch.setattr(module, "CACHE_MAX_BYTES", 25)
    assert images.cleanup(now=1100) == 14
    assert images.cleanup(now=3000) == 2
    assert source.exists() and unrelated.exists()


def test_cached_input_byte_limit_and_unsupported_format_are_rejected(bridge, tmp_path):
    store, cache, path = _record(bridge, tmp_path)
    module = _module(bridge, "history_original")
    images = module.HistoryOriginalImages(store, cache, AsyncMock(), 100)
    with pytest.raises(FileNotFoundError):
        asyncio.run(images.get("bot", "friend", 1, 0))
    Image.new("RGB", (10, 10), "red").save(path, format="BMP")
    images = module.HistoryOriginalImages(store, cache, AsyncMock(), module.IMAGE_MAX_BYTES)
    with pytest.raises(FileNotFoundError):
        asyncio.run(images.get("bot", "friend", 1, 0))


@pytest.mark.parametrize("malformed", [None, {}, "invalid"])
def test_malformed_cdn_metadata_uses_available_local_image(bridge, tmp_path, malformed):
    store, cache, _ = _record(bridge, tmp_path)
    reference = store.image_reference("bot", "friend", 1, 0)
    reference["cdn"] = malformed
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE messages SET metadata = ?", (json.dumps({"media_originals": [reference]}),))
    module = _module(bridge, "history_original")
    download = AsyncMock()
    images = module.HistoryOriginalImages(store, cache, download, module.IMAGE_MAX_BYTES)
    assert asyncio.run(images.get("bot", "friend", 1, 0))["quality"] == "cached"
    download.assert_not_awaited()


def test_journal_failure_has_no_database_path_in_error(bridge, tmp_path, monkeypatch):
    store, _, _ = _record(bridge, tmp_path)

    def failure():
        raise sqlite3.OperationalError("private/path/history.db locked")

    monkeypatch.setattr(store, "_connect", failure)
    with pytest.raises(OSError, match="历史记录暂时无法读取") as error:
        store.image_reference("bot", "friend", 1, 0)
    assert "private" not in str(error.value)


def test_queued_other_image_returns_busy_without_waiting_for_download(bridge, tmp_path):
    store, cache, _ = _record(bridge, tmp_path, raw=_cdn())
    with sqlite3.connect(store.path) as db:
        db.execute("INSERT INTO messages (account_id, session_id, role, content, sender_id, sender_name, group_id, platform, timestamp, metadata, dedup_key) SELECT account_id, session_id, role, content, sender_id, sender_name, group_id, platform, timestamp, metadata, 'other' FROM messages WHERE id = 1")
    module = _module(bridge, "history_original")
    started, release = asyncio.Event(), asyncio.Event()

    async def download(destination, candidates, message_id):
        started.set()
        await release.wait()
        Image.new("RGB", (800, 600), "blue").save(destination, format="PNG")
        return True

    images = module.HistoryOriginalImages(store, cache, download, module.IMAGE_MAX_BYTES)

    async def run():
        first = asyncio.create_task(images.get("bot", "friend", 1, 0))
        await started.wait()
        try:
            with pytest.raises(ValueError, match="正在加载"):
                await asyncio.wait_for(images.get("bot", "friend", 2, 0), timeout=2)
        finally:
            release.set()
        assert (await first)["quality"] == "original"

    asyncio.run(run())


def test_endpoint_requires_current_account_and_hides_references(bridge, tmp_path):
    callback = _module(bridge, "callback_server")
    current = {"wechat_identity": "bot"}
    images = type("Images", (), {})()
    images.get = AsyncMock(return_value={"message_id": 1, "image_index": 0, "image_data_url": "data:image/png;base64,AA==", "quality": "original"})
    app = callback.create_callback_app(
        asyncio.Queue(), 1024, manager_query_token="local-secret",
        status_provider=lambda: current, manager_history_images=images,
    )

    async def run():
        async with TestClient(TestServer(app)) as client:
            url = "/api/manager/history/1/images/0?conversation_id=friend"
            assert (await client.get(url)).status == 401
            headers = {"X-WeChatReBot-Token": "local-secret", "X-WeChatReBot-Account": "bot"}
            assert (await client.get(url, headers={**headers, "Origin": "https://evil.invalid"})).status == 403
            assert (await client.get(url, headers={**headers, "X-WeChatReBot-Account": "other"})).status == 409
            response = await client.get(url, headers=headers)
            assert response.status == 200
            data = (await response.json())["data"]
            assert data["account_id"] == "bot" and data["items"][0]["quality"] == "original"
            images.get.assert_awaited_once_with("bot", "friend", 1, 0)
            assert (await client.get("/api/manager/history/1/images/4?conversation_id=friend", headers=headers)).status == 400
            for invalid_id in (0, -1, 2**63):
                invalid_url = f"/api/manager/history/{invalid_id}/images/0?conversation_id=friend"
                assert (await client.get(invalid_url, headers=headers)).status == 400
            images.get.side_effect = FileNotFoundError()
            assert (await client.get(url, headers=headers)).status == 404

            async def changed(*args):
                current["wechat_identity"] = "other"
                return {"image_data_url": "private"}

            images.get.side_effect = changed
            assert (await client.get(url, headers=headers)).status == 409

    asyncio.run(run())
