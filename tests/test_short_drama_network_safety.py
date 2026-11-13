import asyncio
import importlib.util
from pathlib import Path
import shutil
import subprocess
import sys
from types import ModuleType

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
    shutil.copy2(ROOT / "runtime/jiang_short_drama/title_search.py", checkout / "title_search.py")
    for name in (
        "astrbot-plugin-jiang_short_drama--zzzz-network-safety.patch",
        "astrbot-plugin-jiang_short_drama--zzzzzzzz-catalog-sources.patch",
        "astrbot-plugin-jiang_short_drama--zzzzzzzzz-title-search.patch",
    ):
        patch = ROOT / "patches" / name
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    patched_source = (checkout / "search.py").read_text(encoding="utf-8")
    compile(patched_source, str(checkout / "search.py"), "exec")

    package_name = f"short_drama_safe_search_{tmp_path.name}"
    package = ModuleType(package_name)
    package.__path__ = [str(checkout)]
    sys.modules[package_name] = package
    module_name = f"{package_name}.search"
    spec = importlib.util.spec_from_file_location(module_name, checkout / "search.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module, patched_source


@pytest.mark.asyncio
async def test_xinlang_catalog_gate_is_shared_by_search_people_collection_and_recommendations(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module, _ = _load_patched_search(tmp_path)
    searcher = module.ShortDramaSearcher()
    source = next(value for value in module.DEFAULT_SOURCES if value.name == "新浪资源")
    clock, starts = [100.0], []
    semaphore = asyncio.Semaphore(4)

    class Response:
        status = 404

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

    class Session:
        def get(self, *_args, **_kwargs):
            starts.append(clock[0])
            return Response()

    session = Session()
    raced = False

    async def sleep(delay):
        nonlocal raced
        clock[0] += delay
        if not raced:
            raced = True
            clock[0] += 0.1
            await searcher._search_source_people(session, semaphore, source, "演员")

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await searcher._search_source_variants(session, semaphore, source, "第一部")
    # A late wakeup loses to a concurrent caller and expires without another GET.
    assert await searcher._search_source_variants(session, semaphore, source, "第二部") == []
    assert starts == pytest.approx([100.0, 101.2])
    clock[0] = 103.0
    await searcher._recommendation_source_page(session, semaphore, source, 1, None)
    await searcher._collection_source_page(session, semaphore, source, "NBA", "2026", 1)
    assert starts == pytest.approx([100.0, 101.2, 103.0, 104.1])
    assert all(b - a >= 1.0 for a, b in zip(starts, starts[1:]))


@pytest.mark.asyncio
async def test_cancelled_xinlang_wait_never_starts_request_or_reserves_future_slot(tmp_path, monkeypatch):
    from types import SimpleNamespace
    module, _ = _load_patched_search(tmp_path)
    searcher = module.ShortDramaSearcher()
    source = next(value for value in module.DEFAULT_SOURCES if value.name == "新浪资源")
    clock, sleeping = [100.0], asyncio.Event()

    async def sleep(_delay):
        sleeping.set()
        await asyncio.Event().wait()

    class Session:
        def get(self, *_args, **_kwargs):
            pytest.fail("cancelled catalog wait started a GET")

    monkeypatch.setattr(module, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module.asyncio, "sleep", sleep)
    await searcher._wait_catalog_request(source)
    task = asyncio.create_task(searcher._search_source_variants(Session(), asyncio.Semaphore(4), source, "片名"))
    await sleeping.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    clock[0] = 102.0
    await searcher._wait_catalog_request(source)
    assert searcher._xinlang_next_request == 103.1


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
