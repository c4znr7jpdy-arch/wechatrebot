"""Run delivered memory formatting with stable identities and malformed old data."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
from types import ModuleType
from unittest.mock import Mock

import pytest

from test_hot_config_runtime import _runtime_function
from test_livingmemory_patch import patched_plugin


@pytest.fixture
def formatting(patched_plugin: Path, monkeypatch):
    prefix = "_livingmemory_participant_test"
    for name in (prefix, f"{prefix}.core", f"{prefix}.core.base",
                 f"{prefix}.core.utils", f"{prefix}.core.prompts", "astrbot"):
        package = ModuleType(name)
        package.__path__ = []
        monkeypatch.setitem(sys.modules, name, package)
    api = ModuleType("astrbot.api")
    api.logger = Mock()
    monkeypatch.setitem(sys.modules, "astrbot.api", api)
    prompt_manager = ModuleType(f"{prefix}.core.prompts.prompt_manager")
    prompt_manager.get_prompt_manager = lambda: None
    monkeypatch.setitem(sys.modules, prompt_manager.__name__, prompt_manager)

    modules = {}
    for relative in ("core/base/constants.py", "core/utils/formatting.py"):
        name = prefix + "." + relative.removesuffix(".py").replace("/", ".")
        spec = importlib.util.spec_from_file_location(name, patched_plugin / relative)
        module = importlib.util.module_from_spec(spec)
        monkeypatch.setitem(sys.modules, name, module)
        spec.loader.exec_module(module)
        modules[relative] = module
    return modules["core/utils/formatting.py"]


def _identity(sender="1001234", name="小明", **extra):
    return {
        "identity_key": f"aiocqhttp:{sender}", "sender_id": sender,
        "platform": "aiocqhttp", "display_name": name,
        "aliases": [name], "is_bot": False, **extra,
    }


def _render(formatting, identities, *, participants=None, **metadata):
    return formatting.format_memories_for_injection([{
        "content": "检索原文", "score": 0.8,
        "metadata": {
            "participants": participants if participants is not None else ["模型昵称"],
            "participant_identities": identities, **metadata,
        },
    }])


def _labels(text):
    line = next(line for line in text.splitlines() if line.startswith("Participants:"))
    return line.removeprefix("Participants: ").split(" | ", 1)[0].split("、")


def test_same_nickname_gets_distinct_labels_without_raw_accounts(formatting):
    text = _render(formatting, [_identity(), _identity("2001234")])
    labels = _labels(text)
    assert len(labels) == 2 and labels[0] != labels[1]
    assert all(re.fullmatch(r"小明#[0-9a-f]{8}", label) for label in labels)
    assert "1001234" not in text and "2001234" not in text
    assert "模型昵称" not in text


def test_renamed_user_keeps_suffix_and_uses_saved_alias(formatting, patched_plugin):
    source = (patched_plugin / "core/memory_scope.py").read_text("utf-8")
    namespace = {"Any": object}
    _runtime_function(source, "parse_identity_aliases", namespace)
    alias = _runtime_function(source, "resolve_sender_alias", namespace)
    saved_name = alias("aiocqhttp:1001234=统一昵称", "aiocqhttp", "1001234", "平台昵称")
    before = _labels(_render(formatting, [_identity(name="旧昵称")]))[0]
    after = _labels(_render(formatting, [_identity(name=saved_name)]))[0]
    assert before.startswith("旧昵称#") and after.startswith("统一昵称#")
    assert before.split("#")[-1] == after.split("#")[-1]


def test_bot_identity_keeps_first_person_without_account(formatting):
    text = _render(formatting, [_identity("wxid_bot_9876", "机器人", is_bot=True)])
    assert _labels(text) == ["我"]
    assert "wxid_bot_9876" not in text


@pytest.mark.parametrize("name", [None, "", "1001234", " aiocqhttp:1001234 ",
                                  "用户1001234", ["1001234"], {"id": "1001234"}, 1001234])
def test_missing_or_account_shaped_name_uses_generic_label(formatting, name):
    text = _render(formatting, [_identity(name=name)])
    assert re.fullmatch(r"用户#[0-9a-f]{8}", _labels(text)[0])
    assert "1001234" not in text


@pytest.mark.parametrize("sender", ["7", "42", "1234"])
def test_short_account_is_not_rendered_as_name_or_suffix(formatting, sender):
    label = _labels(_render(formatting, [_identity(sender, sender)]))[0]
    assert re.fullmatch(r"用户#[0-9a-f]{8}", label)
    assert label != "用户#" + sender


@pytest.mark.parametrize("identities", [None, [], {}, "bad", [None, [], "bad", {}],
                                        [{"display_name": "无稳定身份"}],
                                        [{"sender_id": True, "identity_key": [], "display_name": {}}]])
def test_legacy_names_survive_missing_or_invalid_identity_records(formatting, identities):
    text = _render(formatting, identities, participants=["张三", "李四"])
    assert _labels(text) == ["张三", "李四"]


def test_identity_key_alone_is_stable_and_masks_account_name(formatting):
    identity = _identity("wxid_missing_4567", "wxid_missing_4567")
    del identity["sender_id"]
    text = _render(formatting, [identity])
    assert re.fullmatch(r"用户#[0-9a-f]{8}", _labels(text)[0])
    assert "wxid_missing_4567" not in text


def test_numeric_legacy_sender_uses_same_stable_fallback_key(formatting):
    first = _identity("1001234", "昵称")
    second = _identity(1001234, "昵称")
    del second["identity_key"]
    second["platform"] = " AIOCqHTTP "
    assert _labels(_render(formatting, [first])) == _labels(_render(formatting, [second]))


def test_account_name_case_variant_is_also_masked(formatting):
    identity = _identity("wxid_Sensitive_Account", "WXID_SENSITIVE_ACCOUNT")
    text = _render(formatting, [identity])
    assert re.fullmatch(r"用户#[0-9a-f]{8}", _labels(text)[0])
    assert "wxid_sensitive_account" not in text.casefold()


def test_malformed_fields_are_not_stringified_or_treated_as_bot(formatting):
    malformed = _identity("wxid_real_1234", {"secret": "must-not-render"}, is_bot="false")
    text = _render(formatting, [None, malformed, ["must-not-render"]])
    assert re.fullmatch(r"用户#[0-9a-f]{8}", _labels(text)[0])
    assert "must-not-render" not in text and "wxid_real_1234" not in text


def test_identity_labels_preserve_persona_summary_channel(formatting):
    memories = [{
        "content": "检索原文 | 事实原文", "score": 0.8,
        "metadata": {"participant_identities": [_identity()], "persona_summary": "人格摘要"},
    }]
    text = formatting.format_memories_for_injection(memories)
    assert "人格摘要" in text and "检索原文" not in text and "事实原文" not in text
    fake = formatting.format_memories_for_fake_tool_call(memories, "之前说过什么")
    assert json.loads(fake[1]["content"])["results"][0]["content"] == "人格摘要"
