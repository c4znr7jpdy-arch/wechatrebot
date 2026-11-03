"""GitHub Release publisher: naming, gh wrapper, verification, promotion, rollback."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _module():
    script = Path(__file__).parents[1] / "scripts" / "publish_github_release.py"
    spec = importlib.util.spec_from_file_location("publish_github_release", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPO = "c4znr7jpdy-arch/wechatrebot"


def test_release_tag_and_asset_urls_follow_github_download_layout() -> None:
    module = _module()
    assert module.release_tag("0.9.9") == "v0.9.9"
    assert module.release_tag("0.10.0.post1") == "v0.10.0.post1"
    assert module.asset_prefix(REPO, "v0.9.9") == f"https://github.com/{REPO}/releases/download/v0.9.9"
    assert module.asset_url(REPO, "v0.9.9", "payload.wrb") == (
        f"https://github.com/{REPO}/releases/download/v0.9.9/payload.wrb"
    )
    assert module.stable_manifest_url(REPO) == f"https://github.com/{REPO}/releases/download/stable/stable.json"
    with pytest.raises(ValueError):
        module.release_tag("not-a-version")
    with pytest.raises(ValueError):
        module.asset_prefix("owner only", "v1")


import hashlib
import io
import urllib.error


class _Response(io.BytesIO):
    def __init__(self, body: bytes, *, final_url: str, status: int = 200, length: int | None = None) -> None:
        super().__init__(body)
        self.status = status
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def test_public_asset_check_follows_redirect_and_compares_size_and_sha256(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    final = "https://objects.githubusercontent.com/github-production-release-asset/1?X-Amz-Signature=abc"
    seen: list[str] = []

    def urlopen(request, *, timeout):
        seen.append(request.get_method())
        return _Response(body if request.get_method() == "GET" else b"", final_url=final, length=len(body))

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    assert module.verify_public_asset(
        url, expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(), timeout=20,
    ) == final
    assert seen == ["HEAD", "GET"]


def test_public_asset_check_rejects_http_redirect_wrong_size_and_bad_digest(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    digest = hashlib.sha256(body).hexdigest()

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url="http://insecure.example/asset.zip"))
    with pytest.raises(ValueError, match="HTTPS"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256=digest, timeout=20, attempts=1)

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url=url, length=len(body) + 1))
    with pytest.raises(ValueError, match="大小"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256=digest, timeout=20, attempts=1)

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url=url))
    with pytest.raises(ValueError, match="SHA-256"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256="0" * 64, timeout=20, attempts=1)


def test_public_asset_check_retries_transient_404_then_succeeds(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    calls = {"n": 0}

    def urlopen(request, *, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Response(body if request.get_method() == "GET" else b"", final_url=url, length=len(body))

    slept: list[float] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    module.verify_public_asset(url, expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(),
                               timeout=20, attempts=3, delay=5.0, sleep=slept.append)
    assert slept == [5.0]
