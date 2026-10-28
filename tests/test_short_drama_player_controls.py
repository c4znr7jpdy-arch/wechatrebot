"""Custom player interaction regression checks using a locally generated video, no network."""
import re
import subprocess
from urllib.parse import parse_qs, urlsplit

import imageio_ffmpeg
import pytest
from playwright.sync_api import sync_playwright

from test_short_drama_player_hardening import _patched_player


IOS_BROWSER = {
    "has_touch": True,
    "is_mobile": True,
    "user_agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Version/18.0 Mobile/15E148 Safari/604.1",
}
ANDROID_BROWSER = {
    "has_touch": True,
    "is_mobile": True,
    "user_agent": "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36 MicroMessenger/8.0.54",
}


@pytest.fixture(scope="module")
def player_assets(tmp_path_factory):
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
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
    with sync_playwright() as runtime:
        instance = runtime.chromium.launch()
        yield instance
        instance.close()


@pytest.fixture(scope="module")
def screenshots(tmp_path_factory):
    return tmp_path_factory.mktemp("player-screenshots")


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
        if (type === 'click' && this.id === 'fullscreenButton' && typeof args[0] === 'function') {
          const listener = args[0];
          args[0] = function(event) {
            window.fullscreenClickInProgress = true;
            try { return listener.call(this, event); }
            finally { window.fullscreenClickInProgress = false; }
          };
        }
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


def exit_fullscreen(page):
    page.get_by_label("退出全屏", exact=True).click()
    # The handler awaits orientation restoration before removing fullscreen state.
    page.wait_for_function("""!document.fullscreenElement
      && !document.getElementById('video').webkitDisplayingFullscreen
      && !document.getElementById('videoShell').classList.contains('pseudo-fullscreen')
      && !document.body.classList.contains('player-fullscreen')""")


def mock_video_fullscreen(page, *, enter_method="webkitEnterFullscreen", mode="success"):
    """Emulate Safari's fullscreen API and events, while retaining real media playback."""
    page.evaluate("""({enterMethod, mode}) => {
      const video = document.getElementById('video');
      window.videoFullscreenCalls = [];
      window.orientationCalls = [];
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: async value => window.orientationCalls.push(value)});
      Object.defineProperty(screen.orientation, 'unlock', {configurable: true, value: () => window.orientationCalls.push('unlock')});
      video.__displayingFullscreen = false;
      Object.defineProperty(video, 'webkitDisplayingFullscreen', {configurable: true, get: () => video.__displayingFullscreen});
      Object.defineProperty(video, 'webkitSupportsFullscreen', {configurable: true, value: true});
      window.beginVideoFullscreen = () => {
        video.__displayingFullscreen = true;
        video.dispatchEvent(new Event('webkitbeginfullscreen'));
      };
      window.endVideoFullscreen = () => {
        video.__displayingFullscreen = false;
        video.dispatchEvent(new Event('webkitendfullscreen'));
      };
      video.webkitEnterFullscreen = undefined;
      video.webkitEnterFullScreen = undefined;
      video[enterMethod] = () => {
        window.videoFullscreenCalls.push({action: 'enter', synchronous: window.fullscreenClickInProgress === true, active: navigator.userActivation.isActive});
        if (mode === 'rejected') throw new Error('native fullscreen denied');
        if (mode === 'success') window.beginVideoFullscreen();
      };
      video.webkitExitFullscreen = () => {
        window.videoFullscreenCalls.push({action: 'exit'});
        window.endVideoFullscreen();
      };
    }""", {"enterMethod": enter_method, "mode": mode})


def observe_episode_handoff(page):
    page.evaluate("""() => {
      const video = document.getElementById('video');
      const descriptor = Object.getOwnPropertyDescriptor(HTMLMediaElement.prototype, 'src');
      const play = video.play.bind(video), load = video.load.bind(video), remove = video.removeAttribute.bind(video);
      window.handoffVideo = video;
      window.mediaHandoff = [];
      let sourceTurn = false;
      Object.defineProperty(video, 'src', {configurable: true, get: () => descriptor.get.call(video), set(value) {
        window.mediaHandoff.push({action: 'source', value});
        sourceTurn = true;
        descriptor.set.call(video, value);
        queueMicrotask(() => { sourceTurn = false; });
      }});
      video.play = () => {
        window.mediaHandoff.push({action: 'play', sameTurn: sourceTurn});
        return play();
      };
      video.load = () => {
        window.mediaHandoff.push({action: video.getAttribute('src') ? 'load' : 'empty-load'});
        return load();
      };
      video.removeAttribute = name => {
        if (name === 'src') window.mediaHandoff.push({action: 'remove-source'});
        return remove(name);
      };
    }""")


