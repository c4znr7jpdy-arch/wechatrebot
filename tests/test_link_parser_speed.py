from __future__ import annotations

import ast
import asyncio
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from test_plugin_portability import _function, _patched


def _douyin_source(tmp_path: Path) -> str:
    return _patched(
        tmp_path,
        "astrbot_plugin_parser",
        "core/parsers/douyin/__init__.py",
        all_patches=True,
    )


@pytest.mark.parametrize(
    ("statuses", "expected_ratio"),
    [([206], "720p"), ([403, 206], "1080p")],
)
def test_douyin_probe_stops_at_first_usable_quality(
    tmp_path: Path, statuses: list[int], expected_ratio: str
) -> None:
    source = _douyin_source(tmp_path)
    parser_class = next(
        node for node in ast.parse(source).body
        if isinstance(node, ast.ClassDef) and node.name == "DouyinParser"
    )
    ratios = next(
        ast.literal_eval(node.value) for node in parser_class.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "PLAY_RATIOS"
    )

    class Response:
        def __init__(self, status: int, url: str):
            self.status = status
            self.url = url
            self.headers = {"Content-Range": "bytes 0-1/2500000"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return None

    calls: list[str] = []

    def get(url: str, **_):
        calls.append(url)
        return Response(statuses[len(calls) - 1], url)

    probe = _function(
        source,
        "probe_video_url",
        {
            "ProbedVideo": lambda url, size, headers: SimpleNamespace(
                url=url, size=size, headers=headers
            ),
            "ParseException": RuntimeError,
            "ClientError": OSError,
            "TimeoutError": TimeoutError,
            "logger": Mock(),
        },
    )
    parser = SimpleNamespace(
        PLAY_RATIOS=ratios,
        session=SimpleNamespace(get=get),
        _build_play_url=lambda video_id, ratio: f"https://example.test/{video_id}/{ratio}",
        _build_media_headers=lambda referer: {"Referer": referer},
        _extract_response_size=lambda headers: 2500000,
    )

    result = asyncio.run(probe(parser, "video-id", "https://example.test/share"))

    assert result.url.endswith("/" + expected_ratio)
    assert len(calls) == len(statuses)
    assert result.size == 2500000


def test_douyin_download_is_shared_across_conversations(tmp_path: Path) -> None:
    source = _douyin_source(tmp_path)
    download_once = _function(
        source,
        "_download_video_once",
        {"Task": asyncio.Task, "Path": Path},
    )
    video_path = tmp_path / "video.mp4"
    calls: list[str] = []

    async def download():
        await asyncio.sleep(0)
        video_path.write_bytes(b"video")
        return video_path

    def start(url: str, **_):
        calls.append(url)
        return asyncio.create_task(download())

    async def scenario():
        parser = SimpleNamespace(
            _video_downloads=OrderedDict(),
            downloader=SimpleNamespace(download_video=start),
            proxy=None,
        )
        first = download_once(parser, "same-video", "https://cdn.test/first", {})
        second = download_once(parser, "same-video", "https://cdn.test/second", {})
        assert second is first
        await first
        assert download_once(parser, "same-video", "https://cdn.test/third", {}) is first
        assert len(calls) == 1

        video_path.unlink()
        replacement = download_once(parser, "same-video", "https://cdn.test/fourth", {})
        assert replacement is not first
        await replacement
        assert len(calls) == 2

    asyncio.run(scenario())
