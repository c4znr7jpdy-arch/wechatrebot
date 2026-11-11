"""Player continuity checks with local media and synthetic public catalog responses."""
import json
import re
from urllib.parse import parse_qs, urlsplit

import pytest

from test_short_drama_player_controls import browser, player_assets  # noqa: F401


def item(title="测试作品", source="暴风资源", prefix="original"):
    return {"title": title, "source": source, "year": "2005", "category": "国产剧",
            "episodes": [{"name": f"第{n}集", "url": f"https://media.example/{prefix}-{n}.m3u8"} for n in range(1, 4)]}


@pytest.fixture
def continuity_page(browser, player_assets):
    html, media = player_assets
    pages, errors = [], []

    def create(*, stored=None, expired=False, search_failure=False, block_storage=False, url=None, search_items=None):
        page = browser.new_page(viewport={"width": 390, "height": 844}, has_touch=True, is_mobile=True)
        page.set_default_timeout(6000)
        page.on("pageerror", lambda error: errors.append(str(error)))
        page.add_init_script("""(() => {
          HTMLMediaElement.prototype.canPlayType = () => 'probably';
          const src = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
          Object.defineProperty(HTMLMediaElement.prototype, 'src', {get: src.get,
            set(value) { src.set.call(this, value.replace('.m3u8', '.mp4')); }});
        })();""")
        if stored is not None:
            page.add_init_script("localStorage.setItem('short-drama-watch-history', " + json.dumps(stored) + ");")
        if block_storage:
            page.add_init_script("Storage.prototype.getItem = Storage.prototype.setItem = () => {throw new Error('blocked')};")
        requests = []
        works = [item(), item("作品乙", prefix="b"), item("作品丙", prefix="c"), item("作品丁", prefix="d")]

        def respond(route):
            parsed = urlsplit(route.request.url)
            path, query = parsed.path, parse_qs(parsed.query)
            if path.endswith("/player"):
                route.fulfill(body=html, content_type="text/html; charset=utf-8")
            elif "/api/selections/" in path:
                route.fulfill(status=410 if expired else 200, json={"v": 1, "kind": "search", "heading": "搜索片单", "items": works})
            elif path.endswith("/api/resolve"):
                requests.append(query)
                current = next((work for work in works if work["title"] == query.get("title", [""])[0]), works[0])
                values = [current, item(current["title"], "非凡资源", "alternate")] if "alternatives" in query else [current]
                route.fulfill(json={"v": 1, "kind": "search", "items": values})
            elif path.endswith("/api/search"):
                route.fulfill(status=503 if search_failure else 200, json={"v": 1, "kind": "search", "heading": "全源搜索", "items": search_items if search_items is not None else [item("新剧", prefix="new")], "sources_total": 10, "sources_searched": 10, "failures": ["不可用源"], "suggestions": ["相关新剧"]})
            elif path.endswith("/api/recommendations"):
                route.fulfill(json={"v": 1, "kind": "recommendations", "items": []})
            elif path.endswith(".mp4"):
                headers = {"Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"}
                match = re.fullmatch(r"bytes=(\d+)-(\d*)", route.request.headers.get("range", ""))
                if match:
                    start = int(match[1]); end = min(int(match[2] or len(media) - 1), len(media) - 1)
                    headers["Content-Range"] = f"bytes {start}-{end}/{len(media)}"
                    route.fulfill(body=media[start:end + 1], status=206, content_type="video/webm", headers=headers)
                else:
                    route.fulfill(body=media, content_type="video/webm", headers=headers)
            else:
                route.fulfill(body="", content_type="application/javascript")

        page.route("**/*", respond)
        page.goto(url or "https://player.example/short-drama/player?s=abcdefghijklmnop")
        page.locator('#recommendations[aria-busy="false"]').wait_for(state="attached")
        pages.append(page)
        return page, requests

    yield create
    for page in pages:
        page.close()
    assert not errors, errors


def open_first(page):
    page.locator(".recommend-card").first.click()
    try:
        page.wait_for_function("document.getElementById('video').readyState >= 2")
    except Exception:
        pytest.fail(str(page.locator('#video').evaluate("v=>({state:v.readyState,src:v.currentSrc,error:v.error?.message,status:document.getElementById('status').textContent,body:document.body.innerText})")))


