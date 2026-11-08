"""Public catalog search stays bounded and isolated from private selections."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from test_short_drama_player_hardening import _load_state_server, _patched_player


@pytest.fixture
def state_module(tmp_path):
    return _load_state_server(_patched_player(tmp_path), f"catalog_search_{tmp_path.name}")


def record(title="欢天喜地七仙女", *, count=3, **extra):
    return {"vod_name": title, "vod_year": "2005", "type_name": "国产剧",
            "vod_play_url": "#".join(f"第{index}集$https://cdn.example/{index}.m3u8"
                                     for index in range(1, count + 1)), **extra}


def test_search_visits_every_source_and_keeps_playable_versions(state_module, monkeypatch):
    resolver = state_module.SelectionResolver()
    search = state_module.CatalogSearch(resolver)
    sources = list(resolver.SOURCES)
    calls = []

    def fetch(base, **params):
        calls.append((base, params))
        if base == resolver.SOURCES[sources[-1]]:
            raise OSError("source unavailable")
        return {"list": [record("欢天喜地七仙女续集"), record("完全不相关的纪录片"), record(), record(count=1),
                         record("不安全", vod_play_url="第1集$https://127.0.0.1/private.m3u8")]}

    monkeypatch.setattr(state_module, "_catalog_json", fetch)
    response = search.search("电视剧 欢天喜地 七仙女")
    assert response["query"] == "欢天喜地七仙女"
    assert response["sources_total"] == len(sources)
    assert response["sources_searched"] == len(sources) - 1
    assert response["failures"] == [sources[-1]]
    assert response["suggestions"] == ["欢天喜地七仙女续集"]
    assert {base for base, _ in calls} == set(resolver.SOURCES.values())
    assert all(params == {"ac": "detail", "wd": "欢天喜地七仙女", "pg": 1} for _, params in calls)
    assert [item["title"] for item in response["items"][:len(sources) - 1]] == ["欢天喜地七仙女"] * (len(sources) - 1)
    assert all(len(item["episodes"]) == 3 for item in response["items"])
    assert not any(item["title"] == "不安全" for item in response["items"])
    assert not any(item["title"] == "完全不相关的纪录片" for item in response["items"])
    assert search.search("欢天喜地七仙女") == response
    assert len(calls) == len(sources)


@pytest.mark.parametrize("query", ["", " ", "?", "a" * 101, "剧\n名", "剧\x00名"])
def test_invalid_queries_never_fetch_catalogs(state_module, monkeypatch, query):
    monkeypatch.setattr(state_module, "_catalog_json", lambda *_args, **_kwargs: pytest.fail("unexpected request"))
    with pytest.raises(ValueError):
        state_module.CatalogSearch(state_module.SelectionResolver()).search(query)


def test_query_normalization_preserves_latin_word_boundaries(state_module):
    normalize = state_module.CatalogSearch.normalize_query
    assert normalize(" Ｔｈｅ   Last of Us ") == "The Last of Us"
    assert normalize("甄 嬛 传") == "甄嬛传"
    assert normalize("电影  你好 李焕英") == "你好李焕英"


def test_search_caps_results_bytes_and_cache(state_module, monkeypatch):
    resolver = state_module.SelectionResolver()
    search = state_module.CatalogSearch(resolver)
    search.cache_limit = 2
    now = [1000.0]
    calls = []
    monkeypatch.setattr(state_module.time, "time", lambda: now[0])

    def fetch(_base, **params):
        calls.append(params)
        return {"list": [record(params["wd"] + str(index)) for index in range(100)]}

    monkeypatch.setattr(state_module, "_catalog_json", fetch)
    for title in ("作品甲", "作品乙", "作品丙"):
        response = search.search(title)
        assert len(response["items"]) == state_module.MAX_ITEMS
        assert len(json.dumps(response, ensure_ascii=False).encode()) <= state_module.MAX_BODY_BYTES
    assert len(search.cache) == 2
    assert len(calls) == len(resolver.SOURCES) * 3
    now[0] += 121
    search.search("作品丙")
    assert len(search.cache) == 1
    assert len(calls) == len(resolver.SOURCES) * 4
    search.cache_bytes = 100
    search.search("作品丁")
    assert not search.cache


def test_empty_catalogs_are_success_but_all_failed_catalogs_are_not(state_module, monkeypatch):
    search = state_module.CatalogSearch(state_module.SelectionResolver())
    monkeypatch.setattr(state_module, "_catalog_json", lambda *_args, **_kwargs: {"list": []})
    response = search.search("没有这个片名")
    assert response["items"] == [] and response["failures"] == []

    def unavailable(*_args, **_kwargs):
        raise OSError("upstream unavailable")

    monkeypatch.setattr(state_module, "_catalog_json", unavailable)
    with pytest.raises(OSError, match="unavailable"):
        search.search("另一部")
    assert "另一部" not in search.cache


def test_search_fanout_and_request_rate_are_bounded(state_module, monkeypatch):
    search = state_module.CatalogSearch(state_module.SelectionResolver())
    ready, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = 0

    def fetch(_base, **_params):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            if active == 4:
                ready.set()
        try:
            assert release.wait(5)
            return {"list": [record()]}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(state_module, "_catalog_json", fetch)
    with ThreadPoolExecutor(max_workers=2) as executor:
        future = executor.submit(search.search, "欢天喜地七仙女")
        try:
            assert ready.wait(3)
            duplicate = executor.submit(search.search, "欢天喜地七仙女")
            assert not duplicate.done()
        finally:
            release.set()
        assert future.result(timeout=3)["sources_searched"] == len(search.resolver.SOURCES)
        assert duplicate.result(timeout=3) == future.result(timeout=3)
    assert peak == 4
    search.resolver.scan_times = [state_module.time.monotonic()] * 120
    with pytest.raises(OSError, match="limit"):
        search.search("第三部")
    # Already cached titles remain available even when the scan quota is exhausted.
    assert search.search("欢天喜地七仙女")["items"]


def test_two_distinct_searches_run_together_with_shared_network_limit(state_module, monkeypatch):
    search = state_module.CatalogSearch(state_module.SelectionResolver())
    ready, release = threading.Event(), threading.Event()
    lock = threading.Lock()
    active = peak = 0
    queries = []

    def fetch(_base, **params):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
            queries.append(params["wd"])
            if active == 8:
                ready.set()
        try:
            assert release.wait(5)
            return {"list": [record(params["wd"])]}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(state_module, "_catalog_json", fetch)
    with ThreadPoolExecutor(max_workers=3) as executor:
        first = executor.submit(search.search, "作品甲")
        duplicate = executor.submit(search.search, "作品甲")
        second = executor.submit(search.search, "作品乙")
        try:
            assert ready.wait(3)
            assert len(search.resolver.scan_inflight) == 2
        finally:
            release.set()
        assert first.result(timeout=3) == duplicate.result(timeout=3)
        assert second.result(timeout=3)["query"] == "作品乙"
    assert peak == 8
    assert queries.count("作品甲") == len(search.resolver.SOURCES)
    assert queries.count("作品乙") == len(search.resolver.SOURCES)
    assert not search.resolver.scan_inflight


def test_scan_waiting_is_bounded_and_releases_state_after_timeout_or_failure(state_module, monkeypatch):
    resolver = state_module.SelectionResolver()
    monkeypatch.setattr(state_module, "CATALOG_SCAN_QUEUE_SECONDS", 0.02)
    monkeypatch.setattr(state_module, "CATALOG_SCAN_RESULT_SECONDS", 0.02)
    # Both active scan slots are occupied: a third distinct query expires promptly.
    assert resolver.scan_slots.acquire(blocking=False)
    assert resolver.scan_slots.acquire(blocking=False)
    try:
        with pytest.raises(OSError, match="busy"):
            resolver._scan(("search", "queued"), lambda: pytest.fail("unexpected scan"))
        assert not resolver.scan_inflight
    finally:
        resolver.scan_slots.release()
        resolver.scan_slots.release()
    started, release = threading.Event(), threading.Event()

    def load():
        started.set()
        assert release.wait(3)
        return {"items": []}

    with ThreadPoolExecutor(max_workers=1) as executor:
        owner = executor.submit(resolver._scan, ("search", "same"), load)
        try:
            assert started.wait(2)
            with pytest.raises(OSError, match="timed out"):
                resolver._scan(("search", "same"), lambda: pytest.fail("duplicate scan"))
            assert len(resolver.scan_inflight) == 1
        finally:
            release.set()
        assert owner.result(timeout=2) == {"items": []}
    assert not resolver.scan_inflight

    def unavailable():
        raise OSError("upstream unavailable")

    with pytest.raises(OSError, match="upstream"):
        resolver._scan(("search", "failed"), unavailable)
    assert not resolver.scan_inflight
    assert resolver._scan(("search", "failed"), lambda: "recovered") == "recovered"
    # Every caller token has been returned after all timeout/failure paths.
    assert all(resolver.scan_requests.acquire(blocking=False) for _ in range(24))
    assert not resolver.scan_requests.acquire(blocking=False)
    for _ in range(24):
        resolver.scan_requests.release()


def test_search_http_contract_and_invalid_or_offline_sources(state_module, monkeypatch):
    server = state_module.ThreadingHTTPServer(("127.0.0.1", 0), state_module.Handler)
    server.catalog_search = state_module.CatalogSearch(state_module.SelectionResolver())
    monkeypatch.setattr(state_module, "_catalog_json", lambda *_args, **_kwargs: {"list": [record()]})
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/search?"
    try:
        with urlopen(base + urlencode({"q": "欢天喜地七仙女"}), timeout=3) as response:
            assert response.headers["Cache-Control"] == "no-store"
            payload = json.load(response)
            assert payload["v"] == 1 and payload["kind"] == "search"
            assert payload["items"][0]["title"] == "欢天喜地七仙女"
        for query in ("q=", "url=https://127.0.0.1/", "q=a&q=b", "q=a&source=unknown",
                      urlencode({"q": "a" * 101}), "q=" + "a" * 2048):
            with pytest.raises(HTTPError) as error:
                urlopen(base + query, timeout=3)
            assert error.value.code == 400

        def unavailable(*_args, **_kwargs):
            raise OSError("upstream unavailable")

        monkeypatch.setattr(state_module, "_catalog_json", unavailable)
        with pytest.raises(HTTPError) as error:
            urlopen(base + urlencode({"q": "另一部"}), timeout=3)
        assert error.value.code == 503
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
