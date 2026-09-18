from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
import sys


ROOT = Path(__file__).parents[1]


def _publisher():
    script = ROOT / "scripts" / "publish_manager_update.py"
    spec = importlib.util.spec_from_file_location("publish_manager_update_guard", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_update_popup_describes_release_and_offers_later_action() -> None:
    script = (
        ROOT / "src" / "wechatrebot_manager" / "app_webui" / "app.js"
    ).read_text(encoding="utf-8")

    assert 'cancelText: "稍后提醒"' in script
    assert 'intent: "primary"' in script
    assert "formatBytes(release.package_size)" in script
    assert "release.notes.join" in script
    assert "scheduleAutomaticUpdateCheck(1800)" in script
    assert "configured : 600" in script


def test_download_page_version_participates_in_release_floor(monkeypatch) -> None:
    publisher = _publisher()
    response = io.BytesIO(json.dumps({"version": "0.9.2"}).encode())
    monkeypatch.setattr(publisher.urllib.request, "urlopen", lambda *_args, **_kwargs: response)

    assert publisher._fetch_download_version("https://updates.example.test") == "0.9.2"
    assert publisher._published_version_floor("0.9.0", "0.9.2") == "0.9.2"


def test_download_page_version_retries_transient_network_failure(monkeypatch) -> None:
    publisher = _publisher()
    response = io.BytesIO(json.dumps({"version": "0.9.3"}).encode())
    attempts = iter((OSError("temporary"), response))

    def open_url(*_args, **_kwargs):
        result = next(attempts)
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(publisher.urllib.request, "urlopen", open_url)
    monkeypatch.setattr(publisher.time, "sleep", lambda _seconds: None)

    assert publisher._fetch_download_version("https://updates.example.test") == "0.9.3"
