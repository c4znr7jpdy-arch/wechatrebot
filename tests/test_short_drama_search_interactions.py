"""Mobile search and real catalog autocomplete interaction regressions."""

from urllib.parse import parse_qs, urlsplit

import pytest

from test_short_drama_player_continuity import continuity_page, item  # noqa: F401
from test_short_drama_player_controls import browser, player_assets  # noqa: F401


def search_payload(query, *, title=None, suggestions=None):
    return {"v": 1, "kind": "search", "heading": f"搜索：{query}", "query": query,
            "items": [item(title or query + "正片")], "sources_total": 10,
            "sources_searched": 10, "failures": [],
            "suggestions": suggestions if suggestions is not None else [query + "外传"]}


def catalog_responses(page, responses=None):
    """Override only the existing fixture's public catalog endpoint."""
    calls = []

    def respond(route):
        query = parse_qs(urlsplit(route.request.url).query)["q"][0]
        calls.append(query)
        route.fulfill(json=(responses or {}).get(query, search_payload(query)))

    page.route("**/api/search?*", respond)
    return calls


@pytest.mark.parametrize("width", [320, 390, 1440])
def test_search_input_prevents_mobile_zoom_and_button_stays_on_same_row(continuity_page, width):
    page, _ = continuity_page()
    page.set_viewport_size({"width": width, "height": 844})
    page.locator("#catalogSearch").fill("一段比较长的影视搜索片名")
    geometry = page.locator("#catalogSearch").evaluate("""input => {
      const rect = node => { const r=node.getBoundingClientRect(); return {left:r.left,right:r.right,top:r.top,bottom:r.bottom}; };
      return {fontSize:parseFloat(getComputedStyle(input).fontSize),input:rect(input),
        button:rect(document.getElementById('searchAllSources')),
        field:rect(input.closest('.search-field')),width:document.documentElement.clientWidth,
        scrollWidth:document.documentElement.scrollWidth};
    }""")
    assert geometry["fontSize"] >= 16  # iOS zooms focused text inputs below 16 CSS px.
    assert geometry["input"]["right"] <= geometry["button"]["left"] + 1
    assert abs((geometry["input"]["top"] + geometry["input"]["bottom"])
               - (geometry["button"]["top"] + geometry["button"]["bottom"])) <= 3
    assert geometry["button"]["right"] <= geometry["field"]["right"]
    assert geometry["scrollWidth"] <= geometry["width"]


def test_two_character_input_fetches_remote_suggestions_without_submitting(continuity_page):
    page, _ = continuity_page()
    calls = catalog_responses(page, {"走马": search_payload("走马", title="走马上任", suggestions=["走马上任"])})
    page.locator("#catalogSearch").fill("走")
    page.wait_for_timeout(500)
    assert calls == []
    page.locator("#catalogSearch").fill("走马")
    page.locator("#searchAutocomplete [role=option]").filter(has_text="走马上任").wait_for()
    assert calls == ["走马"]
    assert page.locator("#catalogSearch").get_attribute("aria-expanded") == "true"
    assert "search=" not in urlsplit(page.url).fragment
    assert page.locator("#recommendationsTitle").inner_text() == "搜索片单"


def test_ime_waits_for_composition_end_before_requesting_remote_suggestions(continuity_page):
    page, _ = continuity_page()
    calls = catalog_responses(page)
    search = page.locator("#catalogSearch")
    search.dispatch_event("compositionstart")
    search.fill("仙剑")
    search.press("Enter")
    page.wait_for_timeout(500)
    assert calls == []
    assert page.locator("#searchAutocomplete").is_hidden()
    search.dispatch_event("compositionend")
    page.locator("#searchAutocomplete [role=option]").filter(has_text="仙剑正片").wait_for()
    assert calls == ["仙剑"]


def test_clicking_autocomplete_searches_that_title_and_keeps_result_suggestions(continuity_page):
    page, _ = continuity_page()
    calls = catalog_responses(page, {"仙剑外传": search_payload("仙剑外传", title="外传完整剧集", suggestions=["仙剑续作"])})
    page.locator("#catalogSearch").fill("仙剑")
    page.locator("#searchAutocomplete [role=option]").filter(has_text="仙剑外传").click()
    page.locator(".recommend-card").filter(has_text="外传完整剧集").wait_for()
    assert calls == ["仙剑", "仙剑外传"]
    assert page.locator("#catalogSearch").input_value() == "仙剑外传"
    assert page.locator("#searchAutocomplete").is_hidden()
    assert page.locator("#searchSuggestions button").inner_text() == "仙剑续作"
    assert parse_qs(urlsplit(page.url).fragment)["search"] == ["仙剑外传"]
    # A fuzzy result remains visible although its name differs from the submitted query.
    assert page.locator(".recommend-card").filter(has_text="外传完整剧集").is_visible()


