import asyncio
import base64
import copy
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_provider_initialization import _load_main_module, Plain, Image
from test_twitter_webui import _load_webui_module, FakeContext, FakePlatform, FakePlugin


class Config(dict):
    def save_config(self):
        self.saved = copy.deepcopy(dict(self))


@pytest.fixture
def plugin():
    module = _load_main_module()
    instance = module.TwitterPlugin(SimpleNamespace(send_message=AsyncMock(return_value=True)), Config({
        "basic": {"twitter_data_provider": "fxtwitter"},
        "message_format": {"twitter_send_media_separately": False},
    }))
    store = {"twitter_subs": {"OpenAI": {"since_id": "100", "subscribers": {
        "bot:GroupMessage:1": {"status": True, "delivery_mode": "card"},
        "bot:GroupMessage:2": {"status": True, "delivery_mode": "text"},
    }}}}
    async def get(key, default):
        return copy.deepcopy(store.get(key, default))
    async def put(key, value):
        store[key] = copy.deepcopy(value)
    instance.get_kv_data, instance.put_kv_data, instance.test_store = get, put, store
    return instance


@pytest.mark.asyncio
async def test_group_command_and_web_update_share_isolated_subscription(plugin):
    event = SimpleNamespace(unified_msg_origin="bot:GroupMessage:1", plain_result=lambda s: s)
    result = [s async for s in plugin.set_twitter_format(event, "OpenAI", "截图")]
    assert "已更新" in result[0]
    rows = plugin.test_store["twitter_subs"]["OpenAI"]["subscribers"]
    assert rows["bot:GroupMessage:1"]["delivery_mode"] == "screenshot"
    assert rows["bot:GroupMessage:2"]["delivery_mode"] == "text"
    assert plugin.test_store["twitter_subs"]["OpenAI"]["since_id"] == "100"
    assert (await plugin._message_options(event.unified_msg_origin, "OpenAI"))["delivery_mode"] == "screenshot"
    await plugin._update_subscription(event.unified_msg_origin, "OpenAI", {"delivery_mode": "card"})
    assert (await plugin._message_options(event.unified_msg_origin, "OpenAI"))["delivery_mode"] == "card"


@pytest.mark.asyncio
async def test_follow_command_saves_mode_without_resetting_existing_cursor(plugin):
    plugin._provider_ready = True
    event = SimpleNamespace(unified_msg_origin="bot:GroupMessage:3", plain_result=lambda s: s,
                            message_str="/推特关注 OpenAI 卡片")
    result = [s async for s in plugin.follow_twitter(event, "OpenAI")]
    assert "订阅成功" in result[0]
    author = plugin.test_store["twitter_subs"]["OpenAI"]
    assert author["subscribers"][event.unified_msg_origin]["delivery_mode"] == "card"
    assert author["since_id"] == "100"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["text", "card", "screenshot"])
async def test_three_message_modes_and_native_card(plugin, monkeypatch, mode):
    components = sys.modules["astrbot.api.message_components"]
    class Share:
        def __init__(self, **kw):
            self.__dict__.update(kw)
    components.Share = Share
    bridge = importlib.import_module(f"{type(plugin).__module__.rsplit('.', 1)[0]}.services.tweet_page_server")
    monkeypatch.setattr(bridge.TweetPageBridge, "create_urls", AsyncMock(return_value=(
        "https://player.example/short-drama/twitter/test-token", "https://player.example/avatar.jpg")))
    messages = plugin.message_service
    messages.build_screenshot_tweet_chain = AsyncMock(return_value=[Image(file="screenshot.png")])
    tweet = {"username": "OpenAI", "tweet_id": "1234567890", "text": "测试推文 hello",
             "date": "2026-09-24T01:00:00Z", "images": [], "videos": []}
    chain = await messages.build_message_chain("OpenAI", tweet, {"delivery_mode": mode})
    if mode == "card":
        assert isinstance(chain[0], Share)
        assert chain[0].title == "@OpenAI发了新推文"
        assert "2026/09/24 09:00" in chain[0].content
        assert chain[0].url == "https://player.example/short-drama/twitter/test-token"
        assert chain[0].image == "https://player.example/avatar.jpg"
    elif mode == "screenshot":
        assert isinstance(chain[0], Image)
    else:
        assert any(isinstance(c, Plain) and "测试推文" in c.text for c in chain)
    assert plugin.delivery_service.settings.use_node is False
    assert plugin.delivery_service.prepare_event_delivery(chain, "OpenAI").primary_chain == chain


@pytest.mark.asyncio
async def test_screenshot_failure_keeps_text(plugin):
    plugin.message_service.build_screenshot_tweet_chain = AsyncMock(side_effect=RuntimeError("render failed"))
    chain = await plugin.message_service.build_message_chain("OpenAI", {"text": "保留正文"}, {"delivery_mode": "screenshot"})
    assert any(isinstance(c, Plain) and "保留正文" in c.text for c in chain)


