from __future__ import annotations

import asyncio
import importlib
import json
import re
from collections import Counter
from types import SimpleNamespace as NS

import pytest

from capability_catalog_fixture import source_catalog
from test_capability_discovery import ROOT, Event, command, context, discovery, engine, execution, plugin

search = importlib.import_module(discovery.CatalogQuery.__module__)
vocabulary = importlib.import_module(search.phrases_for.__module__)
VOCABULARY_PATH = ROOT / "runtime/jiang_capability_router/vocabulary.json"


@pytest.fixture(scope="module")
def shipped_catalog(patched_plugins):
    return source_catalog(patched_plugins, ROOT / "runtime/jiang_capability_router/capabilities.py")


def target_key(kind, row):
    if kind == "natural":
        return (kind, "", row["capability_id"].casefold())
    if kind == "plugin":
        return (kind, row["name"].casefold(), row["name"].casefold())
    name = row["handler_name"] if kind == "command" else row.get("name", row.get("id", "").rsplit(":", 1)[-1])
    return (kind, row["plugin"].casefold(), name.casefold())


def test_all_vocabulary_targets_exist_in_actual_shipped_sources(shipped_catalog, patched_plugins):
    available = {target_key(kind, row)
                 for kind, rows in (("plugin", "plugins"), ("command", "commands"), ("tool", "tools"),
                                    ("declaration", "declarations"), ("natural", "natural"))
                 for row in shipped_catalog[rows]}
    words = vocabulary.validate(json.loads(VOCABULARY_PATH.read_text(encoding="utf-8")))
    assert words.keys() <= available, words.keys() - available
    # Every shipped standard command is covered, even administrative and English commands.
    assert {target_key("command", row) for row in shipped_catalog["commands"]} <= words.keys()
    native_targets = {target_key("tool", row) for row in shipped_catalog["tools"]
                      if row["plugin"] != "jiang_capability_router"}
    assert native_targets <= words.keys(), native_targets - words.keys()
    assert (patched_plugins / "jiang_capability_router/vocabulary.json").read_bytes() == VOCABULARY_PATH.read_bytes()
    assert (patched_plugins / "jiang_capability_router/vocabulary.py").is_file()


def test_vocabulary_has_reviewed_words_and_phrases_of_different_lengths():
    words = {phrase for phrases in vocabulary.load().values() for phrase in phrases}
    counts = Counter(len(phrase) for phrase in words)
    assert len(words) >= 2000
    assert all(counts[size] >= 20 for size in (2, 3, 4))
    assert any(len(phrase) >= 10 for phrase in words)


def test_every_shipped_command_name_alias_and_fragment_is_retrievable(shipped_catalog, record_property):
    commands = shipped_catalog["commands"]
    owners = {row["name"]: search.search_text(row) for row in shipped_catalog["plugins"]}
    queries = 0
    for row in commands:
        name = row["effective_command"]
        cases = [name, f"请帮我 {name}，谢谢", *row["aliases"]]
        for size in (2, 3, 4):
            cases.append(re.sub(r"[\u3400-\u9fff]+", lambda m: " ".join(
                m[0][i:i + size] for i in range(0, len(m[0]), size)), name))
        for query in cases:
            ranked = search.CatalogQuery(query).rank(commands, search.search_text,
                                                    lambda item: owners.get(item["plugin"], ""))
            assert row in ranked, (row["plugin"], name, query)
            queries += 1
    record_property("commands", len(commands))
    record_property("queries", queries)


@pytest.mark.parametrize("text,required", [
    ("重生逆袭，先婚后爱", {"重生", "重生逆", "重生逆袭", "先婚后爱"}),
    ("请帮我搜一下先婚后爱，可以吗", {"先婚后爱", "先婚", "婚后爱"}),
    ("推特关注列表", {"关注", "关注列", "关注列表", "推特关注列表"}),
    ("请不要取消订阅", {"不要", "取消", "取消订阅"}),
])
def test_two_three_four_character_and_full_phrase_tokens(text, required):
    assert required <= search.tokens(text)
    assert "爱推" not in search.tokens("先婚后爱，推特")