def test_keyboard_can_choose_suggestion_or_submit_original_query(continuity_page):
    page, _ = continuity_page()
    calls = catalog_responses(page)
    search = page.locator("#catalogSearch")
    search.fill("仙剑")
    page.locator("#searchAutocomplete [role=option]").first.wait_for()
    search.press("ArrowDown")
    selected_id = search.get_attribute("aria-activedescendant")
    assert selected_id
    selected = page.locator("#" + selected_id)
    selected_text = selected.inner_text()
    assert selected.get_attribute("aria-selected") == "true"
    search.press("Enter")
    page.wait_for_function("!document.getElementById('recommendations').matches('[aria-busy=true]') && document.getElementById('searchAutocomplete').hidden")
    assert calls[-1] == selected_text
    assert parse_qs(urlsplit(page.url).fragment)["search"] == [selected_text]

    search.fill("倚天")
    page.locator("#searchAutocomplete [role=option]").first.wait_for()
    assert not search.get_attribute("aria-activedescendant")
    search.press("Enter")
    page.locator(".recommend-card").filter(has_text="倚天正片").wait_for()
    assert calls[-1] == "倚天"
    assert parse_qs(urlsplit(page.url).fragment)["search"] == ["倚天"]


@pytest.mark.parametrize("close_action", ["escape", "clear", "blur"])
def test_autocomplete_closes_without_submitting_on_escape_clear_or_blur(continuity_page, close_action):
    page, _ = continuity_page()
    calls = catalog_responses(page)
    search = page.locator("#catalogSearch")
    search.fill("仙剑")
    page.locator("#searchAutocomplete [role=option]").first.wait_for()
    if close_action == "escape":
        search.press("Escape")
    elif close_action == "clear":
        page.locator("#clearSearch").click()
        assert search.input_value() == ""
    else:
        page.locator("#recommendationsTitle").click()
    page.locator("#searchAutocomplete").wait_for(state="hidden")
    assert search.get_attribute("aria-expanded") == "false"
    assert "search=" not in urlsplit(page.url).fragment
    assert calls == ["仙剑"]


def test_late_autocomplete_response_cannot_replace_newer_input_suggestions(continuity_page):
    page, _ = continuity_page()
    # A source may finish despite AbortController: emulate that transport race.
    page.evaluate("""() => {const fetch=window.fetch; window.fetch=(url,options={}) => fetch(url,{...options,signal:undefined});}""")
    pending = []

    def respond(route):
        query = parse_qs(urlsplit(route.request.url).query)["q"][0]
        if query == "旧片":
            pending.append(route)
        else:
            route.fulfill(json=search_payload(query))

    page.route("**/api/search?*", respond)
    with page.expect_request(lambda request: "/api/search?" in request.url and parse_qs(urlsplit(request.url).query).get("q") == ["旧片"]):
        page.locator("#catalogSearch").fill("旧片")
    page.locator("#catalogSearch").fill("新片")
    page.locator("#searchAutocomplete [role=option]").filter(has_text="新片正片").wait_for()
    assert len(pending) == 1
    with page.expect_response(lambda response: "/api/search?" in response.url and parse_qs(urlsplit(response.url).query).get("q") == ["旧片"]):
        pending[0].fulfill(json=search_payload("旧片"))
    page.wait_for_timeout(100)
    assert page.locator("#catalogSearch").input_value() == "新片"
    assert page.locator("#searchAutocomplete [role=option]").filter(has_text="新片正片").is_visible()
    assert page.locator("#searchAutocomplete [role=option]").filter(has_text="旧片").count() == 0


def test_search_response_preserves_filter_edited_while_search_was_pending(continuity_page):
    page, _ = continuity_page()
    pending = []

    def respond(route):
        query = parse_qs(urlsplit(route.request.url).query)["q"][0]
        if query == "仙剑":
            pending.append(route)
        else:
            route.fulfill(json=search_payload(query))

    page.route("**/api/search?*", respond)
    search = page.locator("#catalogSearch")
    with page.expect_request(lambda request: "/api/search?" in request.url
                             and parse_qs(urlsplit(request.url).query).get("q") == ["仙剑"]):
        search.fill("仙剑")
        search.press("Enter")
    search.fill("外传")
    response = search_payload("仙剑")
    response["items"] = [item("仙剑正片"), item("仙剑外传")]
    pending[0].fulfill(json=response)
    page.locator('#recommendations[aria-busy="false"]').wait_for(state="attached")
    assert search.input_value() == "外传"
    assert page.locator(".recommend-card:visible strong").all_text_contents() == ["仙剑外传"]
    assert "找到 1 部作品" in page.locator("#recommendationsSummary").inner_text()
    assert parse_qs(urlsplit(page.url).fragment) == {"search": ["仙剑"], "q": ["外传"]}