def assert_real_fullscreen_only(page):
    assert page.locator("#videoShell").evaluate("node => !node.classList.contains('pseudo-fullscreen') && !node.classList.contains('pseudo-landscape')")
    assert page.locator("#videoShell").evaluate("node => getComputedStyle(node).transform") == "none"


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
def test_cinema_catalog_and_player_responsive(cinema_catalog, width, screenshots):
    page = cinema_catalog
    page.set_viewport_size({"width": width, "height": 900})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    # Failed posters fall back to title initials without broken-image chrome.
    page.wait_for_function("document.querySelectorAll('.cover img').length === 0")
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
    page.wait_for_function("document.fullscreenElement === document.getElementById('videoShell')")
    assert_real_fullscreen_only(page)
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.get_by_role("dialog", name="选择剧集").is_visible()
    panel = page.locator(".sheet-panel").bounding_box()
    assert panel["x"] > 450 and panel["height"] <= 568
    page.locator("#sheetEpisodeRanges").get_by_role("button", name="31–60").click()
    page.locator("#sheetEpisodeGrid").get_by_label("第32集", exact=True).click()
    assert page.locator("#meta").inner_text() == "第32集"
    exit_fullscreen(page)


@pytest.mark.parametrize("player_page", [ANDROID_BROWSER], indirect=True)
def test_fullscreen_back_button_exits_before_navigation(cinema_catalog):
    page = cinema_catalog
    search = page.get_by_label("在当前片单中查找")
    search.fill("山海")
    filtered_titles = page.locator(".recommend-card:visible strong").all_text_contents()
    assert len(filtered_titles) == 2
    page.locator(".recommend-card").first.click()
    page.locator("#episodeGrid").get_by_label("第2集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && document.getElementById('video').readyState >= 2")
    page.locator("#speedButton").click()
    play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    page.locator("#video").evaluate("video => { video.currentTime = 2; }")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.fullscreenElement === document.getElementById('videoShell')")
    before = page.locator("#video").evaluate("""video => {
      window.backNavigationVideo = video;
      return {src: video.currentSrc, time: video.currentTime, rate: video.playbackRate};
    }""")

    page.get_by_label("返回", exact=True).click()
    page.wait_for_function("!document.fullscreenElement && !document.body.classList.contains('player-fullscreen')")
    assert page.locator(".watch-stage").is_visible()
    assert page.locator("#recommendations").is_hidden()
    assert page.locator("#meta").inner_text() == "第2集"
    after = page.locator("#video").evaluate("""video => ({
      same: video === window.backNavigationVideo, src: video.currentSrc,
      time: video.currentTime, rate: video.playbackRate, paused: video.paused
    })""")
    assert after["same"] and not after["paused"]
    assert after["src"] == before["src"]
    assert after["rate"] == before["rate"] == 1.5
    assert after["time"] >= before["time"] - .1
    assert_real_fullscreen_only(page)

    page.get_by_label("返回", exact=True).click()
    assert page.locator(".watch-stage").is_hidden()
    assert page.locator("#recommendations").is_visible()
    assert search.input_value() == "山海"
    assert page.locator(".recommend-card:visible strong").all_text_contents() == filtered_titles
    assert page.locator("#video").get_attribute("src") is None


