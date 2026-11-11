"""Execute the delivered music command with isolated search/send boundaries."""
import ast
import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from test_plugin_portability import _patched


@pytest.fixture(scope="module")
def command(tmp_path_factory):
    source = _patched(tmp_path_factory.mktemp("music-arguments"), "astrbot_plugin_music", "main.py", all_patches=True)
    methods = [node for node in ast.walk(ast.parse(source)) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
               and node.name in {"_parse_music_command", "on_search_song"}]
    for method in methods:
        method.decorator_list = []
    namespace = {
        "re": re, "AstrMessageEvent": object,
        "QishuiMusic": SimpleNamespace(looks_like_qishui_source=lambda value: value.startswith("https://qishui.test/")),
        "logger": SimpleNamespace(debug=lambda *_args: None),
    }
    exec(compile(ast.Module(body=methods, type_ignores=[]), "delivered-music-command", "exec"), namespace)
    return namespace


def run_command(command, text, *, songs=(), source="netease"):
    player = SimpleNamespace(platform=SimpleNamespace(name=source, display_name="测试音乐源"),
                             fetch_songs=AsyncMock(return_value=list(songs)))
    sender = SimpleNamespace(send_song=AsyncMock())
    plugin = SimpleNamespace(
        keywords=["网易点歌"], cfg=SimpleNamespace(real_song_limit=5, select_mode="list"),
        get_player=lambda **_options: player, _pick_playable_single=AsyncMock(side_effect=lambda _player, values: values),
        sender=sender,
    )
    plugin._parse_music_command = lambda value: command["_parse_music_command"](plugin, value)
    event = SimpleNamespace(message_str=text, plain_result=lambda value: value, stop_event=lambda: None)

    async def collect():
        return [result async for result in command["on_search_song"](plugin, event)]

    return asyncio.run(collect()), player, sender, event


@pytest.mark.parametrize("text,expected", [
    ("点歌 2000", "2000"),
    ("点歌 0", "0"),
    ("点歌 000", "000"),
    ("点歌 １９９９", "１９９９"),
    ("点歌 歌名0", "歌名0"),
    ("点歌 年份2026", "年份2026"),
    ("点歌 Love Story", "Love Story"),
    ("网易点歌\u2005Love Story0", "Love Story0"),
    ("点歌 歌名 ²", "歌名 ²"),
    ("点歌 歌名 ⑵", "歌名 ⑵"),
    ("点歌 歌名 02", "歌名"),
    ("点歌 Love Story\u2005２", "Love Story"),
])
def test_music_search_preserves_title_and_only_splits_a_separate_index(command, text, expected):
    results, player, sender, event = run_command(command, text)
    assert player.fetch_songs.await_args.kwargs["keyword"] == expected
    assert results == [f"搜索【{expected}】无结果"]
    assert not sender.send_song.await_count and event.is_at_or_wake_command is True


@pytest.mark.parametrize("index", ["2", "02", "２", "٢"])
def test_explicit_song_index_keeps_existing_selection_semantics(command, index):
    songs = [SimpleNamespace(name=f"song-{number}") for number in range(3)]
    results, player, sender, _event = run_command(command, f"点歌 周杰伦 {index}", songs=songs)
    assert player.fetch_songs.await_args.kwargs["keyword"] == "周杰伦"
    assert not results
    assert sender.send_song.await_args.args[2] is songs[1]


def test_oversized_numeric_suffix_does_not_crash_the_music_handler(command):
    results, player, _sender, _event = run_command(command, "点歌 歌名 " + "9" * 5000)
    assert player.fetch_songs.await_count == 1
    assert results and results[0].endswith("无结果")


def test_qishui_share_source_keeps_full_original_argument(command):
    argument = "https://qishui.test/song/2000 02"
    results, player, _sender, _event = run_command(command, "点歌 " + argument, source="qishui")
    assert player.fetch_songs.await_args.kwargs["keyword"] == argument
    assert results == [f"搜索【{argument}】无结果"]
