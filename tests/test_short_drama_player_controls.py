"""Custom player interaction regression checks using a locally generated video, no network."""
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

import pytest

from test_short_drama_player_hardening import _patched_player


@pytest.fixture(scope="module")
def player_assets(tmp_path_factory):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        pytest.skip("ffmpeg is required for the local video fixture")
    root = tmp_path_factory.mktemp("native-player")
    checkout = _patched_player(root)
    media = root / "sample.mp4"
    subprocess.run([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
        "testsrc2=size=320x180:rate=12", "-t", "8", "-c:v", "libx264",
        "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(media),
    ], check=True, timeout=20)
    return (checkout / "index.html").read_text("utf-8"), media.read_bytes()


@pytest.fixture(scope="module")
def browser():
    playwright = pytest.importorskip("playwright.sync_api")
    with playwright.sync_playwright() as runtime:
        chrome = Path("C:/Program Files/Google/Chrome/Application/chrome.exe")
        if not chrome.is_file():
            pytest.skip("Chrome is required for native media control checks")
        instance = runtime.chromium.launch(executable_path=str(chrome))
        yield instance
        instance.close()


@pytest.fixture
def player_page(browser, player_assets, request):
    html, media = player_assets
    page = browser.new_page(viewport={"width": 390, "height": 844}, **(getattr(request, "param", None) or {}))
    page.set_default_timeout(5000)
    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Force the native path; map HLS episode assignments to a local MP4 fixture.
    # HLS transport is checked separately in the manual browser smoke test.
    page.add_init_script("""(() => {
      HTMLMediaElement.prototype.canPlayType = () => 'probably';
      const src = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
      Object.defineProperty(HTMLMediaElement.prototype, 'src', {
        get: src.get,
        set(value) { src.set.call(this, value.replace('.m3u8', '.mp4')); }
      });
      window.customVideoClicks = 0;
      const add = EventTarget.prototype.addEventListener;
      EventTarget.prototype.addEventListener = function(type, ...args) {
        if (this instanceof HTMLVideoElement && type === 'click') window.customVideoClicks++;
        return add.call(this, type, ...args);
      };
    })();""")
    payload = {
        "v": 1, "kind": "recommendations", "heading": "播放测试", "items": [{
            "title": "测试作品", "episodes": [
                {"name": f"第{index}集", "url": f"https://media.example/{index}.m3u8"}
                for index in range(1, 4)
            ],
        }],
    }

    def respond(route):
        path = urlsplit(route.request.url).path
        if path.endswith("/player"):
            route.fulfill(body=html, content_type="text/html; charset=utf-8")
        elif "/api/selections/" in path:
            route.fulfill(json=payload)
        elif path.endswith("/api/recommendations"):
            route.fulfill(json={"v": 1, "kind": "recommendations", "heading": "猜你喜欢", "items": [
                {"title": f"推荐短剧{index}", "category": "短剧", "cover": "", "selection_token": f"abcdefghijklmnop{index}",
                 "episodes": [{"name": "第1集", "url": f"https://media.example/related{index}.m3u8"}]}
                for index in range(1, 10)
            ]})
        elif path.endswith(".mp4"):
            headers = {"Access-Control-Allow-Origin": "*", "Accept-Ranges": "bytes"}
            requested_range = re.fullmatch(r"bytes=(\d+)-(\d*)", route.request.headers.get("range", ""))
            if requested_range:
                start = int(requested_range[1])
                end = min(int(requested_range[2] or len(media) - 1), len(media) - 1)
                headers["Content-Range"] = f"bytes {start}-{end}/{len(media)}"
                route.fulfill(body=media[start:end + 1], status=206,
                              content_type="video/mp4", headers=headers)
            else:
                route.fulfill(body=media, content_type="video/mp4", headers=headers)
        else:
            route.fulfill(body="", content_type="application/javascript")

    page.route("**/*", respond)
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    try:
        page.wait_for_function("document.getElementById('video').readyState >= 2", timeout=5000)
    except Exception:
        details = page.locator("#video").evaluate("video => ({readyState: video.readyState, src: video.currentSrc, error: video.error?.message, status: document.getElementById('status').textContent})")
        page.close()
        pytest.fail(f"Native media fixture failed to load: {details}; page errors: {errors}")
    yield page
    page.close()
    assert not errors, errors