@pytest.mark.parametrize("phase", ["enter", "exit", "orientation"])
@pytest.mark.parametrize("player_page", [ANDROID_BROWSER], indirect=True)
def test_fullscreen_back_button_ignores_repeated_clicks_during_transition(player_page, phase):
    page = player_page
    page.evaluate("""phase => {
      const video = document.getElementById('video'), shell = document.getElementById('videoShell');
      const exit = document.exitFullscreen.bind(document);
      window.backNavigationVideo = video;
      window.fullscreenBackCalls = {exit: 0, restore: 0};
      Object.defineProperty(screen.orientation, 'type', {configurable: true, value: 'portrait-primary'});
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: mode => {
        if (mode === 'landscape') return Promise.resolve();
        window.fullscreenBackCalls.restore++;
        return phase === 'orientation' ? new Promise(resolve => { window.finishFullscreenTransition = resolve; }) : Promise.resolve();
      }});
      Object.defineProperty(screen.orientation, 'unlock', {configurable: true, value: () => {}});
      if (phase === 'enter') shell.requestFullscreen = () => new Promise((_resolve, reject) => {
        window.finishFullscreenTransition = () => reject(new Error('deferred entry denied'));
      });
      document.exitFullscreen = () => {
        window.fullscreenBackCalls.exit++;
        return phase === 'exit' ? new Promise(resolve => { window.finishFullscreenTransition = () => exit().then(resolve); }) : exit();
      };
    }""", phase)
    source = page.locator("#video").get_attribute("src")
    page.get_by_label("全屏播放", exact=True).click()
    if phase != "enter":
        page.wait_for_function("document.fullscreenElement === document.getElementById('videoShell')")
    pending = page.evaluate("""async phase => {
      const back = document.getElementById('pageBackButton');
      if (phase !== 'enter') back.click();
      await new Promise(resolve => setTimeout(resolve, 0));
      for (let click = 0; click < 3; click++) back.click();
      return {...window.fullscreenBackCalls,
        watching: !document.querySelector('.watch-stage').hidden,
        catalogHidden: getComputedStyle(document.getElementById('recommendations')).display === 'none',
        sameVideo: window.backNavigationVideo === document.getElementById('video'),
        src: document.getElementById('video').getAttribute('src')};
    }""", phase)
    assert pending["watching"] and pending["catalogHidden"] and pending["sameVideo"]
    assert pending["src"] == source
    assert pending["exit"] == (1 if phase == "exit" else 0)
    assert pending["restore"] == (0 if phase == "enter" else 1)
    page.evaluate("window.finishFullscreenTransition()")
    page.wait_for_function("!document.fullscreenElement && !document.body.classList.contains('player-fullscreen')")
    if phase == "enter":
        page.wait_for_function("document.getElementById('videoHint').textContent.includes('全屏')")
    assert page.evaluate("window.fullscreenBackCalls.exit") == (0 if phase == "enter" else 1)
    assert page.locator(".watch-stage").is_visible()
    assert page.locator("#recommendations").is_hidden()
    assert page.locator("#video").get_attribute("src") == source


@pytest.mark.parametrize("player_page", [{"has_touch": True, "is_mobile": True}], indirect=True)
def test_mobile_landscape_video_requests_device_orientation(player_page, tmp_path):
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
    page.screenshot(path=str(tmp_path / "container-fullscreen.png"))

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
    exit_fullscreen(page)
    assert not page.locator("body").evaluate("node => node.classList.contains('player-fullscreen')")


@pytest.mark.parametrize("mode", ["missing", "rejected", "silent", "native-rejected", "native-silent"])
@pytest.mark.parametrize("player_page", [IOS_BROWSER], indirect=True)
def test_fullscreen_unavailable_shows_hint_without_faking_fullscreen(player_page, mode):
    page = player_page
    page.locator("#videoShell").evaluate("""(node, mode) => {
      node.webkitRequestFullscreen = undefined;
      node.requestFullscreen = mode === 'rejected' ? async () => { throw new Error('denied'); }
        : mode === 'silent' ? async () => {} : undefined;
    }""", mode)
    if mode.startswith("native-"):
        mock_video_fullscreen(page, mode=mode.removeprefix("native-"))
    else:
        page.locator("#video").evaluate("node => { node.webkitEnterFullscreen = undefined; node.webkitEnterFullScreen = undefined; }")
    before = page.locator("#videoShell").bounding_box()
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('videoHint').textContent.includes('全屏') && !document.getElementById('videoHint').hidden")
    assert_real_fullscreen_only(page)
    assert page.evaluate("!document.fullscreenElement && !document.getElementById('video').webkitDisplayingFullscreen")
    assert not page.locator("body").evaluate("node => node.classList.contains('player-fullscreen')")
    assert page.get_by_label("全屏播放", exact=True).is_visible()
    after = page.locator("#videoShell").bounding_box()
    assert abs(after["width"] - before["width"]) < 1 and abs(after["height"] - before["height"]) < 1


