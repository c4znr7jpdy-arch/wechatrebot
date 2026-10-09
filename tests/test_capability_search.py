from __future__ import annotations

import json

import pytest

from test_capability_discovery import ROOT, command, discovery, load_module

search = load_module("search")


def catalog():
    return {
        "plugins": [
            {"name": "twitter", "description": "推特订阅、定时推送、链接识别"},
            {"name": "drama", "description": "短剧、电影、电视剧搜索"},
        ],
        "commands": [
            command(plugin="twitter", effective_command="推特列表", handler_full_name="twitter.list",
                    description="查看当前会话订阅的推主", aliases=["twitter_list"]),
            command(plugin="twitter", effective_command="推特测试", handler_full_name="twitter.latest",
                    description="立即获取并推送指定推主的最新一条推文。"),
            command(plugin="drama", effective_command="影视帮助", handler_full_name="drama.help",
                    description="影视用法"),
        ],
        "declarations": [{"id": "decl:drama:search", "plugin": "drama",
                          "description": "搜索短剧、电视剧或电影并返回卡片",
                          "examples": ["短剧推荐", "电影 流浪地球"], "effect": "read"}],
    }


@pytest.mark.parametrize("query", [
    "推特 订阅 卡片", "把我在这个群订阅的推特，推我一次最新的卡片",
    "@姜小妹 帮我看看推特订阅，发张卡片", "订阅，推特。卡片！", "ＴＷＩＴＴＥＲ＿ＬＩＳＴ",
])
def test_real_twitter_phrases_find_existing_commands(query):
    result = discovery.render_catalog(catalog(), "", query)
    assert "command:twitter.list" in result
    assert "command:twitter.latest" in result
    assert "没有匹配结果" not in result


@pytest.mark.parametrize("query", ["帮我找部短剧，霸道总裁的", "短剧 推荐 卡片", "电视剧，想看狂飙"])
def test_drama_phrases_find_declared_execution_entry(query):
    result = discovery.render_catalog(catalog(), "", query)
    assert "decl:drama:search" in result
    assert "command:drama.help" in result


def test_declaration_match_also_lists_its_owner_without_keyword_in_plugin_description():
    snapshot = catalog()
    snapshot["plugins"][1]["description"] = "分类影视资源"
    result = discovery.render_catalog(snapshot, "", "帮我找部短剧")
    assert "decl:drama:search" in result
    assert '"name": "drama"' in result


def test_exact_command_precedes_other_commands_from_its_plugin():
    result = discovery.render_catalog(catalog(), "", "推特测试")
    assert result.index("command:twitter.latest") < result.index("command:twitter.list")


def test_aliases_and_live_plugin_metadata_are_searchable():
    snapshot = catalog()
    snapshot["plugins"][0]["description"] = "观星望远镜控制"
    result = discovery.render_catalog(snapshot, "", "帮我调一下望远镜")
    assert "command:twitter.list" in result
    assert "command:drama.help" not in result


@pytest.mark.parametrize("query", ["帮我一下", "帮我查一下量子隧穿", "handler_full_name", "read"])
def test_no_match_explains_search_and_only_shows_authorized_summaries(query):
    result = discovery.render_catalog(catalog(), "", query)
    assert "本次关键词没有匹配结果，不代表机器人没有此功能" in result
    assert "推特订阅" in result
    assert "command:twitter" not in result
    assert "decl:drama" not in result


def test_empty_authorized_catalog_is_distinguished_from_no_match():
    result = discovery.render_catalog({"plugins": [], "commands": [], "declarations": []}, "", "推特")
    assert "当前会话没有已启用且获授权" in result
    assert "空 query 再查" not in result
    assert "command:twitter" not in result


def test_full_catalog_and_native_tool_search_preserve_permission_filter():
    snapshot = catalog()
    tools = [{"name": "play_music", "description": "点歌 音乐播放", "plugin": "twitter"},
             {"name": "hidden_music", "description": "音乐", "plugin": "disabled"}]
    result = discovery.render_catalog(snapshot, "", "想听音乐，给我点歌", tools)
    assert "play_music" in result and "hidden_music" not in result
    full = discovery.render_catalog(snapshot, "", "", tools)
    for entry in snapshot["commands"]:
        assert "command:" + entry["handler_full_name"] in full
    assert "hidden_music" not in full


def test_tokenization_handles_two_three_characters_and_latin_boundaries():
    assert {"电视剧", "电视", "视剧", "短剧"} <= search.tokens("电视剧和短剧")
    assert search.match_score("cat", "catalog") == 0
    assert search.match_score("weather", "weather forecast") > 0
    assert search.match_score("查成都天气", "天气预报") > 0
    assert search.match_score("量子，隧穿", "量子隧穿") > 0
    assert search.match_score("天，气", "天气") == 0


def test_every_shipped_declaration_is_found_by_its_real_examples():
    checked = 0
    for path in (ROOT / "runtime/capability_declarations").glob("*.json"):
        declarations = json.loads(path.read_text(encoding="utf-8"))["capabilities"]
        for item in declarations:
            entry = dict(item, id=f"decl:{path.stem}:{item['id']}", plugin=path.stem)
            snapshot = {"plugins": [{"name": path.stem}], "commands": [], "declarations": [entry]}
            for example in item.get("examples", []):
                result = discovery.render_catalog(snapshot, "", f"请帮我 {example}，可以吗")
                assert entry["id"] in result, (path.name, example)
                checked += 1
    assert checked >= 10
