"""Recovery must discard offline backlog while retaining short send retries."""
import copy
import sys
import types

import pytest

from test_provider_initialization import plugin_module, _delivery_contract


def tweet_id(timestamp):
    return str(((int(timestamp * 1000) - 1288834974657) << 22) + 1)


@pytest.fixture
def recovery(plugin_module, monkeypatch):
    clock = [1791417600.0, 0.0]
    module = sys.modules[f"{plugin_module.__package__}.services.polling_service"]
    monkeypatch.setattr(module, "time", types.SimpleNamespace(
        time=lambda: clock[0], monotonic=lambda: clock[1],
    ))
    contract = _delivery_contract(plugin_module)
    store = {"tester": {"since_id": tweet_id(clock[0] - 86400), "subscribers": {"group": {"status": True}}}}
    fetched = []
    sent = []

    class API:
        items = []
        async def get_user_timeline_items(self, _username, since_id):
            fetched.append(since_id)
            return [{"tweet_id": item, "username": "tester"} for item in self.items]
        async def get_tweet(self, _username, item):
            return {"status": True, "tweet_id": item, "username": "tester"}

    class Delivery:
        collective_enabled = False
        fail = False
        async def push_to_subscribers(self, _username, info, **_kwargs):
            sent.append(info["tweet_id"])
            return contract.DeliveryResult(contract.DeliveryState.FAILED if self.fail else contract.DeliveryState.DELIVERED)

    async def read(_key, _default):
        return copy.deepcopy(store)
    async def write(_key, data):
        store.clear()
        store.update(copy.deepcopy(data))

    api, delivery = API(), Delivery()
    subscriptions = plugin_module.SubscriptionService(read, write, api, lambda: True)
    polling = plugin_module.PollingService(api, subscriptions, delivery, plugin_module.PollingSettings(
        include_retweets=True, data_provider="fxtwitter", custom_nitter_url="", website_list=(),
    ))
    async def check():
        return await polling.check_user("tester", copy.deepcopy(store["tester"]))
    def advance(seconds):
        clock[0] += seconds
        clock[1] += seconds
    return types.SimpleNamespace(clock=clock, api=api, delivery=delivery, store=store,
        polling=polling, fetched=fetched, sent=sent, check=check, advance=advance)


@pytest.mark.asyncio
async def test_restart_skips_old_tweets_even_when_provider_returns_them(recovery):
    old = tweet_id(recovery.clock[0] - 86400)
    new = tweet_id(recovery.clock[0] + 1)
    recovery.api.items = [old, new]
    assert await recovery.check()
    assert recovery.sent == [new]
    assert int(recovery.fetched[0]) > int(old)
    assert recovery.store["tester"]["since_id"] == new


@pytest.mark.asyncio
async def test_long_polling_outage_resumes_from_recovery_time(recovery):
    await recovery.check()
    recovery.advance(86400)
    old = tweet_id(recovery.clock[0] - 3600)
    new = tweet_id(recovery.clock[0] + 1)
    recovery.api.items = [old, new]
    await recovery.check()
    assert recovery.sent == [new]
    assert int(recovery.fetched[-1]) > int(old)


@pytest.mark.asyncio
async def test_short_delivery_failure_keeps_retry_cursor(recovery):
    new = tweet_id(recovery.clock[0] + 1)
    recovery.api.items = [new]
    recovery.delivery.fail = True
    await recovery.check()
    assert int(recovery.store["tester"]["since_id"]) < int(new)
    recovery.advance(300)
    recovery.delivery.fail = False
    await recovery.check()
    assert recovery.sent == [new, new]
    assert recovery.store["tester"]["since_id"] == new


@pytest.mark.asyncio
async def test_long_delivery_failure_does_not_replay_queued_timeline(recovery):
    old = tweet_id(recovery.clock[0] + 1)
    recovery.api.items = [old]
    recovery.delivery.fail = True
    await recovery.check()
    recovery.advance(3600)
    new = tweet_id(recovery.clock[0] + 1)
    recovery.api.items = [old, new]
    recovery.delivery.fail = False
    await recovery.check()
    assert recovery.sent == [old, new]


@pytest.mark.asyncio
async def test_long_configured_interval_does_not_count_as_an_outage(recovery):
    from dataclasses import replace
    recovery.polling.settings = replace(recovery.polling.settings, poll_interval_seconds=3600)
    await recovery.check()
    new = tweet_id(recovery.clock[0] + 60)
    recovery.advance(3600)
    recovery.api.items = [new]
    await recovery.check()
    assert recovery.sent == [new]


@pytest.mark.asyncio
async def test_expired_collective_cache_is_discarded_before_flush(recovery):
    class Collective:
        collective_enabled = True
        has_collected = True
        discarded = []
        def discard_collected_author(self, username):
            self.discarded.append(username)
            self.has_collected = False
        async def flush_collected(self):
            pytest.fail("expired offline messages must never reach the sender")
    collective = Collective()
    recovery.polling.delivery = collective
    recovery.polling._set_pending_collective_cursor("tester", tweet_id(recovery.clock[0] + 1))
    recovery.advance(3600)
    await recovery.polling.flush_pending_collective()
    assert collective.discarded == ["tester"]
    assert not recovery.polling.has_pending_collective


def test_discarding_old_collective_author_preserves_other_authors(plugin_module):
    module = sys.modules[f"{plugin_module.__package__}.services.tweet_delivery_service"]
    service = module.TweetDeliveryService(None, None, None, module.TweetDeliverySettings(
        use_node=True, collective_forward=True, collective_max_authors=5, deduplicate_retweets=True,
    ))
    old = module.CachedTweet("old", {}, {}, "Old", retweet_dedup_id="1")
    new = module.CachedTweet("new", {}, {}, "New", retweet_dedup_id="2")
    service._collected_tweets = {"group": [old, new], "other": [old]}
    service.discard_collected_author("OLD")
    assert service._collected_tweets == {"group": [new]}
    assert service._pending_retweet_seen == {"group": {"2"}}