@pytest.mark.parametrize("enter_method", ["webkitEnterFullscreen", "webkitEnterFullScreen"])
@pytest.mark.parametrize("player_page", [IOS_BROWSER], indirect=True)
def test_ios_prefers_video_fullscreen_synchronously_and_exit_restores_controls(player_page, enter_method):
    page = player_page
    mock_video_fullscreen(page, enter_method=enter_method)
    page.locator("#videoShell").evaluate("""node => {
      window.containerFullscreenCalls = 0;
      node.requestFullscreen = () => { window.containerFullscreenCalls++; return Promise.resolve(); };
    }""")
    assert page.get_by_label("自动连播").is_checked()
    assert page.get_by_label("全屏播放", exact=True).count() == 1
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('video').webkitDisplayingFullscreen")
    assert page.evaluate("window.videoFullscreenCalls") == [{"action": "enter", "synchronous": True, "active": True}]
    assert page.evaluate("window.containerFullscreenCalls") == 0
    assert page.evaluate("window.orientationCalls") == []
    assert_real_fullscreen_only(page)
    exit_fullscreen(page)
    assert page.evaluate("window.videoFullscreenCalls.at(-1)") == {"action": "exit"}
    assert page.evaluate("window.orientationCalls") == []
    page.locator("#speedButton").click()
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    page.get_by_role("button", name="选择剧集", exact=True).click()
    page.locator("#sheetEpisodeGrid").get_by_role("button", name="第2集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && document.getElementById('video').readyState >= 2")
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    assert page.locator("[inert]").count() == 0


@pytest.mark.parametrize("container", ["missing", "rejected"])
def test_video_fullscreen_fallback_uses_native_state_without_orientation_lock(player_page, container):
    page = player_page
    mock_video_fullscreen(page)
    page.locator("#videoShell").evaluate("""(node, mode) => {
      node.webkitRequestFullscreen = undefined;
      node.requestFullscreen = mode === 'rejected' ? async () => { throw new Error('container denied'); } : undefined;
    }""", container)
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('video').webkitDisplayingFullscreen")
    assert_real_fullscreen_only(page)
    assert page.evaluate("window.orientationCalls") == []
    page.evaluate("window.endVideoFullscreen()")
    page.wait_for_function("!document.body.classList.contains('player-fullscreen')")
    assert page.get_by_label("全屏播放", exact=True).is_visible()
    assert page.locator("body").evaluate("node => getComputedStyle(node).overflowY") != "hidden"
    assert page.evaluate("window.orientationCalls") == []


def test_container_rejection_consumes_gesture_and_next_click_uses_native_directly(player_page):
    page = player_page
    mock_video_fullscreen(page)
    page.evaluate("""() => {
      window.containerFullscreenCalls = 0;
      window.fullscreenActivation = true;
      Object.defineProperty(navigator.userActivation, 'isActive', {configurable: true, get: () => window.fullscreenActivation});
      document.getElementById('videoShell').requestFullscreen = async () => {
        window.containerFullscreenCalls++;
        window.fullscreenActivation = false;
        throw new Error('container denied after consuming gesture');
      };
    }""")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('videoHint').textContent.includes('全屏') && !document.getElementById('videoHint').hidden")
    assert page.evaluate("window.videoFullscreenCalls") == []
    assert page.evaluate("window.containerFullscreenCalls") == 1
    assert_real_fullscreen_only(page)
    page.evaluate("window.fullscreenActivation = true")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('video').webkitDisplayingFullscreen")
    assert page.evaluate("window.videoFullscreenCalls") == [{"action": "enter", "synchronous": True, "active": True}]
    assert page.evaluate("window.containerFullscreenCalls") == 1
    assert page.evaluate("window.orientationCalls") == []


@pytest.mark.parametrize("player_page", [IOS_BROWSER], indirect=True)
def test_native_fullscreen_waits_for_begin_event_and_handles_system_exit(player_page):
    page = player_page
    mock_video_fullscreen(page, mode="deferred")
    page.get_by_label("全屏播放", exact=True).click()
    assert not page.locator("body").evaluate("node => node.classList.contains('player-fullscreen')")
    assert page.get_by_label("全屏播放", exact=True).is_visible()
    page.evaluate("window.beginVideoFullscreen()")
    page.wait_for_function("document.body.classList.contains('player-fullscreen')")
    assert page.get_by_label("退出全屏", exact=True).is_visible()
    assert_real_fullscreen_only(page)
    page.evaluate("window.endVideoFullscreen()")
    page.wait_for_function("!document.body.classList.contains('player-fullscreen')")
    assert page.get_by_label("全屏播放", exact=True).is_visible()
    assert page.evaluate("window.orientationCalls") == []


