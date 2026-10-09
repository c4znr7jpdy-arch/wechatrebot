import ast
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from pipeline_helpers import PipelineResult, run_handler_pipeline
from test_hot_config_runtime import _runtime_function
from test_plugin_portability import _patched


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "vendor/data/plugins"


@pytest.fixture
def bt_source(tmp_path):
    return _patched(tmp_path, "astrbot_plugin_bittorrent", "main.py", source_root=SOURCE)


def _run(source, method_name, message, results=None, preview=None, enable_preview=False):
    scope = {}
    utils = next(node for node in ast.parse(source).body
                 if isinstance(node, ast.ClassDef) and node.name == "MagnetUtils")
    utils.body = [node for node in utils.body if isinstance(node, ast.Assign)
                  or getattr(node, "name", None) in {"is_sort_keyword", "get_sort_param"}]
    exec(compile(ast.Module(body=[utils], type_ignores=[]), "bt-sort", "exec"), scope)
    comp = NS(Plain=lambda text: NS(text=text), Node=lambda **kw: kw,
              Nodes=lambda **kw: NS(**kw), Image=NS(fromURL=lambda url: NS(url=url)))
    method = _runtime_function(source, method_name, {
        "MagnetUtils": scope["MagnetUtils"], "Comp": comp, "_format_size": str,
    })
    sent = []
    event = NS(message_str=message, stopped=False, result=None,
               _has_send_oper=False, is_at_or_wake_command=True, call_llm=False,
               get_sender_name=lambda: "祈", get_sender_id=lambda: "user",
               get_extra=lambda key: [method_name], chain_result=PipelineResult)
    event.stop_event = lambda: setattr(event, "stopped", True)
    event.is_stopped = lambda: event.stopped
    event.get_result = lambda: event.result
    event.set_result = lambda result: setattr(event, "result", result)

    async def send(result):
        sent.append(result.chain)
        event._has_send_oper = True

    event.send = send
    plugin = NS(search_service=NS(search=AsyncMock(return_value=results)),
                whatslink_service=NS(get_preview=AsyncMock(return_value=preview)),
                enable_preview=enable_preview,
                _format_result=_runtime_function(source, "_format_result", {}))
    run_handler_pipeline(method, plugin, event)
    assert event.stopped
    return sent, plugin


@pytest.mark.parametrize("message,results,expected", [
    ("/bt", [], "用法"),
    ("/bt 测试", [], "未找到"),
    ("/bt 测试", [{"error": "连接超时"}], "连接超时"),
    ("/bt 测试", [{"title": "测试", "magnet_link": None, "size": "1 GB", "create_time": "today"}], "共找到"),
])
def test_search_delivers_results_usage_and_failures(bt_source, message, results, expected):
    sent, plugin = _run(bt_source, "magnet_search_handler", message, results)
    assert len(sent) == 1
    assert expected in "".join(item.text for item in sent[0])
    if message == "/bt":
        plugin.search_service.search.assert_not_awaited()
    else:
        plugin.search_service.search.assert_awaited_once_with("测试", "")


def test_search_delivers_both_result_and_preview(bt_source):
    sent, plugin = _run(bt_source, "magnet_search_handler", "\\bt\u3000热门 测试 电影",
                        [{"title": "测试", "magnet_link": "magnet:test", "size": "1 GB", "create_time": "today"}],
                        {"name": "测试", "screenshots": [{"screenshot": "https://example.test/image.png"}]}, True)
    plugin.search_service.search.assert_awaited_once_with("测试 电影", "hot")
    assert len(sent) == 2
    assert sent[1][0].nodes[0]["content"][1].url == "https://example.test/image.png"


def test_disabled_preview_skips_service_and_all_preview_messages(bt_source):
    sent, plugin = _run(
        bt_source, "magnet_search_handler", "/bt 测试",
        [{"title": "测试", "magnet_link": "magnet:test", "size": "1 GB", "create_time": "today"}],
        {"name": "测试", "screenshots": [{"screenshot": "https://example.test/image.png"}]},
        False,
    )
    plugin.whatslink_service.get_preview.assert_not_awaited()
    assert len(sent) == 1
    text = "".join(item.text for item in sent[0])
    assert "magnet:test" in text and "预览" not in text


@pytest.mark.parametrize("preview,expected", [
    (None, "结果1无预览图"),
    ({"name": "测试", "screenshots": []}, "结果1无预览图"),
    ({"name": "测试", "screenshots": [{}]}, "结果1无预览图"),
    ({"name": "测试", "screenshots": [{"screenshot": "https://example.test/image.png"}]}, "结果1预览图"),
])
def test_preview_bridge_output_has_no_sender_prefix(bt_source, preview, expected):
    sent, plugin = _run(
        bt_source, "magnet_search_handler", "/bt 测试",
        [{"title": "测试", "magnet_link": "magnet:test", "size": "1 GB", "create_time": "today"}],
        preview, True,
    )
    plugin.whatslink_service.get_preview.assert_awaited_once_with("magnet:test")
    bridge = ROOT / "vendor/hook_41827_astrbot_bridge/src/hook_bridge/onebot_client.py"
    expand = _runtime_function(bridge.read_text("utf-8"), "_expand_forward_nodes", {
        "Mapping": Mapping, "UnsupportedSegmentError": ValueError,
    })
    nodes = [{**node, "content": [
        {"type": "text", "data": {"text": item.text}} if hasattr(item, "text")
        else {"type": "image", "data": {"file": item.url}}
        for item in node["content"]
    ]} for node in sent[1][0].nodes]
    expanded = expand(nodes)
    text = "".join(item["data"]["text"] for item in expanded[0] if item["type"] == "text")
    assert text == expected
    if expected == "结果1预览图":
        assert expanded[0][1] == {"type": "image", "data": {"file": "https://example.test/image.png"}}


@pytest.mark.parametrize("message,preview,expected", [
    ("/btp", None, "用法"),
    ("/btp invalid", None, "有效的磁力链接"),
    ("/btp magnet:test", None, "未查询到"),
    ("/btp magnet:test", {"name": "测试文件", "size": 100}, "测试文件"),
])
def test_preview_delivers_results_usage_and_failures(bt_source, message, preview, expected):
    sent, _ = _run(bt_source, "magnet_preview_handler", message, preview=preview)
    assert len(sent) == 1
    assert expected in "".join(item.text for item in sent[0])


def test_original_stopped_generator_loses_followup_preview():
    source = (SOURCE / "astrbot_plugin_bittorrent/main.py").read_text("utf-8")
    sent, _ = _run(source, "magnet_search_handler", "/bt 测试",
                   [{"title": "测试", "magnet_link": "magnet:test", "size": "1 GB", "create_time": "today"}],
                   {"name": "测试"}, True)
    assert len(sent) == 1
    assert not any(hasattr(item, "nodes") for chain in sent for item in chain)
