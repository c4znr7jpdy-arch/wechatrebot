"""Exercise the assembled plugin's metadata search without starting the robot."""

import asyncio
import importlib.util
import json
import sys
from types import MethodType, ModuleType

import pytest

from test_short_drama_network_safety import ROOT, _load_patched_search


def test_plugin_assembly_copies_the_authoritative_title_helper(patched_plugins):
    checkout = patched_plugins / "jiang_short_drama"
    assert (checkout / "title_search.py").read_bytes() == (ROOT / "runtime/jiang_short_drama/title_search.py").read_bytes()
    package_name = "assembled_short_drama_title_search"
    package = ModuleType(package_name)
    package.__path__ = [str(checkout)]
    sys.modules[package_name] = package
    spec = importlib.util.spec_from_file_location(f"{package_name}.search", checkout / "search.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    assert module.title_score("写给给阿嬷的情书", "给阿嬷的情书") >= module.DEFAULT_MIN_FUZZY_SCORE
    assert len(module.DEFAULT_SOURCES) == 12


def record(title="给阿嬷的情书", *, source="a", year="2026", **extra):
    return {
        "vod_name": title, "vod_year": year, "type_name": "剧情片", "vod_actor": "演员甲",
        "vod_play_url": f"正片$https://media.example/{source}.m3u8", **extra,
    }


def metadata_session(module, monkeypatch, handler):
    """Use real parsing/ranking while ensuring every HTTP call is a catalog GET."""
    calls = []
    urls = {source.url for source in module.DEFAULT_SOURCES}

    class Response:
        status = 200

        def __init__(self, payload):
            self.body = json.dumps(payload).encode()
            self.content = self

        async def read(self, size):
            data, self.body = self.body[:size], self.body[size:]
            return data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class Session:
        def __init__(self, **options):
            assert options["trust_env"] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        def get(self, url, *, params, allow_redirects):
            assert url in urls, "metadata search attempted a media request"
            assert allow_redirects is False
            calls.append((url, params["wd"]))
            return Response(handler(url, params["wd"]))

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(module, "_public_connector", lambda: None)
    return calls


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["写给给阿嬷的情书", "写给阿嫲的情书"])
async def test_plugin_title_fallback_uses_original_query_and_stops_after_match(tmp_path, monkeypatch, query):
    module, _ = _load_patched_search(tmp_path)
    searcher = module.ShortDramaSearcher()
    terms = module.fuzzy_search_terms(query)
    assert terms and len(terms) <= module.MAX_FALLBACK_TERMS
    calls = metadata_session(module, monkeypatch, lambda _url, term: {
        "list": [record()] if term == terms[0] else [],
    })
    variants = await searcher.search_variants(query)
    assert len(variants) == 1 and variants[0].title == "给阿嬷的情书"
    assert variants[0].score == module.title_score(query, variants[0].title)
    assert module.DEFAULT_MIN_FUZZY_SCORE <= variants[0].score < module.EXACT_TITLE_SCORE
    assert len(calls) == len(module.DEFAULT_SOURCES) * 2
    assert {term for _, term in calls} == {query, terms[0]}
    assert searcher.cached_variants(query) == variants
    await searcher.search_variants(query)
    assert len(calls) == len(module.DEFAULT_SOURCES) * 2


@pytest.mark.asyncio
async def test_plugin_regular_search_reuses_metadata_fallback_and_selected_result_cache(tmp_path, monkeypatch):
    module, _ = _load_patched_search(tmp_path)
    query = "写给给阿嬷的情书"
    terms = module.fuzzy_search_terms(query)
    calls = metadata_session(module, monkeypatch, lambda _url, term: {
        "list": [record()] if term == terms[0] else [],
    })
    searcher = module.ShortDramaSearcher()
    choices = []

    async def select(self, _session, _semaphore, pool, *, prefer_full):
        assert prefer_full is False
        choices.append(pool)
        return pool[0] if pool else None

    monkeypatch.setattr(searcher, "_choose_best_result", MethodType(select, searcher))
    result = await searcher.search(query, prefer_full=False)
    assert result.title == "给阿嬷的情书"
    assert len(calls) == len(module.DEFAULT_SOURCES) * 2
    assert searcher.cached_variants(query) == (result,)
    assert await searcher.search(query, prefer_full=False) == result
    assert len(choices) == 1
    assert len(calls) == len(module.DEFAULT_SOURCES) * 2