@pytest.mark.parametrize("player_page", [IOS_BROWSER], indirect=True)
def test_native_fullscreen_auto_next_keeps_video_and_speed_then_can_be_disabled(player_page):
    page = player_page
    mock_video_fullscreen(page)
    page.locator("#speedButton").click()
    play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("document.getElementById('video').webkitDisplayingFullscreen")
    observe_episode_handoff(page)
    for episode in [2, 3]:
        page.locator("#video").evaluate("video => { video.currentTime = video.duration - .2; }")
        page.wait_for_function(f"document.getElementById('meta').textContent === '第{episode}集' && !document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
        assert page.evaluate("window.handoffVideo === document.getElementById('video')")
        assert page.locator("#video").evaluate("video => video.webkitDisplayingFullscreen && video.playbackRate === 1.5")
    events = page.evaluate("window.mediaHandoff")
    assert len([event for event in events if event["action"] == "source"]) == 2
    assert not any(event["action"] in {"remove-source", "empty-load"} for event in events)
    for index, event in enumerate(events):
        if event["action"] == "source":
            assert events[index + 1] == {"action": "play", "sameTurn": True}
    page.evaluate("window.endVideoFullscreen()")
    page.get_by_label("更多播放设置").click()
    page.locator(".switch-label").click()
    assert not page.get_by_label("自动连播").is_checked()
    page.get_by_label("更多播放设置").click()
    page.get_by_role("button", name="选择剧集", exact=True).click()
    page.locator("#sheetEpisodeGrid").get_by_role("button", name="第1集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第1集' && !document.getElementById('video').paused")
    page.get_by_label("全屏播放", exact=True).click()
    page.locator("#video").evaluate("video => { video.currentTime = video.duration - .2; }")
    page.wait_for_function("document.getElementById('video').ended")
    assert page.locator("#meta").inner_text() == "第1集"
    assert page.locator("#video").evaluate("video => video.webkitDisplayingFullscreen")
    assert page.evaluate("window.orientationCalls") == []


def test_episode_metadata_does_not_override_pause_after_immediate_play(player_page):
    page = player_page
    page.locator("#speedButton").click()
    play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    observe_episode_handoff(page)
    page.evaluate("""() => {
      const video = document.getElementById('video');
      const add = video.addEventListener.bind(video);
      window.metadataReadyListeners = [];
      video.addEventListener = (type, listener, options) => {
        if (type === 'loadedmetadata' && options?.once) window.metadataReadyListeners.push(listener);
        else add(type, listener, options);
      };
    }""")
    page.get_by_label("更多播放设置").click()
    page.get_by_role("button", name="下一集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && !document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    play_click(page)
    page.wait_for_function("document.getElementById('video').paused")
    page.evaluate("window.metadataReadyListeners.forEach(listener => listener(new Event('loadedmetadata')))")
    events = page.evaluate("window.mediaHandoff")
    assert events[:2] == [{"action": "source", "value": "https://media.example/2.m3u8"}, {"action": "play", "sameTurn": True}]
    assert len([event for event in events if event["action"] == "play"]) == 1
    assert page.locator("#video").evaluate("video => video.paused")
    assert not any(event["action"] in {"remove-source", "empty-load"} for event in events)
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5


@pytest.mark.parametrize("restore", ["success", "rejected", "pending"])
@pytest.mark.parametrize("initial", ["portrait-primary", "landscape-secondary"])
def test_fullscreen_exit_restores_entry_orientation_without_retaining_lock(player_page, restore, initial):
    page = player_page
    page.evaluate("""({restore, initial}) => {
      window.orientationCalls = [];
      Object.defineProperty(screen.orientation, 'type', {configurable: true, value: initial});
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: mode => {
        window.orientationCalls.push(mode);
        if (mode === 'landscape') return Promise.resolve();
        if (restore === 'rejected') return Promise.reject(new Error('unsupported'));
        if (restore === 'pending') return new Promise(resolve => { window.resolveRestore = resolve; });
        return Promise.resolve();
      }});
      Object.defineProperty(screen.orientation, 'unlock', {configurable: true, value: () => window.orientationCalls.push('unlock')});
    }""", {"restore": restore, "initial": initial})
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("window.orientationCalls.includes('landscape')")
    exit_fullscreen(page)
    calls = page.evaluate("window.orientationCalls")
    assert calls[:2] == ["landscape", initial]
    assert calls[-1] == "unlock"
    if restore == "pending":
        # A late restoration cannot release the lock of a newly opened player.
        page.get_by_label("全屏播放", exact=True).click()
        page.wait_for_function("window.orientationCalls.filter(value => value === 'landscape').length === 2")
        calls = page.evaluate("window.orientationCalls")
        page.evaluate("window.resolveRestore()")
        page.wait_for_timeout(50)
        assert page.evaluate("window.orientationCalls") == calls


def test_system_fullscreen_exit_releases_successful_orientation_lock(player_page):
    page = player_page
    page.evaluate("""() => {
      window.orientationCalls = [];
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: async mode => window.orientationCalls.push(mode)});
      Object.defineProperty(screen.orientation, 'unlock', {configurable: true, value: () => window.orientationCalls.push('unlock')});
    }""")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("window.orientationCalls.includes('landscape')")
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function("!document.body.classList.contains('player-fullscreen')")
    assert page.evaluate("window.orientationCalls") == ["landscape", "unlock"]


@pytest.mark.parametrize("reenter", [False, True])
def test_delayed_orientation_lock_cannot_survive_exit_or_unlock_new_session(player_page, reenter):
    page = player_page
    page.evaluate("""() => {
      window.orientationCalls = [];
      Object.defineProperty(screen.orientation, 'lock', {configurable: true, value: mode => {
        window.orientationCalls.push(mode);
        if (window.orientationCalls.filter(value => value === 'landscape').length === 1) {
          return new Promise(resolve => { window.resolveLandscape = resolve; });
        }
        return Promise.resolve();
      }});
      Object.defineProperty(screen.orientation, 'unlock', {configurable: true, value: () => window.orientationCalls.push('unlock')});
    }""")
    page.get_by_label("全屏播放", exact=True).click()
    page.wait_for_function("window.orientationCalls.includes('landscape')")
    # System exits can happen while the Screen Orientation promise is still pending.
    page.evaluate("document.exitFullscreen()")
    page.wait_for_function("!document.body.classList.contains('player-fullscreen')")
    assert page.evaluate("window.orientationCalls.at(-1)") == "unlock"
    if reenter:
        page.get_by_label("全屏播放", exact=True).click()
        page.wait_for_function("window.orientationCalls.filter(value => value === 'landscape').length === 2")
    calls = page.evaluate("window.orientationCalls")
    page.evaluate("window.resolveLandscape()")
    page.wait_for_timeout(50)
    if reenter:
        assert page.evaluate("window.orientationCalls") == calls
    else:
        assert page.evaluate("window.orientationCalls") == calls + ["unlock"]


@pytest.mark.parametrize("aspect", [(180, 320, "portrait"), (320, 320, "square"), (420, 180, "cinema")])
def test_video_aspect_fit_with_real_media(player_page, tmp_path, aspect):
    page = player_page
    width, height, mode = aspect
    clip = tmp_path / "aspect.mp4"
    subprocess.run([imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
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
        page.wait_for_function("document.fullscreenElement === document.getElementById('videoShell')")
        assert_real_fullscreen_only(page)
        assert page.evaluate("window.orientationCalls") == []
        shell = page.locator("#videoShell")
        assert shell.evaluate("node => getComputedStyle(node).transform") == "none"
        bounds = shell.bounding_box()
        assert abs(bounds["width"] - 390) < 1 and abs(bounds["height"] - 844) < 1
        exit_fullscreen(page)
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
def test_mobile_sheet_owns_touch_scroll_and_restores_background(player_page, screenshots):
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
    page.screenshot(path=str(screenshots / "mobile-sheet-scroll-375.png"))
    page.locator("#sheetClose").click()
    assert page.evaluate("window.scrollY") == previous_scroll
    assert page.locator("body").evaluate("node => getComputedStyle(node).position") != "fixed"
    page.evaluate("window.scrollTo(0, 120)")
    assert page.evaluate("window.scrollY") == 120


@pytest.fixture
def source_catalog(player_page):
    """A single search result with two independently served playback sources."""
    page = player_page
    variants = [
        {
            "title": "欢天喜地七仙女", "year": "2005", "category": "国产剧",
            "actor": "霍思燕,郑国霖", "source": source,
            "cover": "https://media.example/poster.png", "episode_count": 3,
            "episodes": [
                {"name": f"第{index}集", "url": f"https://media.example/{code}-{index}.m3u8"}
                for index in range(1, 4)
            ],
        }
        for source, code in [("暴风资源", "bf"), ("非凡资源", "ff")]
    ]
    payload = {"v": 1, "kind": "search", "heading": "搜索结果", "items": [variants[0]]}
    state = {"variants": variants, "requests": [], "fail": False}

    def resolve(route):
        query = parse_qs(urlsplit(route.request.url).query)
        state["requests"].append(query)
        assert query.get("alternatives") == ["1"]
        if state["fail"]:
            route.fulfill(status=503, json={"error": "temporarily unavailable"})
        else:
            route.fulfill(json={**payload, "items": state["variants"]})

    page.route("**/api/selections/**", lambda route: route.fulfill(json=payload))
    page.route("**/api/resolve?*", resolve)
    page.goto("https://player.example/short-drama/player?s=abcdefghijklmnop")
    page.locator(".recommend-card").click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    return page, state


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_source_switch_button_is_compact_clickable_and_fits_viewport(source_catalog, width, screenshots):
    import hashlib
    page, state = source_catalog
    state["variants"].extend(
        {**state["variants"][1], "source": source}
        for source in ("量子资源", "红牛资源", "极速资源", "金鹰资源", "飘零资源")
    )
    page.set_viewport_size({"width": width, "height": 844})
    button = page.locator("#sourceButton")
    assert button.inner_text().strip() == "暴风"
    assert button.is_enabled()
    assert not state["requests"]  # Opening the default result does not query every source.
    assert button.evaluate("node => parseFloat(getComputedStyle(node).fontSize)") <= 13
    bounds = button.bounding_box()
    assert bounds["x"] >= 0 and bounds["x"] + bounds["width"] <= width
    assert bounds["height"] >= 32
    button.click()
    alternative = page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True)
    alternative.wait_for(state="visible")
    assert page.locator("#sourceMenu").get_by_role("button", name="暴风", exact=True).get_attribute("aria-pressed") == "true"
    assert state["requests"][0]["episode"] == [hashlib.sha256(b"https://media.example/bf-1.m3u8").hexdigest()]
    assert state["requests"][0]["year"] == ["2005"]
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    menu = page.locator("#sourceMenu").bounding_box()
    assert menu["x"] >= 0 and menu["x"] + menu["width"] <= width
    assert menu["y"] >= 0 and menu["y"] + menu["height"] <= 844
    assert page.locator("#sourceOptions button").count() == 7
    page.screenshot(path=str(screenshots / f"source-menu-{width}.png"))
    alternative.click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('ff-1.mp4')")
    assert button.inner_text().strip() == "非凡"
    page.get_by_label("返回", exact=True).click()
    assert page.locator(".recommend-card:visible").count() == 1


@pytest.mark.parametrize("playing", [False, True])
def test_source_switch_preserves_episode_position_speed_and_play_intent(source_catalog, playing):
    page, _ = source_catalog
    page.locator("#episodeGrid").get_by_label("第2集", exact=True).click()
    page.wait_for_function("document.getElementById('video').readyState >= 2")
    page.locator("#speedButton").click()
    page.locator("#video").evaluate("video => { video.currentTime = 2; }")
    page.wait_for_function("!document.getElementById('video').seeking")
    if playing:
        play_click(page)
        page.wait_for_function("!document.getElementById('video').paused")
    before = page.locator("#video").evaluate("video => video.currentTime")
    page.locator("#sourceButton").click()
    page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True).click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('ff-2.mp4') && document.getElementById('video').readyState >= 2 && !document.getElementById('video').seeking")
    assert page.locator("#meta").inner_text() == "第2集"
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    assert page.locator("#video").evaluate("video => video.paused") is not playing
    assert before - .15 <= page.locator("#video").evaluate("video => video.currentTime") < before + 2


def test_source_switch_does_not_substitute_another_episode_when_missing(source_catalog):
    page, state = source_catalog
    state["variants"][1]["episodes"] = state["variants"][1]["episodes"][:2]
    state["variants"][1]["episode_count"] = 2
    page.locator("#episodeGrid").get_by_label("第3集", exact=True).click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('bf-3.mp4')")
    page.locator("#sourceButton").click()
    alternative = page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True)
    alternative.wait_for(state="visible")
    # Either disable a missing episode or explain it on click; never silently play episode 2.
    if alternative.is_enabled():
        alternative.click()
    assert page.locator("#sourceButton").inner_text().strip() == "暴风"
    assert page.locator("#meta").inner_text() == "第3集"
    assert page.locator("#video").evaluate("video => video.currentSrc.includes('bf-3.mp4')")


def test_source_switch_lookup_failure_can_retry_without_losing_current_video(source_catalog):
    page, state = source_catalog
    state["fail"] = True
    page.locator("#sourceButton").click()
    page.locator("#sourceRetry").wait_for(state="visible")
    assert page.locator("#sourceStatus").inner_text()
    assert page.locator("#video").evaluate("video => video.currentSrc.includes('bf-1.mp4') && video.paused")
    state["fail"] = False
    page.locator("#sourceRetry").click()
    page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True).wait_for(state="visible")
    assert page.locator("#sourceRetry").is_hidden()
    assert len(state["requests"]) == 2


