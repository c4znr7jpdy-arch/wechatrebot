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
    patch = ROOT / "patches/short-drama-player-hosted-only.patch"
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
