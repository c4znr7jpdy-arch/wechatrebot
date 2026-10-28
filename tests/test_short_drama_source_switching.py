import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import urlopen

import pytest

from test_short_drama_player_hardening import _load_state_server, _patched_player


TITLE = "欢天喜地七仙女"
FIRST = "https://cdn.example/original/1.m3u8"
FINGERPRINT = hashlib.sha256(FIRST.encode()).hexdigest()
HINT = {"year": "2005", "category": "国产剧", "actor": "霍思燕,郑国霖"}


@pytest.fixture
def state_module(tmp_path):
    return _load_state_server(_patched_player(tmp_path), f"source_switch_{tmp_path.name}")


def _record(title=TITLE, *, year="2005", category="国产剧", actor="霍思燕,郑国霖", prefix="original", count=38, **extra):
    return {
        "vod_name": title,
        "vod_year": year,
        "type_name": category,
        "vod_actor": actor,
        "vod_play_url": "#".join(f"第{number}集$https://cdn.example/{prefix}/{number}.m3u8" for number in range(1, count + 1)),
        **extra,
    }


def test_source_switch_preserves_providers_and_rejects_other_works(state_module, monkeypatch):
    module = state_module
    resolver = module.SelectionResolver()
    sources = list(resolver.SOURCES)
    calls = []

    def catalog_json(base, **params):
        source = next(name for name, url in resolver.SOURCES.items() if url == base)
        calls.append((source, params))
        if source == "百度云资源":
            return {"list": []}
        index = sources.index(source)
        right = _record(
            TITLE + (" 2005" if index > 3 else ""),
            category="内地剧" if index % 2 else "国产剧",
            prefix="original" if index == 0 else f"provider-{index}",
        )
        return {"list": [
            _record(year="2015", count=99),
            _record(category="电影", count=99),
            _record(actor="其他演员", count=99),
            _record(TITLE + "续集", count=99),
            _record(vod_remarks="电影解说", count=99),
            _record(vod_play_url="第1集$https://127.0.0.1/private.m3u8"),
            right,
        ]}

    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    items = resolver.alternatives(TITLE, sources[0], FINGERPRINT, **HINT)
    assert len(items) == 7
    assert items[0]["source"] == sources[0]
    assert {item["source"] for item in items} == set(sources) - {"百度云资源"}
    assert all(item["year"] == "2005" and len(item["episodes"]) == 38 for item in items)
    assert len(calls) == 8
    assert all(params == {"ac": "detail", "wd": TITLE, "pg": 1} for _, params in calls)
    assert resolver.alternatives(TITLE, sources[0], FINGERPRINT, **HINT) == items
    assert len(calls) == 8


def test_source_switch_uses_card_metadata_when_original_is_offline(state_module, monkeypatch):
    module = state_module
    resolver = module.SelectionResolver()
    original = next(iter(resolver.SOURCES))
    original_url = resolver.SOURCES[original]
    calls = []

    def catalog_json(base, **params):
        calls.append(params)
        if base == original_url:
            raise OSError("original provider offline")
        return {"list": [_record(prefix="alternate"), _record(year="2015", count=99)]}

    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    items = resolver.alternatives(TITLE + " 2005", original, FINGERPRINT, **HINT)
    assert len(items) == 7
    assert all(item["source"] != original and item["year"] == "2005" for item in items)
    assert all(call["wd"] == TITLE for call in calls)


def test_source_switch_does_not_guess_between_remakes_without_metadata(state_module, monkeypatch):
    module = state_module
    resolver = module.SelectionResolver()
    original = next(iter(resolver.SOURCES))

    def catalog_json(base, **_params):
        if base == resolver.SOURCES[original]:
            return {"list": []}
        return {"list": [_record(prefix="alternate"), _record(year="2015", prefix="remake")]}

    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    assert resolver.alternatives(TITLE, original, FINGERPRINT) == []
    items = resolver.alternatives(TITLE, original, FINGERPRINT, **HINT)
    assert len(items) == 7
    assert all(item["year"] == "2005" for item in items)


@pytest.mark.parametrize("changes", [
    {"source": "https://127.0.0.1/private"},
    {"fingerprint": FIRST},
    {"title": ""},
    {"year": "x" * 21},
    {"category": "x" * 101},
    {"actor": "x" * 301},
])
def test_source_switch_rejects_invalid_input_before_network(state_module, monkeypatch, changes):
    module = state_module
    monkeypatch.setattr(module, "_catalog_json", lambda *_args, **_kwargs: pytest.fail("unexpected upstream request"))
    resolver = module.SelectionResolver()
    arguments = {"title": TITLE, "source": next(iter(resolver.SOURCES)), "fingerprint": FINGERPRINT, **HINT, **changes}
    with pytest.raises(ValueError):
        resolver.alternatives(**arguments)


