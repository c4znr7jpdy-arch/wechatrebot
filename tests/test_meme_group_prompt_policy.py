from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from test_hot_config_runtime import _runtime_function


ROOT = Path(__file__).resolve().parents[1]


def _policy_module() -> ModuleType:
    path = ROOT / "runtime" / "meme_manager" / "wechatrebot_group_policy.py"
    spec = importlib.util.spec_from_file_location("wechatrebot_group_policy", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Event:
    def __init__(
        self,
        *,
        group_id: str = "group@chatroom",
        account_id: str = "wxid_bot",
        platform: str = "aiocqhttp",
    ) -> None:
        self.group_id = group_id
        self.account_id = account_id
        self.platform = platform
        self.extra: dict[str, object] = {}

    def get_group_id(self) -> str:
        return self.group_id

    def get_self_id(self) -> str:
        return self.account_id

    def get_platform_name(self) -> str:
        return self.platform

    def get_extra(self, key: str):
        return self.extra.get(key)

    def set_extra(self, key: str, value: object) -> None:
        self.extra[key] = value

    def get_result(self):
        return None


def _write_policy(path: Path, **values: object) -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "accounts": {
                    "wxid_bot": {
                        "groups": {
                            "group@chatroom": {
                                "bot_enabled": True,
                                "reply_mode": "mention",
                                "allowed_features": ["meme_manager"],
                                **values,
                            }
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize(
    ("overrides", "allowed"),
    [
        ({}, True),
        ({"allowed_features": []}, False),
        ({"bot_enabled": False}, False),
        ({"reply_mode": "off"}, False),
    ],
)
def test_hidden_meme_prompt_follows_group_feature_policy(
    tmp_path: Path, overrides: dict[str, object], allowed: bool
) -> None:
    policy_path = tmp_path / "group-policies.json"
    _write_policy(policy_path, **overrides)

    assert _policy_module().group_allows_meme_prompt(policy_path, Event()) is allowed


def test_hidden_meme_prompt_fails_closed_for_missing_or_corrupt_group_policy(
    tmp_path: Path,
) -> None:
    module = _policy_module()
    policy_path = tmp_path / "group-policies.json"
    assert module.group_allows_meme_prompt(policy_path, Event()) is False
    policy_path.write_text("{", encoding="utf-8")
    assert module.group_allows_meme_prompt(policy_path, Event()) is False


def test_non_group_conversation_keeps_existing_meme_behavior(tmp_path: Path) -> None:
    assert _policy_module().group_allows_meme_prompt(
        tmp_path / "missing.json", Event(group_id="")
    ) is True


def test_delivery_patch_gates_native_prompt_at_request_time() -> None:
    patch = (ROOT / "patches" / "astrbot-plugin-meme_manager.patch").read_text(
        encoding="utf-8"
    )
    assert "HIDDEN_MEME_PROMPT_HEAD" not in patch
    assert "group_allows_meme_prompt" in patch
    assert "req.system_prompt = self._strip_meme_prompt(req.system_prompt)" in patch
    assert "persona[\"prompt\"] = self.persona_base_prompts[key]" not in patch


@pytest.fixture(scope="module")
def patched_meme_sources(tmp_path_factory) -> dict[str, str]:
    target = tmp_path_factory.mktemp("meme-prompt-runtime")
    files = ("main.py", "mixins/event_handlers.py", "_conf_schema.json",
             "pages/app/settings/config.js", "pages/app/catalog/script.js")
    for name in files:
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "runtime/meme_manager" / name, destination)
    subprocess.run(["git", "init", "--quiet"], cwd=target, check=True)
    subprocess.run(
        ["git", "apply", "--whitespace=error-all",
         str(ROOT / "patches/astrbot-plugin-meme_manager.patch")],
        cwd=target, check=True, capture_output=True,
    )
    return {name: (target / name).read_text(encoding="utf-8") for name in files}


class ToolSet(dict):
    def get_full_tool_set(self):
        return ToolSet(self)

    def get_tool(self, name: str):
        return self.get(name)

    def remove_tool(self, name: str) -> None:
        self.pop(name, None)


@pytest.fixture
def prompt_runtime(patched_meme_sources, tmp_path, monkeypatch):
    engine_root = tmp_path / "app/astrbot"
    engine_root.mkdir(parents=True)
    monkeypatch.chdir(engine_root)
    policy_path = tmp_path / "data/manager/group-policies.json"
    policy_path.parent.mkdir(parents=True)
    _write_policy(policy_path)
    rng = Mock()
    rng.randint.return_value = 51
    scope = {"Path": Path, "re": re, "random": rng,
             "group_allows_meme_prompt": _policy_module().group_allows_meme_prompt}
    utils = (ROOT / "runtime/meme_manager/utils.py").read_text(encoding="utf-8")
    for name in ("normalize_probability", "probability_hit", "dict_to_string"):
        _runtime_function(utils, name, scope)
    for name, value in (
        ("MEME_PROMPT_MARKER_START", "<!-- meme_manager_prompt:start -->"),
        ("MEME_PROMPT_MARKER_END", "<!-- meme_manager_prompt:end -->"),
        ("SEMANTIC_PROMPT_MARKER_START", "<!-- meme_manager_semantic_prompt:start -->"),
        ("SEMANTIC_PROMPT_MARKER_END", "<!-- meme_manager_semantic_prompt:end -->"),
    ):
        scope[name] = value
    owner = SimpleNamespace(
        emotions_probability=50, emotion_llm_enabled=False, sys_prompt_add="",
        prompt_head="Choose a category: ", prompt_tail="", category_mapping_string="",
        _semantic_pack_ready=Mock(return_value=True),
        _resolve_runtime_pack_context=Mock(side_effect=AssertionError("Unexpected category fallback")),
        _build_meme_quantity_prompt=lambda: "",
    )
    for name in ("_strip_meme_prompt", "_wrap_meme_prompt", "_semantic_system_prompt",
                 "_build_meme_prompt", "_apply_request_prompt", "_semantic_mode_active"):
        method = _runtime_function(patched_meme_sources["main.py"], name, scope)
        setattr(owner, name, method.__get__(owner))
    owner._remove_semantic_tool = _runtime_function(
        patched_meme_sources["main.py"], "_remove_semantic_tool", scope
    )
    return owner, rng, scope, policy_path


def _request_with_stale_prompt(owner):
    tools = ToolSet(search_memes=object(), another_tool=object())
    request = SimpleNamespace(
        system_prompt="Base persona" + owner._wrap_meme_prompt("old category instructions")
        + owner._semantic_system_prompt(), func_tool=tools,
    )
    return request, tools


@pytest.mark.parametrize(("probability", "roll", "selected", "draws"), [
    (0, 1, False, 0), (100, 100, True, 0), (50, 51, False, 1), (50, 50, True, 1),
])
def test_semantic_tool_probability_controls_prompt_and_tool_once(
    prompt_runtime, probability, roll, selected, draws,
):
    owner, rng, _scope, _policy = prompt_runtime
    owner.emotions_probability = probability
    rng.randint.return_value = roll
    request, shared_tools = _request_with_stale_prompt(owner)
    event = Event()
    event.set_extra("meme_manager_runtime_pack_id", "pack")
    owner._apply_request_prompt(request, event)
    assert bool(request.func_tool.get_tool("search_memes")) is selected
    assert request.func_tool.get_tool("another_tool") is not None
    assert shared_tools.get_tool("search_memes") is not None
    assert ("meme_manager_semantic_prompt:start" in request.system_prompt) is selected
    assert "old category instructions" not in request.system_prompt
    if not selected:
        assert request.system_prompt == "Base persona"
    assert event.get_extra("meme_manager_semantic_mode") == "tool"
    assert rng.randint.call_count == draws
    owner._resolve_runtime_pack_context.assert_not_called()


def test_group_denial_removes_tool_prompt_and_stale_semantic_state(prompt_runtime):
    owner, rng, _scope, policy = prompt_runtime
    _write_policy(policy, allowed_features=[])
    request, _shared_tools = _request_with_stale_prompt(owner)
    event = Event()
    event.extra.update(meme_manager_semantic_active=True, meme_manager_semantic_mode="tool",
                       meme_manager_semantic_selected_ids=["meme:old"],
                       meme_manager_semantic_candidates={"meme:old": {}},
                       meme_manager_semantic_verified_pack_id="old-pack")
    owner._apply_request_prompt(request, event)
    assert request.system_prompt == "Base persona"
    assert request.func_tool.get_tool("search_memes") is None
    assert event.get_extra("meme_manager_semantic_active") is False
    assert event.get_extra("meme_manager_semantic_mode") == ""
    assert event.get_extra("meme_manager_semantic_verified_pack_id") == ""
    assert event.get_extra("meme_manager_semantic_selected_ids") == []
    assert event.get_extra("meme_manager_semantic_candidates") == {}
    owner._semantic_pack_ready.assert_not_called()
    rng.randint.assert_not_called()


def test_each_request_rechecks_probability_and_clears_previous_selection(prompt_runtime):
    owner, rng, _scope, _policy = prompt_runtime
    rng.randint.side_effect = [1, 100, 1]
    event = Event()
    shared_tools = ToolSet(search_memes=object())
    previous_prompt = "Base persona"
    for selected in (True, False, True):
        event.extra.update(meme_manager_semantic_candidates={"meme:old": {}},
                           meme_manager_semantic_selected_ids=["meme:old"])
        request = SimpleNamespace(system_prompt=previous_prompt, func_tool=shared_tools)
        owner._apply_request_prompt(request, event)
        assert bool(request.func_tool.get_tool("search_memes")) is selected
        assert request.system_prompt.count("meme_manager_semantic_prompt:start") == int(selected)
        assert event.get_extra("meme_manager_semantic_candidates") == {}
        assert event.get_extra("meme_manager_semantic_selected_ids") == []
        previous_prompt = request.system_prompt
    assert rng.randint.call_count == 3


def test_semantic_auxiliary_model_does_not_sample_at_request_time(prompt_runtime):
    owner, rng, _scope, _policy = prompt_runtime
    owner.emotion_llm_enabled = True
    owner.emotions_probability = 0
    request, _shared_tools = _request_with_stale_prompt(owner)
    event = Event()
    owner._apply_request_prompt(request, event)
    assert request.system_prompt == "Base persona"
    assert request.func_tool.get_tool("search_memes") is None
    assert event.get_extra("meme_manager_semantic_mode") == "llm"
    rng.randint.assert_not_called()


@pytest.fixture
def response_runtime(prompt_runtime, patched_meme_sources, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "runtime/meme_manager"))
    from backend.meme_parser import MemeParser
    from backend.meme_parser.text_safety import strip_internal_image_ref_lines
    from backend.semantic.models import extract_visible_semantic_reply, parse_meme_id

    owner, _rng, scope, _policy = prompt_runtime
    meme_id = "meme:aaaaaaaaaaaa"
    image = tmp_path / "one.png"
    image.write_bytes(b"local semantic image fixture")
    metadata = {"images": {"a" * 64: {"category": "happy", "relative_path": image.name,
                 "content_sha256": hashlib.sha256(image.read_bytes()).hexdigest()}}}
    candidate = {"id": meme_id, "caption": "A happy image", "tags": ["happy"]}
    scope.update(
        MemeParser=MemeParser, strip_internal_image_ref_lines=strip_internal_image_ref_lines,
        extract_visible_semantic_reply=extract_visible_semantic_reply, parse_meme_id=parse_meme_id,
        logger=Mock(), json=json, PLUGIN_DATA_DIR=tmp_path, REVIEW_CATEGORY="review",
        load_metadata=lambda _pack: metadata,
        safe_relative_path=lambda pack, relative: Path(pack) / relative,
        file_sha256=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        search_memes=AsyncMock(return_value={"candidates": [candidate]}),
    )
    query_source = (ROOT / "runtime/meme_manager/backend/semantic/query.py").read_text("utf-8")
    for name in ("candidate_records", "remember_candidates", "validate_selected_id"):
        _runtime_function(query_source, name, scope)
    owner._resolve_runtime_pack_context = Mock(return_value={"pack_id": "pack", "pack_dir": tmp_path})
    owner._scope_allows_llm_origin = lambda _event: True
    owner._read_config_value = lambda _path, default: default
    owner._build_emotion_semantic_query = AsyncMock(return_value="happy")
    owner._resolve_embedding_provider = Mock(return_value=object())
    owner._build_emotion_llm_injection_suffix = lambda _event: ""
    owner._emotion_llm_generate = AsyncMock(return_value=SimpleNamespace(
        completion_text=json.dumps({"meme_id": meme_id}),
    ))
    owner.remove_invalid_alternative_markup = True
    owner.semantic_top_k, owner.semantic_min_score = 3, 0.3
    for name in ("_make_meme_parser", "_resp_impl", "_resp_semantic_impl", "_resp_semantic_llm_impl"):
        method = _runtime_function(patched_meme_sources["mixins/event_handlers.py"], name, scope)
        setattr(owner, name, method.__get__(owner))
    return prompt_runtime, candidate, metadata


@pytest.mark.parametrize("mode", ["tool", "llm"])
@pytest.mark.parametrize(("probability", "roll", "selected", "draws"), [
    (0, 1, False, 0), (100, 100, True, 0), (50, 51, False, 1), (50, 50, True, 1),
])
def test_semantic_reply_samples_once_and_cleans_markers(
    response_runtime, mode, probability, roll, selected, draws,
):
    (owner, rng, scope, _policy), candidate, _metadata = response_runtime
    owner.emotion_llm_enabled = mode == "llm"
    owner.emotions_probability = probability
    rng.randint.return_value = roll
    event = Event()
    event.set_extra("meme_manager_runtime_pack_id", "pack")
    request, _shared_tools = _request_with_stale_prompt(owner)
    owner._apply_request_prompt(request, event)
    if mode == "tool" and selected:
        records = scope["candidate_records"](scope["PLUGIN_DATA_DIR"], [candidate])
        scope["remember_candidates"](event, records)
    response = SimpleNamespace(completion_text=f"正常回复\n&&{candidate['id']}&&", result_chain=None)

    async def process_twice():
        await owner._resp_impl(event, response)
        await owner._resp_impl(event, response)

    asyncio.run(process_twice())
    assert response.completion_text == "正常回复"
    assert event.get_extra("meme_manager_semantic_selected_ids") == ([candidate["id"]] if selected else [])
    assert event.get_extra("found_emotions") is None
    assert rng.randint.call_count == draws
    expected_calls = int(mode == "llm" and selected)
    assert owner._build_emotion_semantic_query.await_count == expected_calls
    assert scope["search_memes"].await_count == expected_calls
    assert owner._emotion_llm_generate.await_count == expected_calls


def test_auxiliary_probability_miss_discards_stale_candidates(response_runtime):
    (owner, rng, scope, _policy), candidate, _metadata = response_runtime
    owner.emotion_llm_enabled = True
    event = Event()
    event.set_extra("meme_manager_runtime_pack_id", "pack")
    request, _shared_tools = _request_with_stale_prompt(owner)
    owner._apply_request_prompt(request, event)
    records = scope["candidate_records"](scope["PLUGIN_DATA_DIR"], [candidate])
    scope["remember_candidates"](event, records)
    event.set_extra("meme_manager_semantic_selected_ids", [candidate["id"]])
    response = SimpleNamespace(completion_text=f"保留这句\n&&{candidate['id']}&&", result_chain=None)
    asyncio.run(owner._resp_impl(event, response))
    assert response.completion_text == "保留这句"
    assert event.get_extra("meme_manager_semantic_candidates") == {}
    assert event.get_extra("meme_manager_semantic_selected_ids") == []
    assert event.get_extra("found_emotions") is None
    owner._build_emotion_semantic_query.assert_not_awaited()
    scope["search_memes"].assert_not_awaited()
    owner._emotion_llm_generate.assert_not_awaited()
    assert rng.randint.call_count == 1


def test_category_mode_still_injects_prompt_without_request_probability(prompt_runtime, tmp_path):
    owner, rng, scope, _policy = prompt_runtime
    owner._semantic_pack_ready.return_value = False
    owner.emotions_probability = 0
    category = tmp_path / "memes/happy"
    category.mkdir(parents=True)
    (category / "one.png").write_bytes(b"local image presence fixture")
    owner._resolve_runtime_pack_context = Mock(return_value={
        "category_mapping": {"happy": "Happy"}, "memes_dir": category.parent,
    })
    scope.update(runtime_category_mapping=lambda mapping: mapping, IMAGE_EXTENSIONS={".png"})
    request, _shared_tools = _request_with_stale_prompt(owner)
    event = Event()
    owner._apply_request_prompt(request, event)
    assert "happy - Happy" in request.system_prompt
    assert "meme_manager_semantic_prompt:start" not in request.system_prompt
    assert request.func_tool.get_tool("search_memes") is None
    assert event.get_extra("meme_manager_semantic_mode") == ""
    rng.randint.assert_not_called()
