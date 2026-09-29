"""Exercise plugin delivery through the pinned AstrBot processing pipeline."""
import ast
import asyncio
from collections.abc import AsyncGenerator
import inspect
import logging
from pathlib import Path
from types import SimpleNamespace
import typing


class PipelineResult:
    def __init__(self, chain):
        self.chain = chain


def run_handler_pipeline(method, plugin, event):
    pipeline = Path(__file__).parent / "fixtures/image_pipeline"
    namespace = {
        "AsyncGenerator": AsyncGenerator, "AstrMessageEvent": object,
        "MessageEventResult": PipelineResult, "CommandResult": PipelineResult,
        "ProviderRequest": type("ProviderRequest", (), {}),
        "inspect": inspect, "T": typing, "logger": logging.getLogger("test-pipeline"),
    }
    nodes = []
    for file, name in (("context_utils.py", "call_handler"),
                       ("scheduler.py", "_process_stages"), ("stage.py", "process")):
        nodes.append(next(n for n in ast.walk(ast.parse((pipeline / file).read_text("utf-8")))
                          if isinstance(n, ast.AsyncFunctionDef) and n.name == name))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), "pinned-pipeline", "exec"), namespace)

    class PluginStage:
        async def process(self, event):
            async for value in namespace["call_handler"](event, lambda e: method(plugin, e)):
                yield value

    class LlmStage:
        async def process(self, event):
            raise AssertionError("Exclusive command unexpectedly fell through to the LLM")
            yield

    class RespondStage:
        async def process(self, event):
            if event.get_result() is not None:
                await event.send(event.get_result())

    process_stage = SimpleNamespace(
        star_request_sub_stage=PluginStage(), agent_sub_stage=LlmStage(),
        ctx=SimpleNamespace(astrbot_config={"provider_settings": {"enable": True}}),
    )
    process_stage.process = lambda e: namespace["process"](process_stage, e)
    scheduler = SimpleNamespace(stages=[process_stage, RespondStage()])
    scheduler._process_stages = lambda e, index=0: namespace["_process_stages"](scheduler, e, index)
    asyncio.run(scheduler._process_stages(event))
