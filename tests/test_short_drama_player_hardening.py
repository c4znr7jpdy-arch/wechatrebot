import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
PROJECT = Path("E:/Project")


def _patched_player(tmp_path: Path) -> Path:
    source = PROJECT / "deploy/short_drama_player"
    if not (source / "index.html").is_file() or not (source / "state_server.py").is_file():
        pytest.skip("hosted short-drama player source is unavailable")
    checkout = tmp_path / "short-drama-player"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(source / "index.html", checkout / "index.html")
    shutil.copy2(source / "state_server.py", checkout / "state_server.py")
    for name in ("short-drama-player-hosted-only.patch", "short-drama-player-controls.patch", "short-drama-player-cinema.patch"):
        patch = ROOT / "patches" / name
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    return checkout


def _load_state_server(checkout: Path, module_name: str):
    path = checkout / "state_server.py"
    source = path.read_text(encoding="utf-8")
    compile(source, str(path), "exec")
    spec = importlib.util.spec_from_file_location(module_name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_hosted_player_removes_open_url_and_third_party_fallback(tmp_path: Path) -> None:
    checkout = _patched_player(tmp_path)
    html = (checkout / "index.html").read_text(encoding="utf-8")

    assert "params.get('url')" not in html
    assert "m3u8-player.cc" not in html
    assert "备用播放器" not in html
    assert 'id="direct" hidden aria-hidden="true"' in html
    assert "validateSelectionPayload(payload)" in html
    assert "isSafePublicHttpsUrl(source)" in html

    scripts = [
        match.group(1)
        for match in re.finditer(
            r"<script(?:\s[^>]*)?>([\s\S]*?)</script>", html, re.IGNORECASE
        )
        if match.group(1).strip()
    ]
    assert len(scripts) == 1
    if shutil.which("node") is None:
        pytest.skip("Node.js is unavailable")
    subprocess.run(
        ["node", "--check"],
        input=scripts[0],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )

    validator = re.search(
        r"const isSafePublicHttpsUrl = \(value\) => \{[\s\S]*?\n      \};",
        html,
    )
    assert validator is not None
    cases = [
        "https://cdn.example/video.m3u8",
        "http://cdn.example/video.m3u8",
        "https://user@cdn.example/video.m3u8",
        "https://localhost/video.m3u8",
        "https://127.0.0.1/video.m3u8",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/video.m3u8",
        "https://2130706433/video.m3u8",
    ]
    script = (
        validator.group(0)
        + "\nconst values = JSON.parse(process.argv[1]);"
        + "process.stdout.write(JSON.stringify(values.map(isSafePublicHttpsUrl)));"
    )
    result = subprocess.run(
        ["node", "-e", script, json.dumps(cases)],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    assert json.loads(result.stdout) == [True, False, False, False, False, False, False, False]


def test_state_service_rejects_unsafe_media_payloads(tmp_path: Path) -> None:
    checkout = _patched_player(tmp_path)
    module = _load_state_server(checkout, f"safe_state_server_{tmp_path.name}")

    assert module._valid_public_https_url("https://cdn.example/video.m3u8")
    for value in (
        "http://cdn.example/video.m3u8",
        "https://user@cdn.example/video.m3u8",
        "https://localhost/video.m3u8",
        "https://10.0.0.8/video.m3u8",
        "https://[::1]/video.m3u8",
        "https://[2606:4700:4700::1111]/video.m3u8",
        "https://cdn.example:99999/video.m3u8",
    ):
        assert not module._valid_public_https_url(value), value

    payload = {
        "v": 1,
        "kind": "recommendations",
        "heading": "最新短剧",
        "items": [{
            "title": "短剧甲",
            "cover": "http://unsafe.example/cover.jpg",
            "episodes": [{
                "name": "第1集",
                "url": "https://cdn.example/episode-1.m3u8",
            }],
        }],
    }
    validated, error = module._validate_payload(payload)
    assert error is None
    assert validated["items"][0]["cover"] == ""

    payload["items"][0]["episodes"][0]["url"] = "https://127.0.0.1/private.m3u8"
    validated, error = module._validate_payload(payload)
    assert validated is None
    assert error == "invalid episode"


def test_recommendations_are_public_validated_cached_and_reopenable(tmp_path, monkeypatch):
    checkout = _patched_player(tmp_path)
    module = _load_state_server(checkout, f"catalog_{tmp_path.name}")
    store = module.StateStore(tmp_path / "state", 600, 100)
    # Private user selections must never become a public recommendation pool.
    store.put({"v": 1, "kind": "search", "heading": "private", "items": [{"title": "private title"}]})
    calls = []

    def catalog_json(base, **params):
        calls.append(params)
        if params["ac"] == "list":
            return {"class": [{"type_id": 3, "type_name": "短剧"}]}
        return {"list": [
            {"vod_name": f"公开短剧{index}", "type_name": "短剧", "vod_pic": "https://cdn.example/poster.jpg",
             "vod_play_url": "第1集$https://cdn.example/one.m3u8#第2集$https://cdn.example/two.m3u8"}
            for index in range(9)
        ] + [
            {"vod_name": "unsafe", "type_name": "短剧", "vod_play_url": "第1集$https://127.0.0.1/private.m3u8"},
            {"vod_name": "movie", "type_name": "电影", "vod_play_url": "完整版$https://cdn.example/movie.m3u8"},
        ]}

    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    catalog = module.RecommendationCatalog(store)
    items = catalog.get()
    assert len(items) == 9
    assert len(calls) == 2
    assert catalog.get() == items and len(calls) == 2
    for item in items:
        assert item["title"] not in {"private title", "unsafe", "movie"}
        saved = store.get(item["selection_token"])
        assert saved["items"][0]["episodes"] == item["episodes"]
    catalog.refresh_at = 0
    monkeypatch.setattr(catalog, "_load", lambda: [])
    assert catalog.get() == items
    catalog.refresh_at = catalog.expires_at = 0
    assert catalog.get() == []


def test_recommendation_http_endpoint_and_busy_cache(tmp_path, monkeypatch):
    import threading
    from urllib.request import urlopen
    from urllib.error import HTTPError
    checkout = _patched_player(tmp_path)
    module = _load_state_server(checkout, f"http_catalog_{tmp_path.name}")
    store = module.StateStore(tmp_path / "state", 600, 100)
    catalog = module.RecommendationCatalog(store)
    catalog.lock.acquire()
    try:
        assert catalog.get() == []
    finally:
        catalog.lock.release()
    monkeypatch.setattr(catalog, "_load", lambda: [])
    server = module.ThreadingHTTPServer(("127.0.0.1", 0), module.Handler)
    server.recommendation_catalog = catalog
    server.selection_resolver = module.SelectionResolver()
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{server.server_port}/recommendations", timeout=2)
        assert error.value.code == 503
        monkeypatch.setattr(catalog, "get", lambda: [{"title": "公开短剧"}])
        with urlopen(f"http://127.0.0.1:{server.server_port}/recommendations", timeout=2) as response:
            assert response.headers["Cache-Control"] == "no-store"
            assert json.load(response)["items"][0]["title"] == "公开短剧"
        from urllib.parse import urlencode
        query = urlencode({"title": "原剧", "source": "量子资源", "episode": "0" * 64})
        monkeypatch.setattr(server.selection_resolver, "get", lambda *_args: {"title": "原剧", "episodes": []})
        with urlopen(f"http://127.0.0.1:{server.server_port}/resolve?{query}", timeout=2) as response:
            assert json.load(response)["items"][0]["title"] == "原剧"
        with pytest.raises(HTTPError) as error:
            urlopen(f"http://127.0.0.1:{server.server_port}/resolve?title=missing", timeout=2)
        assert error.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)


def test_recommendation_proxy_patch_applies_to_hosted_nginx(tmp_path):
    source = PROJECT / "deploy/nginx/short_drama_player.conf"
    if not source.is_file():
        pytest.skip("hosted nginx source unavailable")
    # Preserve the Linux host's LF configuration contract on Windows as well.
    (tmp_path / source.name).write_text(source.read_text("utf-8"), encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
    patch = ROOT / "patches/short-drama-player-cinema-nginx.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=tmp_path, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=tmp_path, check=True)
    config = (tmp_path / source.name).read_text("utf-8")
    assert "location = /short-drama/api/recommendations" in config
    assert "proxy_pass http://127.0.0.1:6198/recommendations;" in config
    assert "limit_except GET { deny all; }" in config
    assert "worker-src 'self' blob:;" in config
    assert "location = /short-drama/api/resolve" in config
    assert "proxy_pass http://127.0.0.1:6198/resolve;" in config


def test_compact_metadata_resolver_uses_original_fixed_source_and_fingerprint(tmp_path, monkeypatch):
    import hashlib
    checkout = _patched_player(tmp_path)
    module = _load_state_server(checkout, f"resolve_{tmp_path.name}")
    calls = []
    first = "https://cdn.example/one.m3u8"
    fingerprint = hashlib.sha256(first.encode()).hexdigest()
    def catalog_json(base, **params):
        calls.append((base, params))
        return {"list": [
            {"vod_name": "原剧", "type_name": "电视剧", "vod_play_url": "第1集$https://other.example/wrong.m3u8"},
            {"vod_name": "原剧", "type_name": "电视剧", "vod_pic": "http://cdn.example/cover.jpg",
             "vod_play_url": f"第1集${first}$$$第1集${first}#第2集$https://cdn.example/two.m3u8"},
        ]}
    monkeypatch.setattr(module, "_catalog_json", catalog_json)
    resolver = module.SelectionResolver()
    item = resolver.get("原剧", "量子资源", fingerprint)
    assert len(item["episodes"]) == 2
    assert item["cover"] == "https://cdn.example/cover.jpg"
    assert calls == [("https://cj.lziapi.com/api.php/provide/vod/", {"ac": "detail", "wd": "原剧", "pg": 1})]
    assert resolver.get("原剧", "量子资源", fingerprint) == item and len(calls) == 1
    with pytest.raises(ValueError):
        resolver.get("原剧", "https://127.0.0.1/private", fingerprint)
    with pytest.raises(ValueError):
        resolver.get("原剧", "量子资源", first)
    assert resolver.get("原剧", "量子资源", "0" * 64) is None