# These held-out sentences are separate from the curated dictionary. Competing
# entries come from the real patched plugin sources, rather than single-item catalogs.
SCENARIOS = [
    ("commands", "群里当前到底关注了谁呀", "list_follows"),
    ("commands", "我订了哪些人来着，想看看名单", "list_follows"),
    ("commands", "这个推主麻烦再推最新一条过来", "test_tweet"),
    ("commands", "群里的推特以后别推了，先让我安静几天", "toggle_push"),
    ("commands", "让这个账号的推文改成卡片展示吧", "set_twitter_format"),
    ("commands", "把这些推主都加上以后一起看", "batch_follow_twitter"),
    ("commands", "这些博主都不关注了，批量取关吧", "batch_unfollow_twitter"),
    ("commands", "麻烦清空这个群的推主列表", "clear_all_subscriptions"),
    ("commands", "帮忙找一下完整歌词，我想跟着唱", "query_lyrics"),
    ("commands", "这首加入我的歌单以后再听", "collect_song"),
    ("commands", "这首歌不收藏了，请移出歌单", "uncollect_song"),
    ("commands", "麻烦列出我的歌曲收藏吧", "view_playlist"),
    ("commands", "从收藏里选一首播放，就要编号二", "play_from_playlist"),
    ("commands", "我想看看能用的音色再决定", "list_voices_command"),
    ("commands", "这群的声音太严肃了，调整一下群默认音色", "set_group_voice_command"),
    ("commands", "看看语音生成进度，已经等了一会儿了", "tasks_command"),
    ("commands", "取消正在生成的语音，我重新写一句", "cancel_task_command"),
    ("commands", "查询一下记忆库是否正常运行", "status"),
    ("commands", "聊天结束了，手动触发记忆总结吧", "summarize"),
    ("commands", "这条记错了，移除错误记忆记录", "forget"),
    ("commands", "管理员想重新建立向量索引", "rebuild_index"),
    ("commands", "本会话可用文件都列出来看看", "doc_list"),
    ("commands", "基于这个文件继续，先选第二个附件", "doc_use"),
    ("commands", "把文档当前使用哪张图告诉我", "img_active"),
    ("commands", "给这张图加备注，写成产品外观", "img_note"),
    ("commands", "看看生成过的文件还在不在", "list_files"),
    ("commands", "检查PDF转换依赖是否齐全", "pdf_status"),
    ("commands", "分享链接不自动处理了，把解析关闭", "close_parser"),
    ("commands", "本群启用链接解析，方便看视频", "open_parser"),
    ("commands", "今天没空爬楼，群聊内容回顾给我看看", "analyze_group_daily"),
    ("commands", "根据群聊画四格，做个趣味回顾", "generate_group_comic"),
    ("commands", "有哪些群报模板可以选呀", "view_templates"),
    ("commands", "我想调整群聊报告样式", "set_report_template"),
    ("commands", "这个群先停止学习，等会儿再说", "stop_learning_command"),
    ("commands", "学一下这种聊天方式，挺有意思的", "remember_command"),
    ("commands", "我在洛克最近打得怎么样", "rocom_battle_record"),
    ("commands", "请查看洛克精灵背包中的收藏", "rocom_package"),
    ("commands", "今天商人带了什么东西啊", "rocom_merchant"),
    ("commands", "商人来了提醒我，别错过了", "subscribe_merchant"),
    ("commands", "商人消息别推了，最近不玩了", "unsubscribe_merchant"),
    ("commands", "家园消息别推了，先退订家园", "unsubscribe_home"),
    ("commands", "帮忙看精灵配队，有什么阵容", "rocom_lineup"),
    ("commands", "麻烦查询蛋的资料，想知道是什么", "rocom_search_eggs"),
    ("commands", "这个技能有什么效果，查一下洛克的资料", "rocom_skill"),
    ("commands", "资源池里面太乱，清理当前会话图片", "img_clear"),
    ("commands", "以本地图库为准，把云端多余的删除", "overwrite_to_remote"),
    ("commands", "以云端图库为准，本地重新同步覆盖", "overwrite_from_remote"),
    ("commands", "想知道本地一共多少表情呢", "show_library_stats"),
    ("tools", "今天有点累，找首歌听听吧", "play_song_by_name"),
    ("tools", "来一首周杰伦的晴天让我听听", "play_song_by_name"),
    ("tools", "把这段文字读一遍，我不方便看屏幕", "mimo_tts_speak"),
    ("tools", "可以用语音回复我这句话吗", "mimo_tts_speak"),
    ("tools", "我以前告诉过你自己喜欢什么，还记得吗", "recall_long_term_memory"),
    ("tools", "从记忆里找一下我们说过的旅游安排", "recall_long_term_memory"),
    ("tools", "我不吃香菜，记住我的偏好吧", "memorize_long_term_memory"),
    ("tools", "这件事以后别忘了，我喜欢蓝色", "memorize_long_term_memory"),
    ("tools", "麻烦读一下上传的合同，总结主要内容", "read_file"),
    ("tools", "这个工作表有哪些数据，需要先看看", "read_workbook"),
    ("tools", "按部门汇总金额，给工作簿做个统计", "execute_excel_script"),
    ("tools", "将这些数据合并到一起，清理重复数据行", "execute_excel_script"),
    ("tools", "我想做份预算表，列出各项开销", "create_workbook"),
    ("tools", "再补几行数据，把遗漏的也填上", "write_rows"),
    ("tools", "完成表格并导出，我要xlsx文件", "export_workbook"),
    ("tools", "帮忙做一个项目汇报PPT，介绍下进展", "create_document"),
    ("tools", "给文档增加一节，说明售后政策", "add_blocks"),
    ("tools", "再做几页幻灯片展示我们的团队", "add_slides"),
    ("tools", "把文档草稿定稿，正文已确认完毕", "finalize_document"),
    ("tools", "把做好的PPT发来，我要下载它", "export_document"),
    ("tools", "把这份文档转为PDF便于打印", "convert_to_pdf"),
    ("tools", "把PDF变成Word后我自己编辑", "convert_from_pdf"),
    ("tools", "好开心，找个庆祝用的贴图吧", "search_memes"),
    ("tools", "看看这个插件怎么实现，读一下源码", "dev_read_file"),
    ("tools", "最近插件报了什么错，查查日志", "dev_check_logs"),
    ("declarations", "最近想看重生逆袭的短剧，推荐一部", "jiang_short_drama:search"),
    ("declarations", "有先婚后爱或者破镜重圆的小甜剧吗", "jiang_short_drama:search"),
    ("declarations", "晚饭时候想看个欢喜冤家的轻喜剧", "jiang_short_drama:search"),
    ("declarations", "找一下昨晚的NBA比赛录像", "jiang_short_drama:search"),
    ("declarations", "小短剧有没有闪婚后慢慢相爱的那种，轻松甜一点", "jiang_short_drama:search"),
    ("declarations", "找一份资料的磁力链接用于学习", "astrbot_plugin_BitTorrent:search"),
    ("declarations", "想看看这条磁力是什么资源", "astrbot_plugin_BitTorrent:preview"),
    ("declarations", "把背景换成海边，保留人物", "jiang_image:generate"),
    ("declarations", "给我生成一张风景壁纸吧", "jiang_image:generate"),
    ("declarations", "这次换一个画图服务模型试试", "jiang_image:model"),
    ("declarations", "我设置了什么提醒，列出来看看", "jiang_schedule:list"),
    ("declarations", "每天早上发天气，需要怎样设置", "jiang_schedule:manage"),
    ("declarations", "帮忙总结刚发的视频，讲了哪些事", "jiang_video_understanding:video"),
    ("declarations", "长时间不说话就聊聊，主动陪聊怎么配", "astrbot_plugin_proactive_chat:proactive"),
    ("natural", "成都现在外面多少度，冷不冷啊", "info.weather"),
    ("natural", "我在杭州，出门要不要带伞呢", "info.weather"),
    ("natural", "北京九五汽油多少钱，一升什么价", "info.oilprice"),
    ("natural", "今天有什么大事，看看热点", "info.news"),
    ("natural", "这周有哪些游戏不要钱，看看Epic", "info.epic"),
    ("natural", "这个UP主最近发了什么动态呢", "info.bili_dynamic"),
    ("natural", "来条疯四文案逗大家乐乐", "content.kfc"),
    ("natural", "最近有什么团可以报，看看活动安排", "info.raid_activity"),
    ("natural", "给我报上双十这个盛世活动", "action.raid_signup"),
    ("natural", "这次活动我去不了，把我的报名撤掉", "action.raid_cancel_signup"),
    ("tools", "今天群里有什么话题，我想查已生成的总结", "group_daily_analysis_get_report"),
    ("tools", "把昨天的群友金句找出来给我看看", "group_daily_analysis_get_report"),
    ("tools", "这次重新分析群里聊天，更新一份日报", "group_daily_analysis_trigger"),
    ("commands", "群里都追着哪几位推主更新呀", "list_follows"),
    ("commands", "刚才没收到，再把那位博主的最新推文发一遍", "test_tweet"),
    ("commands", "先停一停这群的推特通知吧", "toggle_push"),
    ("commands", "我以前存到歌单的歌曲还有哪些", "view_playlist"),
    ("commands", "上传过的那几份附件都列出来", "doc_list"),
    ("tools", "晴天这首周杰伦唱的歌能放给我听吗", "play_song_by_name"),
    ("tools", "我开车不方便看字，把下面这些话念一遍", "mimo_tts_speak"),
    ("tools", "上回跟你聊过的出游计划还能想起来吗", "recall_long_term_memory"),
    ("tools", "帮忙从这份合同里提炼一下主要条款", "read_file"),
    ("tools", "几张Excel的数据去个重，再按月份求和", "execute_excel_script"),
    ("declarations", "最近无聊，来个总裁和女主谈恋爱的小短剧", "jiang_short_drama:search"),
    ("declarations", "给我弄个穿越回古代种田的剧看看", "jiang_short_drama:search"),
    ("declarations", "把这张猫的照片改成手绘风格", "jiang_image:generate"),
    ("declarations", "目前这个群里都安排了什么自动任务", "jiang_schedule:list"),
    ("natural", "杭州这会儿温度大概几度呀", "info.weather"),
    ("natural", "四川的九二现在加一升要多少钱", "info.oilprice"),
    ("tools", "读一下截图中的报错，我看不懂", "angel_describe_image"),
]


