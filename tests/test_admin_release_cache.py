import asyncio
from types import SimpleNamespace

import pytest

from services.activation_service.admin.github import GitHubError
from services.activation_service.admin.release_cache import ReleaseReadCache


async def settle(cache):
    """Await only jobs that were already scheduled, without scheduling a refresh."""
    await asyncio.gather(*tuple(cache._tasks))


@pytest.mark.asyncio
async def test_cold_snapshot_returns_before_network_and_shares_one_refresh_per_kind():
    ready, started = asyncio.Event(), asyncio.Event()
    calls = {"stable": 0, "listing": 0}

    async def stable():
        calls["stable"] += 1
        started.set()
        await ready.wait()
        return {"version": "1.0.1"}

    async def listing():
        calls["listing"] += 1
        await ready.wait()
        return [{"version": "1.0.1"}]

    cache = ReleaseReadCache(SimpleNamespace(stable=stable, listing=listing))
    try:
        first = cache.snapshot("stable")
        assert first == {"value": None, "loading": True, "error": "", "age_seconds": None}
        await started.wait()
        for _ in range(8):
            assert cache.snapshot("stable") == first
        assert calls == {"stable": 1, "listing": 0}
        assert cache.snapshot("listing")["loading"]
        ready.set()
        await settle(cache)
        assert calls == {"stable": 1, "listing": 1}
        assert cache.snapshot("stable")["value"] == {"version": "1.0.1"}
        assert cache.snapshot("listing")["value"] == [{"version": "1.0.1"}]
    finally:
        ready.set()
        await cache.close()


@pytest.mark.asyncio
async def test_expiry_returns_old_value_then_refreshes_without_exposing_mutable_cache():
    now, value = [5.0], {"version": "1.0.1", "notes": ["first"]}
    gate = asyncio.Event()
    gate.set()

    async def stable():
        await gate.wait()
        return value

    cache = ReleaseReadCache(SimpleNamespace(stable=stable), clock=lambda: now[0], ttl=10)
    try:
        cache.snapshot("stable")
        await settle(cache)
        value["notes"].append("external mutation")
        returned = cache.snapshot("stable")
        assert returned["value"]["notes"] == ["first"]
        returned["value"]["notes"].clear()
        now[0] = 14.9
        assert cache.snapshot("stable") == {"value": {"version": "1.0.1", "notes": ["first"]},
                                             "loading": False, "error": "", "age_seconds": 9}
        now[0] = 15.0
        gate.clear()
        stale = cache.snapshot("stable")
        assert stale["loading"] and stale["age_seconds"] == 10
        assert stale["value"]["version"] == "1.0.1"
        value["version"] = "1.0.2"
        gate.set()
        await settle(cache)
        assert cache.snapshot("stable")["value"]["version"] == "1.0.2"
        assert cache.snapshot("stable")["age_seconds"] == 0
    finally:
        gate.set()
        await cache.close()


@pytest.mark.asyncio
async def test_failed_refresh_preserves_success_and_retries_only_after_backoff():
    now, calls, fail = [100.0], [0], [False]

    async def stable():
        calls[0] += 1
        if fail[0]:
            raise GitHubError("GitHub 请求失败（HTTP 403），请检查网络和令牌权限。")
        return {"version": "1.0.1"}

    cache = ReleaseReadCache(SimpleNamespace(stable=stable), clock=lambda: now[0], ttl=10, retry_seconds=15)
    try:
        cache.snapshot("stable")
        await settle(cache)
        now[0], fail[0] = 110.0, True
        cache.snapshot("stable")
        await settle(cache)
        result = cache.snapshot("stable")
        assert result["value"] == {"version": "1.0.1"} and result["age_seconds"] == 10
        assert result["error"].startswith("GitHub 请求失败") and not result["loading"]
        now[0] = 124.9
        cache.snapshot("stable")
        assert calls[0] == 2
        now[0], fail[0] = 125.0, False
        assert cache.snapshot("stable")["loading"]
        await settle(cache)
        assert calls[0] == 3 and cache.snapshot("stable")["error"] == ""
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_timeout_has_safe_error_and_backoff():
    calls, cancelled = [0], asyncio.Event()

    async def listing():
        calls[0] += 1
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cache = ReleaseReadCache(SimpleNamespace(listing=listing), timeout=0.01)
    try:
        cache.snapshot("listing")
        await asyncio.wait_for(settle(cache), timeout=1)
        assert cancelled.is_set()
        result = cache.snapshot("listing")
        assert result == {"value": None, "loading": False, "error": "GitHub 查询超时，请稍后重试。", "age_seconds": None}
        assert calls[0] == 1
    finally:
        await cache.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [RuntimeError("sensitive response token=hidden"), []])