def test_source_switch_is_available_after_media_error(source_catalog, screenshots):
    page, _ = source_catalog
    page.route("**/unavailable.mp4", lambda route: route.abort("failed"))
    page.locator("#video").evaluate("video => { video.src = 'https://media.example/unavailable.m3u8'; video.load(); }")
    page.locator("#switchSourceOnError").wait_for(state="visible")
    assert "切换" in page.locator("#status").inner_text()
    assert page.locator("#videoHint").is_visible()
    assert "右上角" in page.locator("#videoHint").inner_text()
    assert "当前资源" not in page.locator("#videoHint").inner_text()
    assert not page.locator("#videoShell").evaluate("node => node.classList.contains('controls-hidden')")
    page.screenshot(path=str(screenshots / "source-error-390.png"))
    page.locator("#switchSourceOnError").click()
    page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True).click()
    page.wait_for_function("document.getElementById('video').currentSrc.includes('ff-1.mp4') && document.getElementById('video').readyState >= 2")
    assert page.locator("#switchSourceOnError").is_hidden()


def test_source_switch_late_response_does_not_reopen_player_after_return(source_catalog):
    page, state = source_catalog
    page.evaluate("""payload => {
      const originalFetch = window.fetch;
      window.fetch = (input, options) => String(input).includes('alternatives=1')
        ? new Promise(resolve => { window.releaseSourceResponse = () => resolve(new Response(JSON.stringify(payload), {status: 200, headers: {'Content-Type': 'application/json'}})); })
        : originalFetch(input, options);
    }""", {"v": 1, "kind": "search", "heading": "搜索结果", "items": state["variants"]})
    page.locator("#sourceButton").click()
    page.wait_for_function("typeof window.releaseSourceResponse === 'function'")
    page.get_by_label("返回", exact=True).click()
    page.evaluate("window.releaseSourceResponse()")
    page.evaluate("() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))")
    assert page.locator(".recommend-card:visible").count() == 1
    assert page.locator("#sourceMenu").is_hidden()
    assert page.locator("#video").get_attribute("src") is None