def play_click(page):
    page.locator("#togglePlayback").click()


def test_custom_controls_toggle_once_and_seeking_keeps_pause(player_page):
    page = player_page
    assert page.evaluate("window.customVideoClicks") == 1
    assert not page.locator("#video").evaluate("video => video.controls")
    play_click(page)
    page.wait_for_function("!document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    play_click(page)
    page.wait_for_function("document.getElementById('video').paused")
    page.locator("#video").evaluate("video => { video.currentTime = 3; }")
    page.wait_for_function("document.getElementById('video').currentTime > 2")
    assert page.locator("#video").evaluate("video => video.paused")


def test_episode_switch_and_auto_next_preserve_playback_speed(player_page):
    page = player_page
    page.locator("#speedButton").click()
    play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    page.get_by_label("更多播放设置").click()
    page.get_by_role("button", name="下一集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && !document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    page.locator("#video").evaluate("video => { video.currentTime = video.duration - .2; }")
    page.wait_for_function("document.getElementById('meta').textContent === '第3集' && !document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5


def test_disabled_auto_next_stays_on_current_episode(player_page):
    page = player_page
    page.get_by_label("更多播放设置").click()
    page.locator(".switch-label").click()
    page.get_by_label("更多播放设置").click()
    assert not page.get_by_label("自动连播").is_checked()
    play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    page.locator("#video").evaluate("video => { video.currentTime = video.duration - .2; }")
    page.wait_for_function("document.getElementById('video').ended")
    assert page.locator("#meta").inner_text() == "第1集"


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_overlay_controls_and_episode_sheet_fit_player(player_page, width):
    page = player_page
    page.set_viewport_size({"width": width, "height": 844})
    video = page.locator("#video").bounding_box()
    controls = page.get_by_role("group", name="播放控制").bounding_box()
    assert controls["y"] >= video["y"]
    assert controls["y"] + controls["height"] <= video["y"] + video["height"]
    assert page.locator("#videoShell button").count() > 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.locator("#episodeSheet").is_visible()
    page.locator("#sheetEpisodeGrid").get_by_role("button", name="第2集", exact=True).click()
    assert page.locator("#episodeSheet").is_hidden()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && document.getElementById('video').readyState >= 2")
    assert page.locator("#video").evaluate("video => video.paused")


@pytest.fixture
def cinema_catalog(player_page):
    page = player_page
    titles = ["山海之间", "长风入夜", "山海之间：归途", "雨落长安", "下一站春天", "深海来信", "人间烟火", "逐光"]
    payload = {"v": 1, "kind": "search", "heading": "发现好故事", "items": [
        {"title": title, "year": "2026", "category": "电视剧", "source": "测试片单",
         "cover": "https://media.example/missing-cover.jpg", "episodes": [
             {"name": f"第{index}集", "url": f"https://media.example/{index}.m3u8"}
             for index in range(1, 66)
         ]}
        for title in titles
    ]}
    page.route("**/api/selections/**", lambda route: route.fulfill(json=payload))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").first.wait_for()
    return page


def test_catalog_filter_clear_ime_and_return_keep_context(cinema_catalog):
    page = cinema_catalog
    search = page.get_by_label("在当前片单中查找")
    search.dispatch_event("compositionstart")
    search.fill("山海")
    assert page.locator(".recommend-card:visible").count() == 8
    search.dispatch_event("compositionend")
    assert page.locator(".recommend-card:visible").count() == 2
    page.locator(".recommend-card:visible").first.click()
    page.get_by_label("返回", exact=True).click()
    assert search.input_value() == "山海"
    assert page.locator(".recommend-card:visible").count() == 2
    assert page.evaluate("document.activeElement.classList.contains('recommend-card')")
    assert page.locator("#video").get_attribute("src") is None
    search.fill("不存在的片名")
    assert page.locator("#catalogEmpty").is_visible()
    page.get_by_label("清除片名筛选").click()
    assert page.locator(".recommend-card:visible").count() == 8
    assert search.evaluate("node => node === document.activeElement")
    assert "q=" not in page.url


def test_episode_groups_modal_focus_and_current_state(cinema_catalog):
    page = cinema_catalog
    page.locator(".recommend-card").first.click()
    assert page.locator("#episodeGrid button").count() == 65
    page.locator("#episodeGrid").get_by_label("第32集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第32集'")
    assert page.locator('#episodeGrid [aria-current="true"]').inner_text() == "32"
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.get_by_role("dialog", name="选择剧集").is_visible()
    assert page.locator("#sheetClose").evaluate("node => node === document.activeElement")
    assert not page.locator("#episodeSheet").evaluate("node => node.inert")
    page.keyboard.press("Shift+Tab")
    assert page.locator("#sheetEpisodeGrid button").last.evaluate("node => node === document.activeElement")
    page.keyboard.press("Tab")
    assert page.locator("#sheetClose").evaluate("node => node === document.activeElement")
    page.locator("#sheetEpisodeRanges").get_by_role("button", name="61–65").click()
    assert page.locator("#sheetEpisodeGrid button").count() == 5
    page.keyboard.press("Escape")
    assert page.locator("#episodeSheet").is_hidden()
    assert page.locator("#episodeOpen").evaluate("node => node === document.activeElement")
    assert page.locator("[inert]").count() == 0


@pytest.mark.parametrize("width", [320, 390, 768, 1440])
def test_cinema_catalog_and_player_responsive(cinema_catalog, width):
    page = cinema_catalog
    page.set_viewport_size({"width": width, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Failed posters fall back to title initials without broken-image chrome.
    page.wait_for_function("document.querySelectorAll('.cover img').length === 0")
    screenshots = Path(__file__).parents[1] / "output/playwright/cinema"
    screenshots.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshots / f"catalog-{width}.png"), full_page=True)
    page.locator(".recommend-card").first.click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.screenshot(path=str(screenshots / f"player-{width}.png"), full_page=True)
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    if width == 390:
        assert page.locator("#episodes").bounding_box()["y"] > page.locator("#video").bounding_box()["y"]
    page.get_by_role("button", name="选择剧集", exact=True).click()
    page.screenshot(path=str(screenshots / f"episodes-{width}.png"))
    panel = page.locator(".sheet-panel").bounding_box()
    assert panel["x"] >= 0 and panel["x"] + panel["width"] <= width
    assert panel["y"] >= 0 and panel["y"] + panel["height"] <= 900


def test_speed_cycles_temporary_hint_and_seek_does_not_autoplay(player_page):
    page = player_page
    for rate in [1.5, 2, 1]:
        page.locator("#speedButton").click()
        assert page.locator("#video").evaluate("video => video.playbackRate") == rate
        assert page.locator("#videoHint").inner_text() == f"{rate:.1f} 倍速"
    page.locator("#videoHint").wait_for(state="hidden", timeout=2500)
    assert page.locator("#video").evaluate("video => video.paused")
    page.locator("#seek").focus()
    page.keyboard.press("ArrowRight")
    assert page.locator("#video").evaluate("video => video.currentTime > 0 && video.paused")
    # Clicking the video reveals controls without introducing a second play toggle.
    page.locator("#video").click(position={"x": 60, "y": 100})
    assert page.locator("#video").evaluate("video => video.paused")


def test_strip_follows_episode_and_can_scroll_independently(cinema_catalog):
    page = cinema_catalog
    page.emulate_media(reduced_motion="reduce")
    page.locator(".recommend-card").first.click()
    page.get_by_role("button", name="选择剧集", exact=True).click()
    page.locator("#sheetEpisodeRanges").get_by_role("button", name="31–60").click()
    page.locator("#sheetEpisodeGrid").get_by_label("第32集", exact=True).click()
    page.wait_for_function("document.getElementById('episodeGrid').scrollLeft > 500")
    strip = page.locator("#episodeGrid").bounding_box()
    active = page.locator('#episodeGrid [aria-current="true"]').bounding_box()
    assert abs(active["x"] - strip["x"]) < 3
    page.locator("#episodeGrid").evaluate("node => node.scrollLeft += 100")
    assert page.locator('#episodeGrid [aria-current="true"]').inner_text() == "32"


def test_random_three_refresh_navigation_failure_and_return(cinema_catalog):
    page = cinema_catalog
    page.locator(".recommend-card").first.click()
    page.wait_for_function("document.querySelectorAll('.related-card').length === 3")
    previous_titles = set(page.locator(".related-card strong").all_text_contents())
    page.get_by_role("button", name="换一换").click()
    page.wait_for_function("document.getElementById('related').getAttribute('aria-busy') === 'false'")
    next_titles = set(page.locator(".related-card strong").all_text_contents())
    assert len(next_titles) == 3 and previous_titles.isdisjoint(next_titles)
    selected = page.locator(".related-card strong").first.inner_text()
    page.locator(".related-card").first.click()
    assert page.locator("#title").inner_text() == selected
    assert page.locator("#videoTitle").inner_text() == selected
    assert "i=0" in page.url
    page.wait_for_function("document.getElementById('related').getAttribute('aria-busy') === 'false'")
    assert selected not in page.locator(".related-card strong").all_text_contents()
    page.route("**/api/recommendations", lambda route: route.fulfill(status=503, json={"error": "offline"}))
    page.get_by_role("button", name="换一换").click()
    page.wait_for_function("document.getElementById('relatedStatus').textContent.includes('更新失败')")
    assert page.locator(".related-card").count() == 3
    page.get_by_label("返回", exact=True).click()
    assert page.locator(".recommend-card:visible").count() == 8
    assert page.url.endswith("?s=abcdefghijklmnop#") or page.url.endswith("?s=abcdefghijklmnop")
    assert page.locator("#related").is_hidden()


def test_fullscreen_keeps_controls_and_episode_dialog(cinema_catalog):
    page = cinema_catalog
    page.set_viewport_size({"width": 960, "height": 600})
    page.locator(".recommend-card").first.click()
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.fullscreenElement === document.getElementById('videoShell') || document.getElementById('videoShell').classList.contains('pseudo-fullscreen')")
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.get_by_role("dialog", name="选择剧集").is_visible()
    panel = page.locator(".sheet-panel").bounding_box()
    assert panel["x"] > 450 and panel["height"] <= 568
    page.locator("#sheetEpisodeRanges").get_by_role("button", name="31–60").click()
    page.locator("#sheetEpisodeGrid").get_by_label("第32集", exact=True).click()
    assert page.locator("#meta").inner_text() == "第32集"
    page.get_by_label("退出全屏", exact=True).click()
    page.wait_for_function("!document.fullscreenElement && !document.body.classList.contains('player-fullscreen')")


@pytest.mark.parametrize("player_page", [{"has_touch": True, "is_mobile": True}], indirect=True)
def test_mobile_landscape_video_requests_device_orientation(player_page):
    page = player_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.evaluate("""() => {
      window.orientationCalls = [];
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: async mode => {
        window.orientationCalls.push({mode, native: document.fullscreenElement === document.getElementById('videoShell')});
      }});
    }""")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("window.orientationCalls.length === 1")
    assert page.evaluate("window.orientationCalls") == [{"mode": "landscape", "native": True}]
    shell = page.locator("#videoShell")
    assert shell.evaluate("node => getComputedStyle(node).transform") == "none"
    bounds = shell.bounding_box()
    assert abs(bounds["width"] - 390) < 1 and abs(bounds["height"] - 844) < 1

    page.locator("#speedButton").click()
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    play_click(page)
    page.wait_for_function("document.getElementById('videoShell').classList.contains('controls-hidden')")
    page.wait_for_function("getComputedStyle(document.querySelector('.video-header')).opacity === '0' && getComputedStyle(document.querySelector('.video-controls')).opacity === '0'")
    assert shell.locator(".video-header").evaluate("node => getComputedStyle(node).opacity") == "0"
    assert shell.locator(".video-controls").evaluate("node => getComputedStyle(node).opacity") == "0"
    video_bounds = page.locator("#video").bounding_box()
    page.touchscreen.tap(video_bounds["x"] + 100, video_bounds["y"] + 120)
    assert not shell.evaluate("node => node.classList.contains('controls-hidden')")

    page.get_by_role("button", name="选择剧集", exact=True).click()
    panel = page.locator(".sheet-panel").bounding_box()
    assert panel["x"] >= 0 and panel["y"] >= 0
    assert panel["x"] + panel["width"] <= 390 and panel["y"] + panel["height"] <= 844
    page.locator("#sheetClose").click()
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    page.get_by_label("退出全屏", exact=True).click()
    page.wait_for_function("!document.fullscreenElement")
    assert not page.locator("body").evaluate("node => node.classList.contains('player-fullscreen')")


@pytest.mark.parametrize("player_page", [{"has_touch": True, "is_mobile": True}], indirect=True)
def test_mobile_fullscreen_fallback_does_not_fake_rotation(player_page):
    page = player_page
    page.set_viewport_size({"width": 390, "height": 844})
    page.locator("#videoShell").evaluate("node => { node.requestFullscreen = undefined; node.webkitRequestFullscreen = undefined; }")
    page.evaluate("""() => {
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: async () => { throw Error('unavailable'); }});
    }""")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('videoShell').classList.contains('pseudo-fullscreen')")
    shell = page.locator("#videoShell")
    assert shell.evaluate("node => getComputedStyle(node).transform") == "none"
    assert page.locator("#videoHint").inner_text() == "请将手机横过来观看"
    bounds = shell.bounding_box()
    assert abs(bounds["width"] - 390) < 1 and abs(bounds["height"] - 844) < 1
    page.set_viewport_size({"width": 844, "height": 390})
    bounds = shell.bounding_box()
    assert abs(bounds["width"] - 844) < 1 and abs(bounds["height"] - 390) < 1
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.get_by_role("dialog", name="选择剧集").is_visible()
    page.locator("#sheetClose").click()
    page.get_by_label("退出全屏", exact=True).click()
    assert not shell.evaluate("node => node.classList.contains('pseudo-fullscreen')")


@pytest.mark.parametrize("aspect", [(180, 320, "portrait"), (320, 320, "square"), (420, 180, "cinema")])
def test_video_aspect_fit_with_real_media(player_page, tmp_path, aspect):
    page = player_page
    width, height, mode = aspect
    clip = tmp_path / "aspect.mp4"
    subprocess.run([shutil.which("ffmpeg"), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"testsrc2=size={width}x{height}:rate=12", "-t", "2", "-c:v", "libx264", "-preset", "ultrafast",
                    "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(clip)], check=True, timeout=15)
    page.route("**/aspect.mp4", lambda route: route.fulfill(body=clip.read_bytes(), content_type="video/mp4", headers={"Access-Control-Allow-Origin": "*"}))
    page.locator("#video").evaluate("video => { video.src = 'https://media.example/aspect.mp4'; video.load(); }")
    page.wait_for_function(f"document.getElementById('videoShell').classList.contains('player--{mode}') && document.getElementById('video').readyState >= 2")
    if mode == "portrait":
        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate("""() => {
          window.orientationCalls = [];
          Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: async mode => window.orientationCalls.push(mode)});
        }""")
        page.get_by_label("全屏播放", exact=True).click()
        page.wait_for_function("document.fullscreenElement === document.getElementById('videoShell') || document.getElementById('videoShell').classList.contains('pseudo-fullscreen')")
        assert page.evaluate("window.orientationCalls") == []
        shell = page.locator("#videoShell")
        assert shell.evaluate("node => getComputedStyle(node).transform") == "none"
        bounds = shell.bounding_box()
        assert abs(bounds["width"] - 390) < 1 and abs(bounds["height"] - 844) < 1
        page.get_by_label("退出全屏", exact=True).click()
    for viewport in [{"width": 390, "height": 844}, {"width": 1280, "height": 800}, {"width": 844, "height": 390}]:
        page.set_viewport_size(viewport)
        video = page.locator("#video").bounding_box()
        assert video["width"] <= viewport["width"]
        assert video["height"] <= viewport["height"] * .72
        assert page.locator("#video").evaluate("node => getComputedStyle(node).objectFit") == "contain"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")


@pytest.mark.parametrize("player_page", [None, {"has_touch": True, "is_mobile": True}], indirect=True)
def test_compact_link_recovers_cover_full_strip_and_next_episode(player_page):
    import base64
    import json
    import zlib
    page = player_page
    compact = {"v": 1, "kind": "recommendations", "heading": "旧片单", "items": [{
        "title": "独剑九天", "source": "量子资源", "episode_count": 16,
        "episodes": [{"name": "第01集", "url": "https://media.example/1.m3u8"}],
    }]}
    full = {"v": 1, "kind": "search", "heading": "独剑九天", "items": [{
        "title": "独剑九天", "source": "量子资源", "cover": "https://media.example/poster.png",
        "episodes": [{"name": f"第{index:02}集", "url": f"https://media.example/{index}.m3u8"} for index in range(1, 17)],
    }]}
    png = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jf1sAAAAASUVORK5CYII=")
    page.route("**/poster.png", lambda route: route.fulfill(body=png, content_type="image/png"))
    requests = []
    def resolve(route):
        requests.append(route.request.url)
        route.fulfill(json=full)
    page.route("**/api/resolve?*", resolve)
    token = base64.urlsafe_b64encode(zlib.compress(json.dumps(compact).encode())).decode().rstrip("=")
    page.goto(f"https://player.example/short-drama/player#r={token}")
    page.wait_for_function("document.querySelector('.cover img')?.naturalWidth > 0")
    page.locator(".recommend-card").click()
    page.wait_for_function("document.querySelectorAll('#episodeGrid button').length === 16")
    assert page.locator("#episodes").is_visible()
    assert len(requests) == 1
    assert "https%3A" not in requests[0]  # Resolver accepts a fingerprint, never a fetch URL.
    page.get_by_label("更多播放设置").click()
    assert page.get_by_role("button", name="上一集", exact=True).is_disabled()
    assert page.get_by_role("button", name="下一集", exact=True).is_enabled()
    page.get_by_role("button", name="下一集", exact=True).click()
    assert page.locator("#meta").inner_text() == "第02集"


@pytest.mark.parametrize("player_page", [None, {"has_touch": True, "is_mobile": True}], indirect=True)
def test_picture_taps_toggle_chrome_without_toggling_playback(player_page, request):
    page = player_page
    for hidden in [True, False, True, False]:
        if (request.node.callspec.params["player_page"] or {}).get("has_touch"):
            box = page.locator("#video").bounding_box()
            page.touchscreen.tap(box["x"] + 180, box["y"] + 100)
        else:
            page.locator("#video").click(position={"x": 180, "y": 100})
        assert page.locator("#videoShell").evaluate("node => node.classList.contains('controls-hidden')") == hidden
        assert page.locator("#video").evaluate("video => video.paused")


@pytest.mark.parametrize("player_page", [None, {"has_touch": True, "is_mobile": True}], indirect=True)
def test_speed_is_inline_and_touch_navigation_has_no_gold_focus(player_page):
    page = player_page
    for width in [320, 390]:
        page.set_viewport_size({"width": width, "height": 844})
        speed = page.locator("#speedButton").bounding_box()
        fullscreen = page.locator("#fullscreenButton").bounding_box()
        assert speed["x"] + speed["width"] <= fullscreen["x"] + 1
        assert abs(speed["y"] - fullscreen["y"]) < 1
        assert page.locator("#speedButton").evaluate("node => getComputedStyle(node).color") == page.locator("#fullscreenButton").evaluate("node => getComputedStyle(node).color")
        assert page.locator("#pageBackButton").evaluate("node => getComputedStyle(node).outlineStyle") == "none"
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.locator(".related-card").first.click()
    assert page.locator("#togglePlayback").evaluate("node => node !== document.activeElement")


def test_compact_metadata_failure_can_retry_without_losing_playback(player_page):
    page = player_page
    compact = {"v": 1, "kind": "search", "heading": "旧片单", "items": [{
        "title": "旧作品", "source": "量子资源", "episode_count": 3,
        "episodes": [{"name": "第1集", "url": "https://media.example/1.m3u8"}],
    }]}
    full = {"v": 1, "kind": "search", "heading": "旧作品", "items": [{
        "title": "旧作品", "source": "量子资源", "cover": "",
        "episodes": [{"name": f"第{index}集", "url": f"https://media.example/{index}.m3u8"} for index in range(1, 4)],
    }]}
    page.route("**/api/selections/**", lambda route: route.fulfill(json=compact))
    page.route("**/api/resolve?*", lambda route: route.fulfill(status=503, json={"error": "unavailable"}))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    page.locator("#metadataRetry").wait_for(state="visible")
    assert page.locator("#title").inner_text() == "旧作品"
    assert page.locator("#video").evaluate("video => video.paused")

    page.route("**/api/resolve?*", lambda route: route.fulfill(json=full))
    page.locator("#metadataRetry").click()
    page.wait_for_function("document.querySelectorAll('#episodeGrid button').length === 3")
    assert page.locator("#metadataRetry").is_hidden()
    assert page.locator("#metadataStatus").inner_text() == ""
    assert page.locator("#video").evaluate("video => video.paused")


@pytest.mark.parametrize("player_page", [{"has_touch": True, "is_mobile": True}], indirect=True)
def test_mobile_sheet_owns_touch_scroll_and_restores_background(player_page):
    page = player_page
    page.set_viewport_size({"width": 375, "height": 480})
    payload = {"v": 1, "kind": "search", "heading": "手机片单", "items": [{
        "title": "手机滚动测试" + "长片名" * 25,
        "actor": "演员名单" * 40,
        "episodes": [{"name": f"第{index}集", "url": f"https://media.example/{index}.m3u8"} for index in range(1, 181)],
    }]}
    page.route("**/api/selections/**", lambda route: route.fulfill(json=payload))
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    page.evaluate("window.scrollTo(0, 60)")
    previous_scroll = page.evaluate("window.scrollY")
    previous_top = page.locator("#videoShell").bounding_box()["y"]
    assert previous_scroll == 60
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.locator("body").evaluate("node => getComputedStyle(node).position") == "fixed"
    assert abs(page.locator("#videoShell").bounding_box()["y"] - previous_top) < 1
    assert page.evaluate("document.elementFromPoint(60, innerHeight - 80).closest('#episodeSheet') !== null")
    assert page.locator(".sheet-panel").evaluate("node => node.scrollHeight > node.clientHeight")
    cdp = page.context.new_cdp_session(page)
    def swipe(x, y, end_x, end_y):
        cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y}]})
        for step in range(1, 11):
            cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x + (end_x - x) * step / 10, "y": y + (end_y - y) * step / 10}]})
            page.wait_for_timeout(20)
        cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})
    panel = page.locator(".sheet-panel").bounding_box()
    swipe(180, panel["y"] + 260, 180, panel["y"] + 140)
    page.wait_for_function("document.querySelector('.sheet-panel').scrollTop > 20")
    assert abs(page.locator("#videoShell").bounding_box()["y"] - previous_top) < 1
    page.locator(".sheet-panel").evaluate("node => node.scrollTop = 0")
    ranges = page.locator("#sheetEpisodeRanges").bounding_box()
    swipe(280, ranges["y"] + 20, 80, ranges["y"] + 20)
    page.wait_for_function("document.getElementById('sheetEpisodeRanges').scrollLeft > 30")
    assert abs(page.locator("#videoShell").bounding_box()["y"] - previous_top) < 1
    swipe(190, 30, 190, 65)  # Backdrop cannot move the background.
    assert abs(page.locator("#videoShell").bounding_box()["y"] - previous_top) < 1
    screenshots = Path(__file__).parents[1] / "output/playwright/cinema"
    page.screenshot(path=str(screenshots / "mobile-sheet-scroll-375.png"))
    page.locator("#sheetClose").click()
    assert page.evaluate("window.scrollY") == previous_scroll
    assert page.locator("body").evaluate("node => getComputedStyle(node).position") != "fixed"
    page.evaluate("window.scrollTo(0, 120)")
    assert page.evaluate("window.scrollY") == 120