@pytest.mark.parametrize("kind,query,expected", SCENARIOS, ids=[f"{i}-{row[0]}" for i, row in enumerate(SCENARIOS)])
def test_held_out_sentences_rank_real_entries_first(shipped_catalog, kind, query, expected):
    owners = {row["name"]: search.search_text(row) for row in shipped_catalog["plugins"]}
    ranked = search.CatalogQuery(query).rank(shipped_catalog[kind], search.search_text,
                                            lambda row: owners.get(row.get("plugin"), ""))
    keys = [row.get("handler_name", row.get("capability_id", row.get("name", row.get("id", "").rsplit(":", 1)[-1])))
            for row in ranked]
    if kind == "declarations":
        keys = [row["id"].removeprefix("decl:") for row in ranked]
    assert keys and keys[0] == expected, (query, keys[:5])


@pytest.mark.parametrize("query", ["帮我一下", "请帮我查一下量子隧穿", "谢谢你可以吗", "能不能帮我找一下",
                                   "阿贝尔规范对称性", "请问有没有希尔伯特空间", "handler_full_name"])
def test_unrelated_or_filler_queries_do_not_match_vocabulary(shipped_catalog, query):
    rows = [row for key in ("commands", "tools", "declarations", "natural") for row in shipped_catalog[key]]
    assert search.CatalogQuery(query).rank(rows, search.search_text) == []