async def test_unexpected_errors_are_logged_by_type_and_never_expose_details(caplog, outcome):
    async def stable():
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    cache = ReleaseReadCache(SimpleNamespace(stable=stable))
    try:
        cache.snapshot("stable")
        await settle(cache)
        assert cache.snapshot("stable")["error"] == "GitHub 暂不可用，请稍后重试。"
        assert "sensitive" not in caplog.text and "token" not in caplog.text
        assert ("RuntimeError" if isinstance(outcome, Exception) else "TypeError") in caplog.text
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_invalidation_discards_late_read_and_starts_independent_new_generation():
    entered, cancelled, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = [0]

    async def stable():
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                cancelled.set()
                await release.wait()
            return {"version": "old"}
        return {"version": "new"}

    cache = ReleaseReadCache(SimpleNamespace(stable=stable))
    try:
        cache.snapshot("stable")
        await entered.wait()
        cache.invalidate("stable")
        assert cache.snapshot("stable")["value"] is None
        await cancelled.wait()
        # Let the new generation finish before the cancelled remote read returns.
        await asyncio.sleep(0)
        assert cache.snapshot("stable")["value"] == {"version": "new"}
        release.set()
        await settle(cache)
        assert cache.snapshot("stable")["value"] == {"version": "new"}
        assert calls[0] == 2
    finally:
        release.set()
        await cache.close()


@pytest.mark.asyncio
async def test_invalidation_clears_only_requested_kind_or_all_by_default():
    async def stable():
        return {"version": "1.0.1"}

    async def listing():
        return [{"version": "1.0.1"}]

    cache = ReleaseReadCache(SimpleNamespace(stable=stable, listing=listing))
    try:
        cache.snapshot("stable")
        cache.snapshot("listing")
        await settle(cache)
        cache.invalidate("listing")
        assert cache.snapshot("stable")["value"] == {"version": "1.0.1"}
        assert cache.snapshot("listing")["value"] is None
        await settle(cache)
        cache.invalidate()
        assert cache.snapshot("stable")["value"] is None
        assert cache.snapshot("listing")["value"] is None
    finally:
        await cache.close()


@pytest.mark.asyncio
async def test_close_cancels_pending_reads_and_never_schedules_again():
    entered, cancelled = asyncio.Event(), asyncio.Event()

    async def stable():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    cache = ReleaseReadCache(SimpleNamespace(stable=stable))
    cache.snapshot("stable")
    await entered.wait()
    await asyncio.wait_for(cache.close(), timeout=1)
    assert cancelled.is_set() and not cache._tasks
    assert cache.snapshot("stable") == {"value": None, "loading": False, "error": "", "age_seconds": None}
    await cache.close()


@pytest.mark.asyncio
async def test_close_before_refresh_starts_clears_loading_state():
    async def stable():
        raise AssertionError("A refresh cancelled before startup must not run")

    cache = ReleaseReadCache(SimpleNamespace(stable=stable))
    assert cache.snapshot("stable")["loading"]
    await cache.close()
    assert not cache.snapshot("stable")["loading"] and not cache._tasks


@pytest.mark.asyncio
async def test_read_waits_for_one_shared_refresh_and_returns_completed_snapshots():
    started, ready = asyncio.Event(), asyncio.Event()
    calls = 0

    async def stable():
        nonlocal calls
        calls += 1
        started.set()
        await ready.wait()
        return {"version": "1.0.1"}

    cache = ReleaseReadCache(SimpleNamespace(stable=stable), ttl=0)
    readers = [asyncio.create_task(cache.read("stable")) for _ in range(2)]
    try:
        await started.wait()
        await asyncio.sleep(0)
        assert not any(reader.done() for reader in readers)
        ready.set()
        results = await asyncio.gather(*readers)
        assert calls == 1
        assert results == [{"value": {"version": "1.0.1"}, "loading": False,
                            "error": "", "age_seconds": 0}] * 2
    finally:
        ready.set()
        await cache.close()


@pytest.mark.asyncio
async def test_cancelling_one_client_does_not_cancel_the_shared_read():
    started, ready, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def stable():
        started.set()
        try:
            await ready.wait()
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return {"version": "1.0.1"}

    cache = ReleaseReadCache(SimpleNamespace(stable=stable))
    first = asyncio.create_task(cache.read("stable"))
    second = asyncio.create_task(cache.read("stable"))
    try:
        await started.wait()
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not cancelled.is_set() and not second.done()
        ready.set()
        assert (await second)["value"] == {"version": "1.0.1"}
        assert not cancelled.is_set()
    finally:
        ready.set()
        await cache.close()


@pytest.mark.asyncio
async def test_read_follows_invalidation_to_the_new_generation():
    started, cancelled, ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    calls = 0

    async def stable():
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        await ready.wait()
        return {"version": "new"}

    cache = ReleaseReadCache(SimpleNamespace(stable=stable))
    reader = asyncio.create_task(cache.read("stable"))
    try:
        await started.wait()
        cache.invalidate("stable")
        await cancelled.wait()
        ready.set()
        assert (await reader)["value"] == {"version": "new"}
        assert calls == 2
    finally:
        ready.set()
        await cache.close()


@pytest.mark.asyncio
async def test_read_returns_safe_timeout_in_the_single_response():
    async def stable():
        await asyncio.Event().wait()

    cache = ReleaseReadCache(SimpleNamespace(stable=stable), timeout=0.01)
    try:
        result = await asyncio.wait_for(cache.read("stable"), timeout=1)
        assert result == {"value": None, "loading": False,
                          "error": "GitHub 查询超时，请稍后重试。", "age_seconds": None}
    finally:
        await cache.close()
