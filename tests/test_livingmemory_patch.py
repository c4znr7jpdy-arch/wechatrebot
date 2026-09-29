from __future__ import annotations

import asyncio
import ast
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).parents[1]
SOURCE = ROOT / "runtime/astrbot_plugin_livingmemory"
PATCH = ROOT / "patches" / "astrbot-plugin-astrbot_plugin_livingmemory.patch"


@pytest.fixture()
def patched_plugin(tmp_path: Path) -> Path:
    if not SOURCE.is_dir():
        pytest.skip("long-term-memory source is unavailable")
    target = tmp_path / "astrbot_plugin_livingmemory"
    shutil.copytree(SOURCE, target)
    # The host user profile can itself be a Git worktree. Make the copied plugin
    # its own repository so git apply never resolves paths against that parent.
    subprocess.run(
        ["git", "init", "--quiet"],
        cwd=target,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "apply", "--ignore-space-change", "--check", "-p1", str(PATCH)],
        cwd=target,
        check=True,
    )
    subprocess.run(
        ["git", "apply", "--ignore-space-change", "-p1", str(PATCH)],
        cwd=target,
        check=True,
    )
    return target


def _recall_type(source: str) -> type:
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "MemoryRecall"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            ast.Import(names=[ast.alias(name="re")]),
            class_node,
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, "memory_recall.py", "exec"), namespace)
    return namespace["MemoryRecall"]  # type: ignore[return-value]


def _plugin_ready_type(source: str) -> type:
    tree = ast.parse(source)
    class_node = next(
        node
        for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "LivingMemoryPlugin"
    )
    method = next(
        node
        for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_ensure_plugin_ready"
    )
    module = ast.Module(
        body=[
            ast.ImportFrom(
                module="__future__",
                names=[ast.alias(name="annotations")],
                level=0,
            ),
            ast.ClassDef(
                name="LivingMemoryPlugin",
                bases=[],
                keywords=[],
                body=[method],
                decorator_list=[],
            ),
        ],
        type_ignores=[],
    )
    ast.fix_missing_locations(module)
    namespace: dict[str, object] = {}
    exec(compile(module, "main.py", "exec"), namespace)
    return namespace["LivingMemoryPlugin"]  # type: ignore[return-value]


class _Config:
    def __init__(self, values: dict[str, object] | None = None):
        self.values = values or {}

    def get(self, key: str, default: object = None) -> object:
        return self.values.get(key, default)


def _router(recall_type: type, values: dict[str, object] | None = None):
    recall = recall_type.__new__(recall_type)
    recall.config_manager = _Config(values)
    return recall


def test_patch_forces_explicit_recall_and_skips_repeated_greetings(
    patched_plugin: Path,
) -> None:
    path = patched_plugin / "core" / "event_handler_modules" / "memory_recall.py"
    source = path.read_text(encoding="utf-8")
    recall_type = _recall_type(source)
    recall = _router(recall_type)

    assert recall._should_auto_recall("上次说的什么来着") == (
        True,
        "explicit_memory_request",
    )
    assert recall._should_auto_recall("你还记得吗？") == (
        True,
        "explicit_memory_request",
    )
    assert recall._should_auto_recall("[At:wxid_masked] @小妹 你好你好") == (
        False,
        "short_casual",
    )
    assert recall._should_auto_recall("喂喂喂") == (False, "short_casual")
    assert recall._should_auto_recall("我们周末去哪里比较好") == (
        True,
        "potential_memory_context",
    )


def test_patch_uses_builtin_intent_and_respects_master_off(
    patched_plugin: Path,
) -> None:
    source = (
        patched_plugin / "core" / "event_handler_modules" / "memory_recall.py"
    ).read_text(encoding="utf-8")
    recall_type = _recall_type(source)
    disabled = _router(
        recall_type,
        {"recall_engine.auto_recall_mode": "off"},
    )

    assert _router(recall_type)._should_auto_recall("上次说了什么") == (
        True,
        "explicit_memory_request",
    )
    assert disabled._should_auto_recall("还记得吗") == (
        False,
        "auto_recall_off",
    )
    manual = _router(
        recall_type,
        {"recall_engine.auto_recall_mode": "manual"},
    )
    assert manual._should_auto_recall("我们周末去哪里比较好") == (
        False,
        "manual_no_explicit_request",
    )
    assert manual._should_auto_recall("上次说了什么") == (
        True,
        "explicit_memory_request",
    )


def test_patch_adds_customer_config_and_bounded_observability(
    patched_plugin: Path,
) -> None:
    schema = json.loads(
        (patched_plugin / "_conf_schema.json").read_text(encoding="utf-8")
    )
    source = (
        patched_plugin / "core" / "event_handler_modules" / "memory_recall.py"
    ).read_text(encoding="utf-8")

    assert "explicit_recall_phrases" not in schema["recall_engine"]["items"]
    assert 'event.set_extra("livingmemory_recall_state", state)' in source
    assert 'event.set_extra("livingmemory_recall_reason", reason)' in source


def test_runtime_hooks_never_wait_for_missing_embedding_provider(
    patched_plugin: Path,
) -> None:
    source = (patched_plugin / "main.py").read_text(encoding="utf-8")
    hooks = source[
        source.index("@filter.on_llm_request()") : source.index(
            '@filter.command_group("lmem")'
        )
    ]

    assert hooks.count("wait_for_initialization=False") == 3
    assert "ready, _ = await self._ensure_plugin_ready()" not in hooks

    plugin_type = _plugin_ready_type(source)

    class MissingProviderInitializer:
        is_initialized = False

        def __init__(self) -> None:
            self.wait_calls = 0

        async def ensure_initialized(self) -> bool:
            self.wait_calls += 1
            await asyncio.sleep(30)
            return False

    initializer = MissingProviderInitializer()
    plugin = plugin_type()
    plugin.initializer = initializer
    plugin._get_initialization_status_message = lambda: "not ready"

    ready, message = asyncio.run(
        plugin._ensure_plugin_ready(wait_for_initialization=False)
    )

    assert ready is False
    assert message == "not ready"
    assert initializer.wait_calls == 0