def test_source_switch_limits_parallel_requests_and_coalesces_scans(state_module, monkeypatch):
    module = state_module
    resolver = module.SelectionResolver()
    original = next(iter(resolver.SOURCES))
    lock = threading.Lock()
    ready, release = threading.Event(), threading.Event()
    active = peak = calls = 0

    def catalog_json(_base, **_params):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            if active == 4:
                ready.set()
        try:
            assert release.wait(5)
            return {"list": [_record()]}
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(resolver.alternatives, TITLE, original, FINGERPRINT, **HINT)
        try:
            assert ready.wait(3)
            with pytest.raises(OSError, match="busy"):
                resolver.alternatives(TITLE, original, FINGERPRINT, **HINT)
            assert calls == 4
        finally:
            release.set()
        assert len(future.result(timeout=3)) == 8
    assert peak == 4 and calls == 8


def test_source_switch_cache_bounds_bytes_entries_and_expiry(state_module, monkeypatch):
    module = state_module
    resolver = module.SelectionResolver()
    resolver.alternative_cache_limit = 2
    original = next(iter(resolver.SOURCES))
    calls = []
    now = [1000.0]
    monkeypatch.setattr(module.time, "time", lambda: now[0])

    def catalog_json(_base, **params):
        calls.append(params)
        return {"list": [_record(params["wd"], count=1)]}

    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    for title in ("作品甲", "作品乙", "作品丙"):
        assert resolver.alternatives(title, original, FINGERPRINT, **HINT)
    assert len(resolver.alternative_cache) == 2
    assert len(calls) == 24
    now[0] += 301
    assert resolver.alternatives("作品丙", original, FINGERPRINT, **HINT)
    assert len(calls) == 32
    assert len(resolver.alternative_cache) == 1
    resolver.alternative_cache_bytes = 100
    assert resolver.alternatives("作品丁", original, FINGERPRINT, **HINT)
    assert sum(entry[2] for entry in resolver.alternative_cache.values()) <= 100
    assert not resolver.alternative_cache


def test_source_switch_http_contract_and_empty_result(state_module, monkeypatch):
    module = state_module
    resolver = module.SelectionResolver()
    monkeypatch.setattr(module, "_catalog_json", lambda *_args, **_kwargs: {"list": []})
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.selection_resolver = resolver
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    base = f"http://127.0.0.1:{server.server_port}/resolve?"
    arguments = {"title": TITLE, "source": next(iter(resolver.SOURCES)), "episode": FINGERPRINT}
    try:
        query = urlencode({**arguments, "alternatives": "1", **HINT})
        with urlopen(base + query, timeout=3) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert json.load(response) == {"v": 1, "kind": "search", "heading": TITLE, "items": []}
        for query in (
            urlencode({**arguments, "alternatives": "0"}),
            urlencode({**arguments, "year": "2005"}),
            urlencode({**arguments, "alternatives": "1", "url": "https://127.0.0.1/"}),
            urlencode({**arguments, "alternatives": "1"}) + "&alternatives=1",
            urlencode({**arguments, "alternatives": "1", "actor": "x" * 301}),
        ):
            with pytest.raises(HTTPError) as error:
                urlopen(base + query, timeout=3)
            assert error.value.code == 400
        with pytest.raises(HTTPError) as error:
            urlopen(base + urlencode(arguments), timeout=3)
        assert error.value.code == 404
        def unavailable(*_args, **_kwargs):
            raise OSError("provider offline")
        monkeypatch.setattr(module, "_catalog_json", unavailable)
        with pytest.raises(HTTPError) as error:
            urlopen(base + urlencode({**arguments, "title": "另一作品", "alternatives": "1"}), timeout=3)
        assert error.value.code == 503
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_catalog_reader_stops_slow_trickles_and_closes_response(state_module, monkeypatch):
    module = state_module
    clock = [0.0]
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])

    class SlowResponse:
        reads = 0
        closed = False

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.closed = True

        def read1(self, size):
            assert 0 < size <= 65536
            self.reads += 1
            clock[0] += 1.5
            return b" "

    response = SlowResponse()

    class Opener:
        def open(self, request, *, timeout):
            assert request.full_url.startswith("https://catalog.example/?")
            assert timeout == 3
            return response

    monkeypatch.setattr("urllib.request.build_opener", lambda *_args: Opener())
    with pytest.raises(TimeoutError, match="deadline"):
        module._catalog_json("https://catalog.example/", ac="detail", wd=TITLE)
    assert response.reads == 3
    assert response.closed