@pytest.mark.asyncio
async def test_card_send_failure_falls_back_to_readable_text(plugin):
    components = sys.modules["astrbot.api.message_components"]
    class Share:
        title, content, url = "推文", "摘要", "https://x.com/OpenAI/status/123"
    components.Share = Share
    plugin.context.send_message.side_effect = [False, True]
    assert await plugin.delivery_service.send_plain_chain_resilient("bot:GroupMessage:1", [Share()])
    sent = plugin.context.send_message.call_args.args[1].chain
    assert isinstance(sent[0], Plain) and "https://x.com/" in sent[0].text


@pytest.mark.asyncio
async def test_health_timeout_and_cancel_clear_checking(plugin):
    async def never():
        await asyncio.Event().wait()
    plugin.twitter_api.check_fxtwitter_available = never
    plugin.network_settings.timeout = 0.01
    assert await plugin.network_settings.check() is False
    assert "超时" in plugin.network_settings.error
    assert not plugin.network_settings.checking
    plugin.network_settings.timeout = 10
    task = asyncio.create_task(plugin.network_settings.check())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not plugin.network_settings.checking


@pytest.mark.asyncio
async def test_save_applies_network_and_retains_subscriptions(plugin):
    before = copy.deepcopy(plugin.test_store)
    values = plugin.network_settings.settings() | {"proxy": "http://user:secret@127.0.0.1:7892", "delivery_mode": "screenshot"}
    await plugin.network_settings.save(values)
    assert plugin.config.saved["basic"]["twitter_proxy"] == values["proxy"]
    assert plugin.twitter_api.proxy == values["proxy"]
    assert plugin.message_service.settings.text_render_mode == "screenshot"
    assert plugin.test_store == before
    route = plugin.network_settings.status()["provider"]["route"]
    assert route == "代理 http://127.0.0.1:7892"
    assert "secret" not in json.dumps(plugin.network_settings.status())


@pytest.mark.asyncio
async def test_save_failure_keeps_previous_runtime_and_config(plugin):
    before = copy.deepcopy(dict(plugin.config))
    plugin.config.save_config = lambda: (_ for _ in ()).throw(OSError("disk failure"))
    with pytest.raises(OSError):
        await plugin.network_settings.save(plugin.network_settings.settings() | {"proxy": "http://127.0.0.1:7892"})
    assert dict(plugin.config) == before
    assert plugin.proxy is None


@pytest.mark.asyncio
async def test_client_close_failure_does_not_leave_saved_settings_inactive(plugin):
    plugin.twitter_api.close = AsyncMock(side_effect=OSError("close failed"))
    values = plugin.network_settings.settings() | {"proxy": "http://127.0.0.1:7892"}
    await plugin.network_settings.save(values)
    assert plugin.twitter_api.proxy == values["proxy"]
    assert plugin.message_service.settings.proxy == values["proxy"]


@pytest.mark.asyncio
async def test_list_command_is_plain_text_and_shows_current_session_format(plugin):
    event = SimpleNamespace(unified_msg_origin="bot:GroupMessage:1", plain_result=lambda s: s)
    result = [s async for s in plugin.list_follows(event)]
    assert len(result) == 1
    assert "OpenAI" in result[0] and "卡片" in result[0]


@pytest.mark.asyncio
async def test_saving_restarts_only_plugin_health_task(plugin):
    old_check = asyncio.create_task(asyncio.sleep(60))
    plugin._poll_task, plugin._running = old_check, True
    called = asyncio.Event()
    async def poll():
        await plugin.network_settings.check()
        called.set()
    plugin._poll_tweets = poll
    await plugin.network_settings.save(plugin.network_settings.settings())
    await asyncio.wait_for(called.wait(), 1)
    assert old_check.cancelled()
    assert not plugin.network_settings.checking
    assert plugin._provider_ready
    plugin._running = False


@pytest.mark.asyncio
async def test_translation_save_applies_to_cards_without_reload(plugin, monkeypatch):
    settings = plugin.network_settings.settings() | {"translate_enabled": True,
        "translate_target_lang": "简体中文", "translate_timeout_seconds": 20}
    await plugin.network_settings.save(settings)
    assert plugin.config.saved["translation"]["twitter_translate_enabled"] is True
    assert plugin.message_service.settings.translate_timeout_seconds == 20
    plugin.context.get_current_chat_provider_id = AsyncMock(return_value="model")
    plugin.context.llm_generate = AsyncMock(return_value=SimpleNamespace(completion_text="这是译文"))
    plugin.context.get_provider_by_id = lambda _: None
    text, model = await plugin.message_service.maybe_translate({"text": "original"}, "session")
    assert (text, model) == ("这是译文", "model")
    card_module = importlib.import_module(f"{type(plugin).__module__.rsplit('.', 1)[0]}.services.wechat_card")
    sys.modules["astrbot.api.message_components"].Share = lambda **kw: SimpleNamespace(**kw)
    create = AsyncMock(return_value=("https://player.example/tweet", "https://player.example/avatar"))
    monkeypatch.setattr(card_module.TweetPageBridge, "create_urls", create)
    card = await card_module.build_card("author", {"text": "original"}, text)
    assert "这是译文" in card.content
    assert create.call_args.args[0]["text"] == "original"
    assert create.call_args.args[0]["translated_text"] == text