def test_command_rename_keeps_alias_but_uses_current_execution_identity():
    entry = command(plugin="astrbot_plugin_twitter", handler_full_name="twitter.main.list_follows",
                    handler_name="list_follows", effective_command="本群推主")
    snapshot = {"plugins": [{"name": "astrbot_plugin_twitter"}], "commands": [entry], "declarations": []}
    rendered = discovery.render_catalog(snapshot, "", "关注了谁")
    assert "本群推主" in rendered and "command:twitter.main.list_follows" in rendered
    with pytest.raises(ValueError, match="改名"):
        execution.select_entry(snapshot, "command:twitter.main.list_follows", "推特列表")
    assert execution.select_entry(snapshot, "command:twitter.main.list_follows", "本群推主")


def test_same_named_handler_in_another_plugin_cannot_inherit_words():
    assert vocabulary.phrases_for(command(plugin="unrelated", handler_name="list_follows")) == ()
    assert vocabulary.phrases_for({"plugin": "unrelated", "name": "read_file"}) == ()
    assert vocabulary.phrases_for(NS(capability_id="info.weather"))


def test_vocabulary_never_recreates_uninstalled_disabled_or_admin_entries(engine):
    owner = "astrbot_plugin_twitter"
    active = plugin(owner)
    entries = [command(plugin=owner, handler_full_name="twitter.list_follows", handler_name="list_follows",
                       effective_command="推特列表", permission="admin"),
               command(plugin=owner, handler_full_name="twitter.test_tweet", handler_name="test_tweet",
                       effective_command="推特测试", enabled=False)]
    ctx, event = context([active]), Event()
    snapshot = asyncio.run(discovery.discover(ctx, event, entries))
    assert snapshot["commands"] == []
    text = discovery.render_catalog(snapshot, "", "关注了谁 最新卡片")
    assert "command:" not in text
    engine.add(owner)
    snapshot = asyncio.run(discovery.discover(ctx, event, entries))
    assert snapshot["plugins"] == []
    assert "command:" not in discovery.render_catalog(snapshot, "", "关注了谁")
    ctx.get_all_stars = lambda: []
    assert asyncio.run(discovery.discover(ctx, event, entries))["commands"] == []


