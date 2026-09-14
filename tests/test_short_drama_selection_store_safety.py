import asyncio
import importlib
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
PROJECT = Path("E:/Project")


def _load_patched_store(tmp_path: Path):
    source_root = PROJECT / "data/plugins/jiang_short_drama"
    required = ("search.py", "watch_url.py", "selection_store.py")
    if not all((source_root / name).is_file() for name in required):
        pytest.skip("short-drama upstream source is unavailable")

    package_name = f"safe_short_drama_store_{tmp_path.name.replace('-', '_')}"
    checkout = tmp_path / package_name
    checkout.mkdir()
    (checkout / "__init__.py").write_text("", encoding="utf-8")
    for name in required:
        shutil.copy2(source_root / name, checkout / name)
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    for patch in (
        ROOT / "patches/astrbot-plugin-jiang_short_drama--zzzz-network-safety.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--zzzzz-selection-store-safety.patch",
    ):
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)

    sys.path.insert(0, str(tmp_path))
    try:
        module = importlib.import_module(f"{package_name}.selection_store")
    finally:
        sys.path.remove(str(tmp_path))
    return module


def _store(module, *, api_url=None, player_url=None, api_token=None):
    return module.RemoteSelectionStore(
        api_url=api_url or "https://player.example/short-drama/api/selections",
        player_url=player_url or "https://player.example/short-drama/player",
        api_token=api_token or ("a" * 32),
    )


def test_selection_store_requires_same_public_hosted_service(tmp_path: Path) -> None:
    module = _load_patched_store(tmp_path)

    assert _store(module).enabled
    for overrides in (
        {"api_url": "http://player.example/short-drama/api/selections"},
        {"api_url": "https://127.0.0.1/short-drama/api/selections"},
        {"api_url": "https://upload.example/short-drama/api/selections"},
        {"api_url": "https://player.example/short-drama/api/other"},
        {"api_url": "https://player.example/short-drama/api/selections?next=1"},
        {"player_url": "https://player.example/short-drama/other"},
        {"player_url": "https://player.local/short-drama/player"},
        {"api_token": "too-short"},
        {"api_token": "a" * 513},
        {"api_token": ("a" * 24) + "\nheader"},
    ):
        assert not _store(module, **overrides).enabled, overrides


def test_selection_store_disables_redirects_and_bounds_network_data(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_patched_store(tmp_path)
    connector = object()
    sessions = []

    class FakeContent:
        def __init__(self, body: bytes) -> None:
            self.body = body
            self.limit = None
            self.position = 0

        async def read(self, limit: int) -> bytes:
            self.limit = limit
            chunk = self.body[self.position:self.position + min(limit, 7)]
            self.position += len(chunk)
            return chunk

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self.status = 200
            self.content = FakeContent(body)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            return False

    class FakeSession:
        response_body = b'{"token":"abcdefghijklmnop"}'

        def __init__(self, **kwargs) -> None:
            self.kwargs = kwargs
            self.post_args = None
            sessions.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, _type, _value, _traceback):
            return False

        def post(self, *args, **kwargs):
            self.post_args = (args, kwargs)
            return FakeResponse(self.response_body)

    monkeypatch.setattr(module, "_public_connector", lambda: connector)
    monkeypatch.setattr(module.aiohttp, "ClientSession", FakeSession)
    monkeypatch.setattr(
        module,
        "build_selection_payload",
        lambda *_args, **_kwargs: {
            "v": 1,
            "kind": "search",
            "heading": "测试",
            "items": [{"title": "短剧甲", "episodes": []}],
        },
    )

    result = asyncio.run(
        _store(module).create_url((), heading="测试", kind="search")
    )

    assert result == "https://player.example/short-drama/player?s=abcdefghijklmnop"
    session = sessions[-1]
    assert session.kwargs["trust_env"] is False
    assert session.kwargs["connector"] is connector
    args, kwargs = session.post_args
    assert args == ("https://player.example/short-drama/api/selections",)
    assert kwargs["allow_redirects"] is False
    assert isinstance(kwargs["data"], bytes)
    assert "json" not in kwargs

    FakeSession.response_body = b"x" * (module._MAX_SELECTION_RESPONSE_BYTES + 1)
    assert asyncio.run(
        _store(module).create_url((), heading="测试", kind="search")
    ) is None
    assert sessions[-1].post_args is not None
