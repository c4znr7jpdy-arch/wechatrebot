"""Recommendations keep two AI dramas per live short drama without quota backfill."""
from collections import Counter

import pytest

from test_short_drama_player_controls import browser, player_assets, player_page, cinema_catalog  # noqa: F401
from test_short_drama_source_switching import state_module, _record  # noqa: F401


@pytest.mark.parametrize(("category", "title", "expected"), [
    ("AI漫剧", "作品", "ai"), ("ai短剧", "作品", "ai"), ("ＡＩ 剧", "作品", "ai"),
    ("漫剧", "作品", "ai"), ("短剧大全", "作品", "live"), ("短剧", "作品", "live"),
    ("短剧", "我最亲爱的(Ai)", "ai"), ("短剧", "作品（ＡＩ）", "ai"),
    ("短剧", "AI短剧：新生", "ai"), ("短剧", "Air中的你", "live"),
    ("国产动漫", "作品", ""), ("动画电影", "作品", ""), ("电影", "作品AI剧", ""),
    ("动漫短剧", "作品", ""), (None, "作品", ""),
])
def test_only_explicit_drama_categories_and_ai_labels_count(state_module, category, title, expected):
    assert state_module._recommendation_kind(category, title) == expected


def test_catalog_samples_both_kinds_and_keeps_reserve_through_exclusions(state_module, tmp_path, monkeypatch):
    module = state_module
    monkeypatch.setattr(module.time, "time", lambda: 1000.0)
    monkeypatch.setattr(module.secrets, "randbelow", lambda _upper: 0)
    catalog = module.RecommendationCatalog(module.StateStore(tmp_path / "ratio-state", 600, 100))
    requested = []

    def fetch(_base, **params):
        if params["ac"] == "list":
            return {"class": [{"type_id": 68, "type_name": "AI漫剧"},
                              {"type_id": 58, "type_name": "短剧大全"},
                              {"type_id": 2, "type_name": "国产动漫"}]}
        requested.append(params["t"])
        kind = "AI漫剧" if params["t"] == 68 else "短剧大全"
        return {"list": [_record(f"{kind}{i}", category=kind, count=1) for i in range(20)]
                + [_record("普通电影", category="电影", count=1)]}

    monkeypatch.setattr(module, "_catalog_json", fetch)
    first = catalog.get(set())
    assert set(requested) == {68, 58}
    assert Counter(item["recommendation_kind"] for item in first) == {"ai": 8, "live": 4}
    seen = {item["recommendation_id"] for item in first}
    second = catalog.get(seen)
    assert Counter(item["recommendation_kind"] for item in second) == {"ai": 8, "live": 4}
    assert not seen.intersection(item["recommendation_id"] for item in second)
    assert catalog.get(set())  # One viewer's exclusions never empty another viewer's pool.
    assert all(item["title"] != "普通电影" for item in catalog.cached)
    assert all(catalog.state.get(item["selection_token"])["items"][0]["recommendation_kind"]
               == item["recommendation_kind"] for item in first)


def test_category_timeout_keeps_the_other_kind_and_all_failures_raise(state_module, tmp_path, monkeypatch):
    module = state_module
    catalog = module.RecommendationCatalog(module.StateStore(tmp_path / "partial-state", 600, 100))
    monkeypatch.setattr(module.secrets, "randbelow", lambda _upper: 0)

    def fetch(_base, **params):
        if params["ac"] == "list":
            return {"class": [{"type_id": 1, "type_name": "AI漫剧"}, {"type_id": 2, "type_name": "短剧"}]}
        if params["t"] == 1:
            raise TimeoutError("AI catalog unavailable")
        return {"list": [_record("真人作品", category="短剧", count=1)]}

    monkeypatch.setattr(module, "_catalog_json", fetch)
    assert [item["recommendation_kind"] for item in catalog._load()] == ["live"]
    monkeypatch.setattr(module, "_catalog_json", lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("offline")))
    with pytest.raises(OSError, match="unavailable"):
        catalog._load()


def related_kinds(page):
    return page.locator(".related-card").evaluate_all("nodes => nodes.map(node => node.dataset.recommendationKind)")


def test_three_cards_keep_ratio_across_refresh_and_navigation(cinema_catalog):
    page = cinema_catalog
    page.locator(".recommend-card").first.click()
    for turn in range(3):
        page.wait_for_function("document.getElementById('related').getAttribute('aria-busy') === 'false'")
        assert related_kinds(page) == ["ai", "ai", "live"]
        assert page.locator(".related-kind").all_text_contents() == ["AI 剧", "AI 剧", "真人短剧"]
        if turn == 0:
            title = page.locator(".related-card strong").first.inner_text()
            page.locator(".related-card").first.click()
            assert page.locator("#title").inner_text() == title
        elif turn == 1:
            page.get_by_role("button", name="换一换").click()
    page.get_by_role("button", name="换一换").click()
    page.wait_for_function("document.getElementById('related').getAttribute('aria-busy') === 'false'")
    assert related_kinds(page) == []
    assert page.get_by_role("button", name="换一换").is_enabled()


@pytest.mark.parametrize(("ai_count", "live_count", "expected"), [(8, 0, ["ai", "ai"]), (0, 8, ["live"]), (1, 8, ["ai", "live"])])
def test_missing_kind_never_fills_its_slots_with_other_content(cinema_catalog, ai_count, live_count, expected):
    page = cinema_catalog
    items = [{"title": f"{kind}推荐{i}", "recommendation_kind": kind, "recommendation_id": f"{index:016x}",
              "episodes": [{"name": "第1集", "url": f"https://media.example/{kind}-{i}.m3u8"}]}
             for index, (kind, i) in enumerate([("ai", n) for n in range(ai_count)]
                                               + [("live", n) for n in range(live_count)], start=30)]
    items.append({"title": "未分类旧电影", "category": "电影",
                  "episodes": [{"name": "正片", "url": "https://media.example/movie.m3u8"}]})
    page.route("**/api/recommendations?*", lambda route: route.fulfill(json={"v": 1, "kind": "recommendations", "items": items}))
    page.locator(".recommend-card").first.click()
    page.wait_for_function("document.getElementById('related').getAttribute('aria-busy') === 'false'")
    assert related_kinds(page) == expected
    assert "不足" in page.locator("#relatedStatus").inner_text()
    assert "未分类旧电影" not in page.locator("#relatedGrid").inner_text()