def test_plugin_title_matching_respects_catalog_year_and_explicit_sequel_number(tmp_path):
    module, _ = _load_patched_search(tmp_path)
    correct_year = module.matching_items("给阿嬷的情书2026", [record()], "example")
    assert correct_year and correct_year[0].score >= module.DEFAULT_MIN_FUZZY_SCORE
    wrong_year = module.matching_items("给阿嬷的情书2025", [record()], "example")
    assert not wrong_year or wrong_year[0].score < module.DEFAULT_MIN_FUZZY_SCORE
    assert module.matching_items("流浪地球2", [record("流浪地球", year="2019")], "example") == []


@pytest.mark.asyncio
async def test_plugin_exact_title_avoids_fallback_and_preserves_category_filter(tmp_path, monkeypatch):
    module, _ = _load_patched_search(tmp_path)
    query = "给阿嬷的情书"
    calls = metadata_session(module, monkeypatch, lambda _url, _term: {
        "list": [record(), record(query + "2", source="sequel", type_name="国产剧")],
    })
    searcher = module.ShortDramaSearcher()
    variants = await searcher.search_variants(query, media_type="电影")
    assert len(variants) == 1 and variants[0].title == query
    assert variants[0].score == module.EXACT_TITLE_SCORE
    assert calls and all(term == query for _, term in calls)
    assert len(calls) == len(module.DEFAULT_SOURCES)


@pytest.mark.asyncio
async def test_plugin_unrelated_candidates_do_not_stop_fallback_budget(tmp_path, monkeypatch):
    module, _ = _load_patched_search(tmp_path)
    query = "写给阿嫲的情书"
    calls = metadata_session(module, monkeypatch, lambda _url, _term: {
        "list": [record("完全不同的电影")],
    })
    variants = await module.ShortDramaSearcher().search_variants(query)
    assert variants == ()
    assert len(calls) == len(module.DEFAULT_SOURCES) * (1 + len(module.fuzzy_search_terms(query)))
    assert len({term for _, term in calls}) <= 1 + module.MAX_FALLBACK_TERMS


@pytest.mark.asyncio
async def test_plugin_metadata_deadline_keeps_completed_sources_and_cancels_slow_ones(tmp_path, monkeypatch):
    module, _ = _load_patched_search(tmp_path)
    searcher = module.ShortDramaSearcher(sources=module.DEFAULT_SOURCES[:3])
    monkeypatch.setattr(module, "_TITLE_SEARCH_TIMEOUT_SECONDS", 0.03)
    cancelled, started = [], []
    result = module.matching_items("测试片名", [record("测试片名")], searcher.sources[0].name)[0]

    async def lookup(self, _session, _semaphore, source, term, **kwargs):
        started.append((source.name, term, kwargs.get("match_query")))
        if source == self.sources[0]:
            return [result]
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.append(source.name)

    monkeypatch.setattr(searcher, "_search_source_variants", MethodType(lookup, searcher))
    found = await asyncio.wait_for(searcher._search_title_candidates(None, asyncio.Semaphore(4), "测试片名", None), 1)
    assert found == [result]
    assert len(started) == 3 and all(term == original == "测试片名" for _, term, original in started)
    assert set(cancelled) == {source.name for source in searcher.sources[1:]}


@pytest.mark.asyncio
async def test_plugin_caller_cancellation_propagates_and_cleans_up_metadata_tasks(tmp_path, monkeypatch):
    module, _ = _load_patched_search(tmp_path)
    searcher = module.ShortDramaSearcher(sources=module.DEFAULT_SOURCES[:2])
    entered, stopped = asyncio.Event(), []

    async def lookup(self, _session, _semaphore, source, _term, **_kwargs):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.append(source.name)

    monkeypatch.setattr(searcher, "_search_source_variants", MethodType(lookup, searcher))
    task = asyncio.create_task(searcher._search_title_candidates(None, asyncio.Semaphore(4), "测试片名", None))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(stopped) == {source.name for source in searcher.sources}