def test_tool_vocabulary_requires_current_model_tool_and_owner():
    tool = {"plugin": "astrbot_plugin_livingmemory", "name": "memorize_long_term_memory", "description": ""}
    snapshot = {"plugins": [{"name": "astrbot_plugin_livingmemory"}], "commands": [], "declarations": []}
    assert tool["name"] in discovery.render_catalog(snapshot, "", "以后聊天记得这点", [tool])
    assert tool["name"] not in discovery.render_catalog(snapshot, "", "以后聊天记得这点", [])
    snapshot["plugins"] = []
    assert tool["name"] not in discovery.render_catalog(snapshot, "", "以后聊天记得这点", [tool])


@pytest.mark.parametrize("phrase", ["取关这个博主", "不要取消订阅", "怎么清空订阅", "以后别推了", "移出歌单"])
def test_search_words_cannot_confirm_a_write(phrase):
    assert not execution.authorized_message(phrase, "推特取关 someone", "write")
    assert execution.authorized_message("执行 推特取关 someone", "推特取关 someone", "write")


def test_more_synonyms_do_not_inflate_a_result(monkeypatch):
    rows = [{"name": "small"}, {"name": "large"}]
    monkeypatch.setattr(search, "phrases_for", lambda row: ("订阅列表",) * (1 if row["name"] == "small" else 100))
    assert search.CatalogQuery("我想看订阅列表").rank(rows, lambda row: "") == rows


@pytest.mark.parametrize("query", ["help", "full", "name", "查一下"])
def test_stop_words_still_find_a_live_command_or_alias_with_that_exact_name(query):
    rows = [command(effective_command="其他", description="help full name 查一下"),
            command(effective_command=query), command(effective_command="自定义", aliases=[query])]
    assert search.CatalogQuery(query).rank(rows, search.search_text) == rows[1:]


@pytest.mark.parametrize("payload", [None, [], {"schema_version": True, "entries": []},
                                   {"schema_version": 2, "entries": []}, {"schema_version": 1, "entries": [None]}])
def test_invalid_schema_is_rejected(payload):
    with pytest.raises(ValueError):
        vocabulary.validate(payload)


@pytest.mark.parametrize("changes", [{"kind": []}, {"kind": "execute"}, {"plugin": ""}, {"plugin": []},
                                   {"names": []}, {"phrases": [None]}, {"phrases": [" x"]},
                                   {"phrases": ["x" * 101]}, {"phrases": ["a\nb"]},
                                   {"phrases": ["word", "WORD"]}])
def test_invalid_entries_are_rejected(changes):
    row = dict(kind="command", plugin="example", names=["search"], phrases=["搜索资料"])
    row.update(changes)
    with pytest.raises(ValueError):
        vocabulary.validate({"schema_version": 1, "entries": [row]})


@pytest.mark.parametrize("content", [None, "{", "[]", '{"schema_version":1,"entries":[null]}', " " * (512 * 1024 + 1)],
                         ids=["missing", "json", "type", "entry", "oversize"])
def test_missing_or_bad_dictionary_falls_back_to_live_metadata(tmp_path, monkeypatch, content):
    if content is not None:
        (tmp_path / "vocabulary.json").write_text(content, encoding="utf-8")
    monkeypatch.setattr(vocabulary, "__file__", str(tmp_path / "vocabulary.py"))
    vocabulary.load.cache_clear()
    try:
        assert vocabulary.load() == {}
        row = command(effective_command="天气预报", description="查城市天气")
        assert search.CatalogQuery("成都天气").rank([row], search.search_text) == [row]
    finally:
        vocabulary.load.cache_clear()
