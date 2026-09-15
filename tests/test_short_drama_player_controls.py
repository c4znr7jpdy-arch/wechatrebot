"""Native-control regression checks using a locally generated video, no network."""
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
def player_page(browser, player_assets):
    html, media = player_assets
    page = browser.new_page(viewport={"width": 390, "height": 844})
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


def native_play_click(page):
    box = page.locator("#video").bounding_box()
    page.mouse.move(box["x"] + 24, box["y"] + box["height"] - 48)
    page.mouse.click(box["x"] + 24, box["y"] + box["height"] - 48)


def test_native_control_clicks_toggle_once_and_seeking_keeps_pause(player_page):
    page = player_page
    assert page.evaluate("window.customVideoClicks") == 0
    native_play_click(page)
    page.wait_for_function("!document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    native_play_click(page)
    page.wait_for_function("document.getElementById('video').paused")
    page.locator("#video").evaluate("video => { video.currentTime = 3; }")
    page.wait_for_function("document.getElementById('video').currentTime > 2")
    assert page.locator("#video").evaluate("video => video.paused")


def test_episode_switch_and_auto_next_preserve_playback_speed(player_page):
    page = player_page
    page.get_by_label("播放速度").select_option("1.5")
    native_play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    page.get_by_role("button", name="下一集", exact=True).click()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && !document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5
    page.locator("#video").evaluate("video => { video.currentTime = video.duration - .2; }")
    page.wait_for_function("document.getElementById('meta').textContent === '第3集' && !document.getElementById('video').paused && document.getElementById('video').currentTime > .1")
    assert page.locator("#video").evaluate("video => video.playbackRate") == 1.5


def test_disabled_auto_next_stays_on_current_episode(player_page):
    page = player_page
    page.locator(".switch-label").click()
    assert not page.get_by_label("自动连播").is_checked()
    native_play_click(page)
    page.wait_for_function("!document.getElementById('video').paused")
    page.locator("#video").evaluate("video => { video.currentTime = video.duration - .2; }")
    page.wait_for_function("document.getElementById('video').ended")
    assert page.locator("#meta").inner_text() == "第1集"


@pytest.mark.parametrize("width", [320, 390, 1280])
def test_toolbar_and_episode_sheet_do_not_overlap_native_controls(player_page, width):
    page = player_page
    page.set_viewport_size({"width": width, "height": 844})
    video = page.locator("#video").bounding_box()
    toolbar = page.get_by_role("group", name="剧集和播放设置").bounding_box()
    assert toolbar["y"] >= video["y"] + video["height"]
    assert page.locator("#videoShell button").count() == 0
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.get_by_role("button", name="选择剧集", exact=True).click()
    assert page.locator("#episodeSheet").is_visible()
    page.locator("#sheetEpisodeGrid").get_by_role("button", name="第2集", exact=True).click()
    assert page.locator("#episodeSheet").is_hidden()
    page.wait_for_function("document.getElementById('meta').textContent === '第2集' && document.getElementById('video').readyState >= 2")
    assert page.locator("#video").evaluate("video => video.paused")
