import asyncio
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).parents[1]
PROJECT = Path(__file__).parents[1] / "vendor"


def _load_patched_search(tmp_path: Path):
    source = PROJECT / "data/plugins/jiang_short_drama/search.py"
    if not source.is_file():
        pytest.fail("short-drama upstream source is unavailable")
    checkout = tmp_path / "short-drama-network"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(source, checkout / "search.py")
    patch = ROOT / "patches/astrbot-plugin-jiang_short_drama--zzzz-network-safety.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    patched_source = (checkout / "search.py").read_text(encoding="utf-8")
    compile(patched_source, str(checkout / "search.py"), "exec")

    module_name = f"short_drama_safe_search_{tmp_path.name}"
    spec = importlib.util.spec_from_file_location(module_name, checkout / "search.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, patched_source


def test_network_patch_rejects_unsafe_urls_and_filters_episodes(tmp_path: Path) -> None:
    module, patched = _load_patched_search(tmp_path)
    valid = "https://cdn.example/video/episode.m3u8"

    assert module._safe_public_https_url(valid)
    assert module._safe_public_https_url("https://8.8.8.8/video.m3u8")
    for value in (
        "http://cdn.example/video.m3u8",
        "https://user@cdn.example/video.m3u8",
        "https://localhost/video.m3u8",
        "https://media.local/video.m3u8",
        "https://127.0.0.1/video.m3u8",
        "https://10.0.0.8/video.m3u8",
        "https://169.254.169.254/latest/meta-data/",
        "https://[::1]/video.m3u8",
        "https://[fe80::1]/video.m3u8",
        "https://[2606:4700:4700::1111]/video.m3u8",
        "https://cdn.example:99999/video.m3u8",
        "https://[::1/video.m3u8",
        "https://2130706433/video.m3u8",
        "https://0177.0.0.1/video.m3u8",
        "https://224.0.0.1/video.m3u8",
    ):
        assert not module._safe_public_https_url(value), value

    episodes = module.parse_episodes(
        "不安全$http://cdn.example/one.m3u8#"
        "内网$https://127.0.0.1/two.m3u8#"
        f"可用${valid}"
    )
    assert [(item.name, item.url) for item in episodes] == [("可用", valid)]
    assert patched.count("connector=_public_connector()") == 6
    assert patched.count("allow_redirects=False") == 6
    assert "allow_redirects=True" not in patched
    assert "ssl=False" not in patched
    assert patched.count("raw = await _read_bounded_body(response)") == 5


def test_public_resolver_rejects_private_and_mixed_dns_answers(tmp_path: Path) -> None:
    module, _ = _load_patched_search(tmp_path)

    class FakeResolver:
        def __init__(self, answers):
            self.answers = answers
            self.closed = False

        async def resolve(self, _hostname, _port, _family):
            return self.answers

        async def close(self):
            self.closed = True

    public_answers = [
        {"host": "8.8.8.8"},
        {"host": "2606:4700:4700::1111"},
    ]
    resolver = module._PublicResolver(FakeResolver(public_answers))
    assert asyncio.run(resolver.resolve("cdn.example")) == public_answers

    for answers in (
        [{"host": "127.0.0.1"}],
        [{"host": "169.254.169.254"}],
        [{"host": "::1"}],
        [{"host": "fe80::1%3"}],
        [{"host": "ff02::1"}],
        [{"host": "224.0.0.1"}],
        [{"host": "8.8.8.8"}, {"host": "192.168.1.5"}],
        [],
    ):
        with pytest.raises(OSError):
            asyncio.run(
                module._PublicResolver(FakeResolver(answers)).resolve("cdn.example")
            )


def test_bounded_body_reads_all_chunks_and_stops_at_limit(tmp_path: Path) -> None:
    module, _ = _load_patched_search(tmp_path)

    class ChunkedContent:
        def __init__(self, body, chunk_size=3):
            self.body = body
            self.position = 0
            self.chunk_size = chunk_size

        async def read(self, size):
            chunk = self.body[self.position:self.position + min(size, self.chunk_size)]
            self.position += len(chunk)
            return chunk

    from types import SimpleNamespace

    body = b'{"items":[1,2,3]}'
    response = SimpleNamespace(content=ChunkedContent(body))
    assert asyncio.run(module._read_bounded_body(response, len(body))) == body

    response = SimpleNamespace(content=ChunkedContent(b"x" * 100))
    assert asyncio.run(module._read_bounded_body(response, 10)) == b"x" * 11
    assert response.content.position == 11