@pytest.mark.asyncio
async def test_hosted_card_uploads_cached_author_avatar(plugin, monkeypatch):
    module = importlib.import_module(f"{type(plugin).__module__.rsplit('.', 1)[0]}.services.tweet_page_server")
    # The renderer is owned by the existing content plugin, not a duplicate template.
    monkeypatch.setattr(sys.modules[__name__], "_tweet_page_html",
        lambda t: f'<img src="{t["avatar"]}"><p>{t["translated_text"]}</p>', raising=False)
    class Server:
        pass
    store = SimpleNamespace(enabled=True, player_url="https://player.example/short-drama/player",
                            store_payload=AsyncMock(return_value="abcdefghijklmnopqrstuvwx"))
    monkeypatch.setattr(module.TweetPageBridge, "_active_plugin", lambda _: SimpleNamespace(
        selection_store=store, player_server=Server()))
    uri = "data:image/png;base64," + base64.b64encode(
        (Path(__file__).parents[1] / "logo.png").read_bytes()).decode()
    cache = SimpleNamespace(get=AsyncMock(return_value=uri))
    tweet = {"avatar": "https://pbs.twimg.com/author.png", "translated_text": "译文",
             "retweet": {"avatar": "https://pbs.twimg.com/wrong.png"}}
    url, avatar = await module.TweetPageBridge().create_urls(tweet, cache)
    cache.get.assert_awaited_once_with(tweet["avatar"])
    payload = store.store_payload.call_args.args[0]
    assert base64.b64decode(payload["avatar"]).startswith(b"\xff\xd8\xff")
    assert 'src="data:image/jpeg;base64,' in payload["html"]
    assert "pbs.twimg.com" not in payload["html"]
    assert avatar == url + "/avatar"
    assert url.startswith("https://player.example/short-drama/twitter/")
    store.store_payload.return_value = None
    with pytest.raises(ValueError, match="保存失败"):
        await module.TweetPageBridge().create_urls(tweet, cache)
    cache.get.return_value = None
    with pytest.raises(ValueError, match="头像下载失败"):
        await module.TweetPageBridge().create_urls(tweet, cache)


@pytest.mark.asyncio
async def test_missing_hosted_service_falls_back_to_text(plugin, monkeypatch):
    module = importlib.import_module(f"{type(plugin).__module__.rsplit('.', 1)[0]}.services.tweet_page_server")
    monkeypatch.setattr(module.TweetPageBridge, "_active_plugin", lambda _: None)
    chain = await plugin.message_service.build_message_chain("author", {"text": "正文"}, {"delivery_mode": "card"})
    assert any(isinstance(c, Plain) and "正文" in c.text for c in chain)


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [("translate_enabled", "false"), ("translate_timeout_seconds", True),
    ("translate_timeout_seconds", 0), ("translate_target_lang", " ")])
async def test_invalid_translation_does_not_write_config(plugin, field, value):
    with pytest.raises(ValueError):
        await plugin.network_settings.save(plugin.network_settings.settings() | {field: value})
    assert not hasattr(plugin.config, "saved")


@pytest.mark.parametrize("proxy", ["127.0.0.1:7892", "http://127.0.0.1", "http://host:99999", "file:///secret", "http://host:80/path"])
@pytest.mark.asyncio
async def test_invalid_proxy_is_rejected_before_writing(plugin, proxy):
    with pytest.raises(ValueError):
        await plugin.network_settings.save(plugin.network_settings.settings() | {"proxy": proxy})
    assert not hasattr(plugin.config, "saved")


@pytest.mark.asyncio
async def test_web_accepts_three_modes_and_rejects_unknown():
    module = _load_webui_module()
    plugin = FakePlugin()
    controller = module.TwitterWebUIController(plugin, FakeContext([FakePlatform("bot", [{"group_id": "1"}])]))
    for mode in ("text", "card", "screenshot"):
        module.fake_request.payload = {"umo": "bot:GroupMessage:1", "username": "OpenAI", "delivery_mode": mode}
        _, code = await controller.add_subscription()
        assert code == 200
        assert plugin.last_add[2]["delivery_mode"] == mode
        _, code = await controller.update_subscription()
        assert code == 200
        assert plugin.last_update[2]["delivery_mode"] == mode
    module.fake_request.payload["delivery_mode"] = "invalid"
    assert (await controller.update_subscription())[1] == 400


def test_delivery_source_is_vendored_and_old_patches_are_removed():
    root = Path(__file__).resolve().parents[3]
    manifest = json.loads((root / "release/source-manifest.json").read_text("utf-8"))
    entry = next(e for e in manifest["entries"] if e["target"].endswith("/astrbot_plugin_twitter"))
    assert entry["origin"] == "commercial"
    assert entry["source"] == "runtime/astrbot_plugin_twitter"
    assert "tests" in entry["exclude"]
    assert not list((root / "patches").glob("astrbot-plugin-astrbot_plugin_twitter*.patch"))