def test_source_switch_hls_manifest_waits_for_new_media_metadata_before_resuming(source_catalog):
    page, _ = source_catalog
    page.locator("#video").evaluate("video => { video.currentTime = 2; }")
    page.wait_for_function("!document.getElementById('video').seeking")
    page.evaluate("""() => {
      document.getElementById('video').canPlayType = () => '';
      window.Hls = class {
        static Events = {MANIFEST_PARSED: 'manifest', LEVEL_SWITCHED: 'level', ERROR: 'error'};
        static isSupported() { return true; }
        constructor() { this.handlers = {}; this.levels = []; window.testHls = this; }
        loadSource(url) { this.url = url; }
        attachMedia(video) { this.video = video; }
        on(name, callback) { this.handlers[name] = callback; }
        destroy() {}
      };
    }""")
    page.locator("#sourceButton").click()
    page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True).click()
    # A real HLS manifest may arrive while the previous video's duration is still valid.
    page.evaluate("window.testHls.handlers.manifest()")
    assert page.locator("#video").evaluate("video => video.readyState >= 2 && video.currentSrc.includes('bf-1.mp4')")
    page.evaluate("window.testHls.video.src = window.testHls.url")
    page.wait_for_function("document.getElementById('video').currentSrc.includes('ff-1.mp4') && document.getElementById('video').readyState >= 2 && !document.getElementById('video').seeking")
    assert page.locator("#video").evaluate("video => video.currentTime") == pytest.approx(2, abs=.1)
    assert page.locator("#video").evaluate("video => video.paused")


def test_source_switch_keyboard_escape_restores_button_focus(source_catalog):
    page, _ = source_catalog
    page.keyboard.press("Tab")
    page.locator("#sourceButton").focus()
    page.keyboard.press("Enter")
    page.locator("#sourceMenu").get_by_role("button", name="非凡", exact=True).wait_for(state="visible")
    page.keyboard.press("Escape")
    assert page.locator("#sourceMenu").is_hidden()
    assert page.locator("#sourceButton").get_attribute("aria-expanded") == "false"
    assert page.locator("#sourceButton").evaluate("node => node === document.activeElement")
