from pathlib import Path

from wechatrebot_manager.response_trace import ResponseTraceService


def test_reply_trace_is_white_labelled_and_never_returns_message_content(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "astrbot-core.log").write_text(
        "\n".join(
            [
                "[16:11:48.724] [Core] [INFO] [roles.front_desk:847]: AngelHeart[tag:GroupMessage:51632940287@chatroom]: 命中快路径 route=direct_wake",
                "[16:11:49.143] [Core] [INFO] [event_handler_modules.memory_recall:154]: [tag:GroupMessage:51632940287@chatroom] 智能召回决策: skip reason=short_casual",
                "[16:11:49.140] [Core] [INFO] [hooks.llm_hook_handler:305]: [LLM Hook] compact 情绪上下文已注入，persona=wxid_secret",
                "[16:12:03.545] [Core] [INFO] [respond.stage:206]: Prepare to send - 张三/wxid_secret: [引用消息] 你好呀",
            ]
        ),
        encoding="utf-8",
    )

    result = ResponseTraceService(tmp_path).recent()

    assert len(result) == 1
    serialized = str(result)
    assert "你好呀" not in serialized
    assert "wxid_secret" not in serialized
    assert "51632940287" not in serialized
    assert "AstrBot" not in serialized
    assert [stage["label"] for stage in result[0]["stages"]] == [
        "收到消息",
        "点名快路径",
        "长期记忆",
        "精简情感",
        "回复模型",
        "文字发送",
    ]


def test_reply_trace_reports_memory_timeout_as_non_blocking_degradation(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "astrbot-core.log").write_text(
        "\n".join(
            [
                "[17:09:21.227] [INFO] [memory_recall] 开始记忆召回",
                "[17:09:22.039] [WARN] [hybrid_retriever] 向量检索超过 0.8 秒，本轮将使用其他检索通道降级",
                "[17:09:23.889] [INFO] [respond.stage] Prepare to send - hidden message",
            ]
        ),
        encoding="utf-8",
    )

    result = ResponseTraceService(tmp_path).recent()

    memory = next(item for item in result[0]["stages"] if item["code"] == "memory")
    assert memory["code"] == "memory"
    assert memory["label"] == "长期记忆降级"
    assert memory["status"] == "skipped"
    assert memory["detail"] == "检索超时，已继续回复"
    assert memory["duration_text"] == "0.8 秒"


def test_reply_trace_includes_send_completion_and_stage_durations(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "astrbot-core.log").write_text(
        "\n".join(
            [
                "[17:10:00.000] [INFO] [core.event_bus] received hidden text",
                "[17:10:00.300] [INFO] [roles.front_desk] 命中快路径 route=direct_wake",
                "[17:10:00.500] [INFO] [memory_recall] 智能召回决策: skip reason=short_casual",
                "[17:10:01.100] [INFO] [respond.stage] Prepare to send - hidden reply",
                "[17:10:01.400] [INFO] [angel_heart_context] AI回复完成",
            ]
        ),
        encoding="utf-8",
    )

    trace = ResponseTraceService(tmp_path).recent()[0]

    decision = next(item for item in trace["stages"] if item["code"] == "decision")
    model = next(item for item in trace["stages"] if item["code"] == "model")
    send = next(item for item in trace["stages"] if item["code"] == "send")
    assert decision["duration_text"] == "0.3 秒"
    assert decision["offset_seconds"] == 0.0
    assert model["offset_seconds"] == 0.5
    assert send["status"] == "success"
    assert send["duration_text"] == "0.3 秒"
    assert send["offset_seconds"] == 1.1