def stored_history(page):
    return page.evaluate("JSON.parse(localStorage.getItem('short-drama-watch-history') || '[]')")


def test_source_preloads_before_menu_and_paused_switch_resumes(continuity_page):
    page, requests = continuity_page()
    open_first(page)
    page.wait_for_function("document.querySelectorAll('#sourceOptions button').length === 2")
    assert any("alternatives" in query for query in requests)
    page.locator("#video").evaluate("video => {video.currentTime=2; video.pause()}")
    page.locator("#speedButton").click()
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("!document.getElementById('video').paused && document.getElementById('video').currentTime >= 1.9")
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    assert "非凡" in page.locator("#videoHint").inner_text()
    hint = page.locator("#videoHint").bounding_box()
    shell = page.locator("#videoShell").bounding_box()
    assert hint["y"] > shell["y"] + shell["height"] / 2
    assert hint["height"] < 55
    assert len([q for q in requests if "alternatives" in q]) == 1


def test_history_survives_refresh_and_same_title_source_merges(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.locator("#moreButton").click()
    page.locator("#next").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.locator("#video").evaluate("video => {video.currentTime=3; video.dispatchEvent(new Event('pause'))}")
    page.wait_for_function("JSON.parse(localStorage.getItem('short-drama-watch-history'))[0].episode === 1")
    page.locator("#moreButton").click()
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    # The old source may still be playing while the replacement media loads.
    page.wait_for_function("""() => {
      const video = document.getElementById('video');
      return video.currentSrc.includes('alternate-2.mp4') && video.readyState >= 2 && !video.paused;
    }""")
    page.locator("#video").evaluate("video => {video.currentTime=3; video.pause()}")
    page.wait_for_function("JSON.parse(localStorage.getItem('short-drama-watch-history'))[0]?.item.source === '非凡资源'")
    assert len(stored_history(page)) == 1
    assert stored_history(page)[0]["item"]["source"] == "非凡资源"
    page.reload()
    page.locator(".history-row").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2 && document.getElementById('video').currentTime >= 2.9")
    assert "alternate-2" in page.locator("#video").get_attribute("src")
    assert page.locator("#sourceLabel").inner_text() == "非凡"
    assert page.locator("#watchHistory").bounding_box()["width"] == page.locator(".watch-description").bounding_box()["width"]


def test_history_has_three_recent_unique_titles(continuity_page):
    page, _ = continuity_page()
    for title in ["测试作品", "作品乙", "作品丙", "作品丁"]:
        page.locator(".recommend-card").filter(has_text=title).click()
        page.wait_for_function("document.getElementById('video').readyState >= 2")
        page.locator("#video").evaluate("video => {video.currentTime=1; video.dispatchEvent(new Event('pause'))}")
        page.locator("#pageBackButton").click()
    assert [entry["title"] for entry in stored_history(page)] == ["作品丁", "作品丙", "作品乙"]
    assert page.locator(".history-row").count() == 3


def test_search_all_sources_with_coverage_suggestions_and_reload(continuity_page):
    page, _ = continuity_page()
    page.locator("#catalogSearch").fill("新剧")
    page.locator("#searchAllSources").click()
    page.locator(".recommend-card").filter(has_text="新剧").wait_for()
    assert "10/10" in page.locator("#recommendationsSummary").inner_text()
    assert "1 个来源暂不可用" in page.locator("#recommendationsSummary").inner_text()
    assert page.locator("#searchSuggestions button").inner_text() == "相关新剧"
    page.reload()
    assert page.locator(".recommend-card").filter(has_text="新剧").count() == 1


def test_expired_selection_still_exposes_search(continuity_page):
    page, _ = continuity_page(expired=True)
    assert "片单已过期" in page.locator("#recommendationsTitle").inner_text()
    page.locator("#catalogSearch").fill("新剧")
    page.locator("#catalogSearch").press("Enter")
    page.locator(".recommend-card").filter(has_text="新剧").wait_for()


@pytest.mark.parametrize("stored,block_storage", [("{invalid", False), ('[{"item":{"title":"恶意","episodes":[{"url":"https://127.0.0.1/x.m3u8"}]}}]', False), (None, True)])
def test_corrupt_or_unavailable_storage_does_not_block_player(continuity_page, stored, block_storage):
    page, _ = continuity_page(stored=stored, block_storage=block_storage)
    open_first(page)
    assert page.locator("#video").evaluate("video => video.readyState") >= 2
    assert page.locator(".history-row").count() == 1
    if block_storage:
        page.evaluate("history.replaceState(null, '', location.pathname + location.search)")
        page.reload()
        page.locator('#recommendations[aria-busy="false"]').wait_for(state="attached")
        assert page.locator(".history-row").count() == 0
        open_first(page)
        assert page.locator(".history-row").count() == 1
        assert "仅在本次页面保留" in page.locator("#historyStatus").inner_text()


def test_intro_outro_skip_settings_are_per_work_and_do_not_double_advance(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.locator("#moreButton").click()
    page.locator("#skipIntro").fill("2")
    page.locator("#skipOutro").fill("2")
    page.locator("#skipEnabled").check()
    page.locator("#moreButton").click()
    page.locator("#togglePlayback").click()
    page.wait_for_function("document.getElementById('video').currentTime >= 2")
    page.locator("#video").evaluate("video => { video.currentTime=6.1; video.dispatchEvent(new Event('timeupdate')); video.dispatchEvent(new Event('ended')); }")
    page.wait_for_function("document.getElementById('meta').textContent === '第2集'")
    assert "original-2" in page.locator("#video").get_attribute("src")
    page.locator("#pageBackButton").click()
    page.locator(".recommend-card").filter(has_text="作品乙").click()
    page.locator("#moreButton").click()
    assert page.locator("#skipIntro").input_value() == "0"
    assert page.locator("#skipEnabled").is_checked()


def test_history_progress_updates_keep_focused_row_and_latest_position(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.locator(".history-row").focus()
    page.locator(".history-row").evaluate("node => window.originalHistoryRow = node")
    page.locator("#video").evaluate("video => {video.currentTime=3; video.dispatchEvent(new Event('pause'))}")
    assert page.locator(".history-row").evaluate("node => node === window.originalHistoryRow && document.activeElement === node")
    page.locator(".history-row").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2 && document.getElementById('video').currentTime >= 2.9")


def test_history_numeric_year_title_is_preserved_and_year_suffix_merges(continuity_page):
    original = item("2012")
    seed = json.dumps([{"item": original, "episode": 1, "position": 2, "updated": 1}])
    page, _ = continuity_page(stored=seed)
    assert "2012" in page.locator(".history-row").inner_text()
    page.reload()
    assert "2012" in page.locator(".history-row").inner_text()
    # Same-work provider title suffixes share one local record and skip preference.
    page.locator(".recommend-card").first.click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.route("**/api/resolve?*", lambda route: route.fulfill(json={"v": 1, "kind": "search", "items": [item("测试作品 2005", "非凡资源", "suffix")]}))
    page.get_by_label("返回", exact=True).click()
    page.locator(".recommend-card").first.click()
    page.wait_for_function("document.querySelectorAll('#sourceOptions button').length >= 2")
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2 && !document.getElementById('video').paused")
    page.locator("#video").evaluate("video => video.pause()")
    assert len([entry for entry in stored_history(page) if entry["title"].startswith("测试作品")]) == 1


def test_history_rotated_media_url_recovers_same_source_episode(continuity_page):
    original = item()
    original["episodes"][1]["url"] = "https://media.example/expired-2.m3u8"
    seed = json.dumps([{"item": original, "episode": 1, "position": 2, "updated": 1}])
    page, _ = continuity_page(stored=seed)
    rotated = item(prefix="rotated")
    page.route("**/expired-2.mp4", lambda route: route.abort("failed"))
    page.route("**/api/resolve?*", lambda route: route.fulfill(status=404, json={"error": "not found"}))
    page.route("**/api/search?*", lambda route: route.fulfill(json={"v": 1, "kind": "search", "items": [rotated]}))
    page.locator(".history-row").click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('rotated-2.mp4') && document.getElementById('video').readyState >= 2 && document.getElementById('video').currentTime >= 1.9")
    assert page.locator("#meta").inner_text() == "第2集"
    assert page.locator("#sourceLabel").inner_text() == "暴风"


def test_hls_fatal_network_recovery_is_bounded(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.evaluate("""() => {
      document.getElementById('video').canPlayType = () => '';
      window.Hls = class {
        static Events = {MANIFEST_PARSED: 'manifest', LEVEL_SWITCHED: 'level', ERROR: 'error'};
        static ErrorTypes = {NETWORK_ERROR: 'network', MEDIA_ERROR: 'media'};
        static isSupported() { return true; }
        constructor() { this.handlers = {}; this.levels = []; this.reconnects = 0; window.testHls = this; }
        loadSource(url) { this.url = url; }
        attachMedia(video) { this.video = video; video.src = this.url; }
        on(name, callback) { this.handlers[name] = callback; }
        startLoad() { this.reconnects++; }
        recoverMediaError() { this.reconnects++; }
        destroy() {}
      };
    }""")
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("!document.getElementById('video').paused")
    page.evaluate("for(let i=0;i<3;i++) window.testHls.handlers.error('',{fatal:true,type:'network'})")
    assert page.evaluate("window.testHls.reconnects") == 2
    assert page.locator("#retry").is_visible()
    assert page.locator("#status").get_attribute("class") == "error"


def test_explicit_other_source_card_resumes_its_own_source(continuity_page):
    saved = item()
    page, _ = continuity_page(stored=json.dumps([{"item": saved, "episode": 1, "position": 2, "updated": 1}]))
    selected = item(source="非凡资源", prefix="explicit")
    page.route("**/api/selections/**", lambda route: route.fulfill(json={"v": 1, "kind": "search", "items": [selected]}))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('explicit-2.mp4') && document.getElementById('video').currentTime >= 1.9")
    assert page.locator("#sourceLabel").inner_text() == "非凡"


def test_remake_card_does_not_restore_other_year_progress(continuity_page):
    page, _ = continuity_page(stored=json.dumps([{"item": item(), "episode": 2, "position": 2, "updated": 1}]))
    remake = {**item(prefix="remake"), "year": "2015"}
    page.route("**/api/selections/**", lambda route: route.fulfill(json={"v": 1, "kind": "search", "items": [remake]}))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    assert "remake-1" in page.locator("#video").get_attribute("src")
    assert page.locator("#video").evaluate("video => video.currentTime") < 1


def test_timeupdate_clears_stale_buffering_message(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.locator("#togglePlayback").click()
    page.wait_for_function("!document.getElementById('video').paused && document.getElementById('video').readyState >= 3")
    # Capture each synchronous handler's result before native media events run.
    waiting_status = page.locator("#video").evaluate("""video => {
      video.dispatchEvent(new Event('waiting'));
      return document.getElementById('status').textContent;
    }""")
    assert "缓冲" in waiting_status
    playing_status = page.locator("#video").evaluate("""video => {
      video.dispatchEvent(new Event('timeupdate'));
      return document.getElementById('status').textContent;
    }""")
    assert playing_status == "正在播放"


@pytest.mark.parametrize("dispatch_update, advances", [
    pytest.param(True, (3,), id="timeupdate-then-stall"),
    pytest.param(False, (3,), id="no-timeupdate-then-stall"),
    pytest.param(True, (1.04,), id="tiny-final-frame-then-stall"),
    pytest.param(True, (3, 5, 7, 9, 11), id="ongoing-progress-then-stall"),
])
def test_advancing_playback_avoids_false_stall_even_with_low_ready_state(continuity_page, dispatch_update, advances):
    page, _ = continuity_page()
    open_first(page)
    page.clock.install()
    page.locator("#video").evaluate("""video => {
      window.mediaProgress = 1;
      Object.defineProperties(video, {
        paused: {configurable: true, get: () => false},
        readyState: {configurable: true, get: () => 2},
        currentTime: {configurable: true, get: () => window.mediaProgress},
        seeking: {configurable: true, get: () => false}
      });
      video.dispatchEvent(new Event('waiting'));
    }""")
    for progress in advances:
        page.clock.fast_forward(9000)
        page.locator("#video").evaluate("""(video, [dispatchUpdate, progress]) => {
          window.mediaProgress = progress;
          if (dispatchUpdate) video.dispatchEvent(new Event('timeupdate'));
        }""", [dispatch_update, progress])
        assert page.locator("#retry").is_hidden()
    page.clock.fast_forward(10000)
    assert page.locator("#status").inner_text() == "正在播放"
    assert page.locator("#retry").is_hidden()
    # Without timeupdate, progress is first observed by the watchdog at 18 seconds.
    page.clock.fast_forward(7000 if dispatch_update else 16000)
    assert page.locator("#retry").is_hidden()
    # No second waiting event: stopping after any advance must still expose recovery.
    page.clock.fast_forward(2001)
    assert "持续缓冲" in page.locator("#status").inner_text()
    assert page.locator("#retry").is_visible()
    assert page.locator("#switchSourceOnError").is_visible()


@pytest.mark.parametrize("seek_target", [10, 200])
def test_seek_resets_stall_baseline_without_counting_jump_as_playback(continuity_page, seek_target):
    page, _ = continuity_page()
    open_first(page)
    page.clock.install()
    page.locator("#video").evaluate("""video => {
      window.mediaProgress = 100; window.mediaSeeking = false;
      Object.defineProperties(video, {
        paused: {configurable: true, get: () => false},
        readyState: {configurable: true, get: () => 2},
        currentTime: {configurable: true, get: () => window.mediaProgress},
        seeking: {configurable: true, get: () => window.mediaSeeking}
      });
      video.dispatchEvent(new Event('waiting'));
    }""")
    page.clock.fast_forward(9000)
    page.locator("#video").evaluate("""(video, target) => {
      window.mediaSeeking = true; window.mediaProgress = target;
      video.dispatchEvent(new Event('seeking'));
      video.dispatchEvent(new Event('waiting'));
    }""", seek_target)
    page.clock.fast_forward(18001)
    assert page.locator("#retry").is_hidden()
    page.locator("#video").evaluate("""video => {
      window.mediaSeeking = false; video.dispatchEvent(new Event('seeked'));
      window.mediaProgress += 2; video.dispatchEvent(new Event('timeupdate'));
    }""")
    page.clock.fast_forward(9000)
    assert page.locator("#retry").is_hidden()
    assert page.locator("#status").inner_text() == "正在播放"
    page.clock.fast_forward(10000)
    assert "持续缓冲" in page.locator("#status").inner_text()
    assert page.locator("#retry").is_visible()


@pytest.mark.parametrize("error_name, expected_error", [("NotSupportedError", True), ("NetworkError", True), ("NotAllowedError", False)])
def test_play_rejection_distinguishes_autoplay_permission_from_media_failure(continuity_page, error_name, expected_error):
    page, _ = continuity_page()
    open_first(page)
    page.locator("#video").evaluate("(video, name) => { video.play = () => Promise.reject(new DOMException('rejected', name)); }", error_name)
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("document.getElementById('status').classList.contains('error') || document.getElementById('status').textContent.includes('已就绪')")
    assert page.locator("#status").evaluate("node => node.classList.contains('error')") is expected_error
    if expected_error:
        assert page.locator("#retry").is_visible()
        assert "已就绪" not in page.locator("#status").inner_text()
    else:
        assert "点击播放" in page.locator("#status").inner_text()
        assert page.locator("#retry").is_hidden()


def test_late_autoplay_rejection_keeps_existing_network_error(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.locator("#video").evaluate("video => { video.play = () => new Promise((resolve, reject) => { window.rejectPendingPlay = reject; }); }")
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("typeof window.rejectPendingPlay === 'function'")
    page.locator("#video").evaluate("video => { Object.defineProperty(video, 'error', {configurable: true, value:{code:2}}); video.dispatchEvent(new Event('error')); }")
    message = page.locator("#status").inner_text()
    page.evaluate("window.rejectPendingPlay(new DOMException('blocked', 'NotAllowedError'))")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(resolve))")
    assert page.locator("#status").inner_text() == message
    assert page.locator("#status").evaluate("node => node.classList.contains('error')")
    assert page.locator("#retry").is_visible()


def test_history_then_related_work_refresh_keeps_new_work(continuity_page):
    page, _ = continuity_page(stored=json.dumps([{"item": item(), "episode": 1, "position": 2, "updated": 1}]))
    related = {**item("推荐新剧", prefix="related"), "recommendation_kind": "live", "selection_token": "bbbbbbbbbbbbbbbb"}
    page.route("**/api/recommendations?*", lambda route: route.fulfill(json={"v": 1, "kind": "recommendations", "items": [related]}))
    page.route("**/api/selections/bbbbbbbbbbbbbbbb", lambda route: route.fulfill(json={"v": 1, "kind": "recommendations", "items": [related]}))
    page.locator(".history-row").click()
    page.locator(".related-card").filter(has_text="推荐新剧").click()
    assert "resume=" not in page.url
    assert "search=" not in page.url
    page.reload()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    assert page.locator("#title").inner_text() == "推荐新剧"
    assert "related-1" in page.locator("#video").get_attribute("src")


def test_history_return_to_search_then_new_card_refresh_keeps_new_work(continuity_page):
    page, _ = continuity_page(stored=json.dumps([{"item": item(), "episode": 1, "position": 2, "updated": 1}]))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop#search=new")
    page.reload()  # A fragment-only navigation does not rerun the document entry flow.
    page.locator(".recommend-card").filter(has_text="新剧").wait_for()
    page.locator(".history-row").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.locator("#pageBackButton").click()
    page.locator(".recommend-card").filter(has_text="新剧").click()
    assert "resume=" not in page.url
    page.reload()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    assert page.locator("#title").inner_text() == "新剧"
    assert "new-1" in page.locator("#video").get_attribute("src")


@pytest.mark.parametrize("available", [True, False])
def test_explicit_source_card_normalizes_episode_number_and_does_not_guess(continuity_page, available):
    page, _ = continuity_page(stored=json.dumps([{"item": item(), "episode": 1, "position": 2, "updated": 1}]))
    selected = item(source="非凡资源", prefix="padded")
    selected["episodes"] = [{**episode, "name": episode["name"].replace("第", "第0")} for episode in selected["episodes"]]
    if not available:
        selected["episodes"] = selected["episodes"][:1]
    page.route("**/api/selections/**", lambda route: route.fulfill(json={"v": 1, "kind": "search", "items": [selected]}))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    if available:
        page.wait_for_function("document.getElementById('video').currentSrc.includes('padded-2.mp4') && document.getElementById('video').currentTime >= 1.9")
        assert page.locator("#meta").inner_text() == "第02集"
    else:
        assert page.locator(".watch-stage").is_hidden()
        assert "暂无上次观看的第2集" in page.locator("#recommendationsSummary").inner_text()
        assert not page.locator("#video").get_attribute("src")


@pytest.mark.parametrize("entry", ["card", "deep_link", "related"])
def test_existing_history_is_visible_before_new_work_media_metadata(continuity_page, entry):
    page, _ = continuity_page(stored=json.dumps([{"item": item(), "episode": 1, "position": 2, "updated": 1}]))
    # An unavailable new source never emits loadedmetadata; history must move on entry.
    page.route("**/*.mp4", lambda route: route.abort("failed"))
    if entry == "related":
        related = {**item("作品乙", prefix="b"), "selection_token": "bbbbbbbbbbbbbbbb"}
        related["recommendation_kind"] = "live"
        page.route("**/api/recommendations?*", lambda route: route.fulfill(json={"v": 1, "kind": "recommendations", "items": [related]}))
        page.locator(".history-row").click()
        page.locator(".related-card").filter(has_text="作品乙").click()
    elif entry == "deep_link":
        page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop#i=1")
        page.reload()
    else:
        page.locator(".recommend-card").filter(has_text="作品乙").click()
    page.wait_for_function("document.getElementById('title').textContent === '作品乙'")
    assert page.locator("#video").evaluate("video => video.readyState") == 0
    assert page.locator("#watchHistory").is_visible()
    assert page.locator("#watchHistory").evaluate("node => node.parentElement.classList.contains('watch-stage')")
    assert page.locator(".history-row").filter(has_text="测试作品").is_visible()


def shared_url(page):
    page.evaluate("Object.defineProperty(navigator, 'share', {configurable:true, value: async data => {window.sharedPlayerData=data}})")
    page.locator("#shareButton").click()
    page.wait_for_function("!!window.sharedPlayerData")
    return page.evaluate("window.sharedPlayerData.url")


def test_history_share_opens_same_source_and_episode_in_new_context(continuity_page):
    saved = item(source="非凡资源", prefix="saved")
    page, _ = continuity_page(stored=json.dumps([{"item": saved, "episode": 1, "position": 2, "updated": 1}]))
    page.locator(".history-row").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    url = shared_url(page)
    assert "resume=" not in url and "s=" not in url
    assert "watch=1" in url
    recipient, _ = continuity_page(url=url, search_items=[item("无关作品"), item(), saved])
    recipient.wait_for_function("document.getElementById('video').readyState >= 2")
    assert recipient.locator("#title").inner_text() == "测试作品"
    assert recipient.locator("#sourceLabel").inner_text() == "非凡"
    assert "saved-2" in recipient.locator("#video").get_attribute("src")
    # The recipient context has no sender's saved record before entering this exact selection.
    assert recipient.context != page.context
    recipient.reload()
    recipient.wait_for_function("document.getElementById('video').readyState >= 2")
    assert "saved-2" in recipient.locator("#video").get_attribute("src")


def test_search_share_ignores_catalog_result_order_and_local_history(continuity_page):
    selected = item("新剧", "无尽资源", "selected")
    page, _ = continuity_page(search_items=[item("其他作品"), selected])
    page.locator("#catalogSearch").fill("新剧")
    page.locator("#searchAllSources").click()
    page.locator(".recommend-card").filter(has_text="新剧").click()
    page.locator("#episodeGrid").get_by_label("第3集", exact=True).click()
    url = shared_url(page)
    assert "search=" not in url and "i=" not in url
    recipient, _ = continuity_page(url=url, search_items=[selected, item("其他作品")],
        stored=json.dumps([{"item": item("新剧", "暴风资源", "old-history"), "episode": 0, "position": 1, "updated": 1}]))
    recipient.wait_for_function("document.getElementById('video').readyState >= 2")
    assert recipient.locator("#sourceLabel").inner_text() == "无尽"
    assert recipient.locator("#meta").inner_text() == "第3集"
    assert "selected-3" in recipient.locator("#video").get_attribute("src")


def test_changed_source_share_opens_changed_source_and_episode(continuity_page):
    page, _ = continuity_page()
    open_first(page)
    page.locator("#episodeGrid").get_by_label("第2集", exact=True).click()
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('alternate-2.mp4')")
    url = shared_url(page)
    recipient, _ = continuity_page(url=url, search_items=[item(), item(source="非凡资源", prefix="alternate")])
    recipient.wait_for_function("document.getElementById('video').readyState >= 2")
    assert recipient.locator("#sourceLabel").inner_text() == "非凡"
    assert "alternate-2" in recipient.locator("#video").get_attribute("src")


@pytest.mark.parametrize("unavailable", ["source", "episode"])
def test_shared_missing_source_or_episode_keeps_actionable_results(continuity_page, unavailable):
    target = item(source="非凡资源", prefix="shared")
    page, _ = continuity_page(stored=json.dumps([{"item": target, "episode": 2, "position": 0, "updated": 1}]))
    page.locator(".history-row").click()
    url = shared_url(page)
    available = item() if unavailable == "source" else {**target, "episodes": target["episodes"][:1]}
    recipient, _ = continuity_page(url=url, search_items=[available])
    assert recipient.locator(".watch-stage").is_hidden()
    assert recipient.locator(".recommend-card").is_visible()
    assert recipient.locator("#searchAllSources").is_visible()
    assert "暂不可用" in recipient.locator("#recommendationsSummary").inner_text()
    assert not recipient.locator("#video").get_attribute("src")


def test_legacy_resume_without_local_record_has_search_results(continuity_page):
    page, _ = continuity_page(url="https://player.example/short-drama/player#resume=测试作品", search_items=[item()])
    assert page.locator(".recommend-card").is_visible()
    assert page.locator("#searchAllSources").is_visible()
    assert "本设备没有这条观看记录" in page.locator("#recommendationsSummary").inner_text()


def test_shared_entry_source_change_updates_public_locator_for_refresh(continuity_page):
    query = {"watch": "1", "title": "测试作品", "source": "暴风资源", "year": "2005", "category": "国产剧", "episode": "第2集", "ep": "2"}
    from urllib.parse import urlencode
    page, _ = continuity_page(url="https://player.example/short-drama/player#" + urlencode(query),
        search_items=[item(), item(source="非凡资源", prefix="alternate")])
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.locator("#sourceButton").click()
    page.locator("#sourceOptions button").filter(has_text="非凡").click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('alternate-2.mp4')")
    locator = parse_qs(urlsplit(page.url).fragment)
    assert locator["source"] == ["非凡资源"]
    assert locator["episode"] == ["第2集"]
    page.reload()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    assert page.locator("#sourceLabel").inner_text() == "非凡"
    assert "alternate-2" in page.locator("#video").get_attribute("src")


def test_missing_shared_source_then_other_result_uses_selected_public_identity(continuity_page):
    from urllib.parse import urlencode
    query = {"watch": "1", "title": "测试作品", "source": "失效来源", "year": "2005", "category": "国产剧", "episode": "第2集", "ep": "2"}
    selected = item("其他作品", "非凡资源", "chosen-result")
    page, _ = continuity_page(url="https://player.example/short-drama/player#" + urlencode(query), search_items=[item(), selected])
    page.locator(".recommend-card").filter(has_text="其他作品").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    locator = parse_qs(urlsplit(page.url).fragment)
    assert locator["watch"] == ["1"]
    assert locator["title"] == ["其他作品"]
    assert locator["source"] == ["非凡资源"]
    assert "i" not in locator and "search" not in locator
    # The selected title now has a different result order, without the original result index.
    page.route("**/api/search?*", lambda route: route.fulfill(json={"v": 1, "kind": "search", "items": [selected, item()]}))
    page.reload()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    assert page.locator("#title").inner_text() == "其他作品"
    assert "chosen-result-1" in page.locator("#video").get_attribute("src")


def test_shared_player_back_opens_search_catalog_that_survives_refresh(continuity_page):
    from urllib.parse import urlencode
    query = {"watch": "1", "title": "测试作品", "source": "暴风资源", "year": "2005", "category": "国产剧", "episode": "第2集", "ep": "2"}
    page, _ = continuity_page(url="https://player.example/short-drama/player#" + urlencode(query), search_items=[item()])
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.locator("#pageBackButton").click()
    locator = parse_qs(urlsplit(page.url).fragment)
    assert locator == {"search": ["测试作品"]}
    assert page.locator(".recommend-card").is_visible()
    page.reload()
    assert page.locator(".watch-stage").is_hidden()
    assert page.locator(".recommend-card").is_visible()
    assert "非法" not in page.locator("#recommendationsTitle").inner_text()
    assert "暂时无法打开" not in page.locator("#recommendationsTitle").inner_text()


def test_search_card_address_refresh_matches_identity_after_source_order_changes(continuity_page):
    selected = item("新剧", "无尽资源", "search-choice")
    other = item("新剧", "暴风资源", "other-source")
    page, _ = continuity_page(search_items=[other, selected])
    page.locator("#catalogSearch").fill("新剧")
    page.locator("#searchAllSources").click()
    page.locator(".recommend-card").filter(has_text="无尽资源").wait_for()
    page.locator("#catalogSearch").fill("新")  # Local filter writes q into the fragment.
    page.evaluate("() => {const url = new URL(location.href); url.hash += '&p=old-packed&r=old-list'; history.replaceState(null, '', url)}")
    page.reload()
    page.locator(".recommend-card").filter(has_text="无尽资源").click()
    page.locator("#episodeGrid").get_by_label("第3集", exact=True).click()
    locator = parse_qs(urlsplit(page.url).fragment)
    assert locator["watch"] == ["1"]
    assert locator["source"] == ["无尽资源"]
    assert set(locator) == {"watch", "title", "source", "year", "category", "episode", "ep"}
    # Clear local state, reorder sources and open the actual address in a clean context.
    recipient, _ = continuity_page(url=page.url, search_items=[selected, other])
    recipient.wait_for_function("document.getElementById('video').readyState >= 2")
    assert recipient.locator("#title").inner_text() == "新剧"
    assert recipient.locator("#sourceLabel").inner_text() == "无尽"
    assert recipient.locator("#meta").inner_text() == "第3集"
    assert "search-choice-3" in recipient.locator("#video").get_attribute("src")
