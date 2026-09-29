from __future__ import annotations

import ast
import asyncio
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest

from pipeline_helpers import run_handler_pipeline


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def handler(patched_image, monkeypatch):
    source, _module = patched_image
    for name in ("astrbot", "astrbot.api", "astrbot.api.message_components"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))

    class At:
        def __init__(self, target):
            self.qq = target

    sys.modules["astrbot.api.message_components"].At = At

    async def quoted(event):
        return event.quote

    node = next(n for n in ast.walk(ast.parse(source))
                if isinstance(n, ast.AsyncFunctionDef) and n.name == "handle_image")
    node.decorator_list = []
    scope = {
        "AstrMessageEvent": object,
        "_get_plain_text": lambda event: event.text,
        "_get_quoted_image_path": quoted,
        "detect_image_edit_intent": lambda text: text if text.startswith("修图") else None,
        "detect_image_intent": lambda text: text if text.startswith("生图") else None,
        "_parse_aspect": lambda text: (text, "1:1"),
    }
    exec(compile(ast.Module(body=[node], type_ignores=[]), "image-handler", "exec"), scope)
    return scope["handle_image"], At


@pytest.mark.parametrize("group,target,sender,expected", [
    ("", None, "user", True),
    ("room", "bot", "user", True),
    ("room", None, "user", False),
    ("room", "someone-else", "user", False),
    ("room", "all", "user", False),
    ("", None, "bot", False),
])
@pytest.mark.parametrize("request_kind", ["generate", "edit", "quoted_generate"])
def test_private_images_do_not_require_at_and_group_images_keep_at_guard(
    handler, group, target, sender, expected, request_kind,
):
    method, At = handler
    segments = [At(target)] if target is not None else []
    timeline = []
    sent = []
    async def send(result):
        sent.append(result)
    event = SimpleNamespace(
        message_obj=SimpleNamespace(message=segments, group_id=group),
        text="修图 换背景" if request_kind == "edit" else "生图 一只小猫",
        quote="fixture.png" if request_kind != "generate" else None,
        get_sender_id=lambda: sender,
        get_self_id=lambda: "bot",
        get_group_id=lambda: group,
        get_messages=lambda: segments,
        stop_event=lambda: timeline.append("claimed"),
        send=send,
    )

    async def generate(*args):
        timeline.append("generate")
        yield "image"

    async def edit(*args):
        timeline.append("edit")
        yield "image"

    instance = SimpleNamespace(_do_image_gen=generate, _do_image_edit=edit)

    asyncio.run(method(instance, event))
    assert sent == (["image"] if expected else [])
    assert timeline == (["claimed", "generate" if request_kind == "generate" else "edit"] if expected else [])


@pytest.mark.parametrize("outcome", ["image", "failure"])
def test_stopped_image_handler_finishes_after_progress_in_real_pipeline(handler, outcome):
    method, _at = handler
    sequence = []
    event = SimpleNamespace(
        text="生图 一只小猫", quote=None, message_obj=SimpleNamespace(message=[]),
        get_sender_id=lambda: "user", get_self_id=lambda: "bot", get_group_id=lambda: "",
        stopped=False, _has_send_oper=False, is_at_or_wake_command=True, call_llm=False,
        get_extra=lambda key: ["image-handler"], get_result=lambda: None,
    )
    event.stop_event = lambda: setattr(event, "stopped", True)
    event.is_stopped = lambda: event.stopped
    event.set_result = lambda result: None

    async def send(message):
        event._has_send_oper = True
        sequence.append(message)

    event.send = send

    async def generate(*args):
        yield "正在画"
        sequence.append("generation-request")
        yield "final-image" if outcome == "image" else "generation-failed"

    plugin = SimpleNamespace(_do_image_gen=generate)

    run_handler_pipeline(method, plugin, event)
    assert sequence == ["正在画", "generation-request", "final-image" if outcome == "image" else "generation-failed"]
    assert event.stopped
