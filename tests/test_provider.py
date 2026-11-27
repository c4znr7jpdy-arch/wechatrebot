from io import BytesIO
import copy
from dataclasses import replace
from types import SimpleNamespace
from urllib.error import HTTPError, URLError

import pytest

from wechatrebot_manager import provider
from wechatrebot_manager.plugin_errors import ProviderTestError
from wechatrebot_manager.provider import (
    DEEPSEEK_PROVIDER_API_BASE,
    DEEPSEEK_PROVIDER_MODEL,
    DEFAULT_PROVIDER_API_BASE,
    DEFAULT_PROVIDER_MODEL,
    DEFAULT_PROVIDER_MODEL_ID,
    DEFAULT_PROVIDER_SOURCE_ID,
    MODEL_PROVIDER_PRESETS,
    OpenAIProviderProfile,
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    discover_openai_models,
    infer_provider_modalities,
    normalize_api_base,
    provider_display_name,
    provider_source_id,
    prepare_provider_save,
    supports_thinking_toggle,
    thinking_mode_adapter,
    without_thinking_mode,
    with_thinking_mode,
)


def test_provider_profile_generates_astrbot_source_and_model() -> None:
    profile = OpenAIProviderProfile(
        api_key=" customer-secret ",
        api_base="https://api.example.com/v1/",
        model=" chat-model ",
    )
    assert profile.validate() == ()
    source = profile.astrbot_source()
    model = profile.astrbot_model()
    assert source["key"] == ["customer-secret"]
    assert source["api_base"] == "https://api.example.com/v1"
    assert source["timeout"] == PROVIDER_REQUEST_TIMEOUT_SECONDS
    assert model["id"] == "commercial_openai/chat-model"
    assert model["provider_source_id"] == source["id"]


def test_provider_profile_keeps_public_id_independent_of_connection() -> None:
    profile = OpenAIProviderProfile(
        "secret", "https://example.test/v1", "vendor/model", "isolated",
        provider_id=" legacy/vendor/model ",
    )
    assert profile.validate() == ()
    assert profile.normalized().provider_id == "legacy/vendor/model"
    assert profile.model_id == "legacy/vendor/model"
    assert profile.astrbot_model()["id"] == "legacy/vendor/model"
    assert profile.astrbot_model()["provider_source_id"] == "isolated"


@pytest.mark.parametrize("identifier", ["x" * 257, "old\nmodel", "old\x7fmodel", "old\x85model", "\nold/model"])
def test_provider_profile_rejects_invalid_public_ids(identifier) -> None:
    profile = OpenAIProviderProfile(
        "secret", "https://example.test/v1", "model", provider_id=identifier,
    )
    assert "模型服务 ID 格式无效" in " ".join(profile.validate())
    with pytest.raises(ValueError, match="模型服务 ID 格式无效"):
        prepare_provider_save(profile, [], [])


def _provider_save_records():
    profile = OpenAIProviderProfile("old-secret", "https://example.test/v1", "first", "shared")
    return profile, [profile.astrbot_source()], [profile.astrbot_model()]


@pytest.mark.parametrize("enabled", [True, False])
def test_editing_shared_credentials_isolates_source_and_keeps_model_id(enabled) -> None:
    profile, sources, providers = _provider_save_records()
    sibling = replace(profile, model="second").astrbot_model()
    sibling["enable"] = enabled
    providers.append(sibling)
    original = copy.deepcopy((sources, providers))

    saved, source = prepare_provider_save(replace(profile, api_key="new-secret"), sources, providers)

    assert saved.model_id == profile.model_id
    assert saved.source_id != profile.source_id
    assert source["id"] == saved.source_id
    assert source["key"] == ["new-secret"]
    assert (sources, providers) == original


def test_adding_different_model_credentials_does_not_overwrite_existing_source() -> None:
    profile, sources, providers = _provider_save_records()
    saved, source = prepare_provider_save(
        replace(profile, model="second", api_key="other-secret"), sources, providers,
        create_new=True,
    )
    assert saved.source_id != profile.source_id
    assert saved.model_id == f"{saved.source_id}/second"
    assert source["key"] == ["other-secret"]
    assert sources[0]["key"] == ["old-secret"]


def test_adding_same_model_with_other_credentials_creates_distinct_identity() -> None:
    profile, sources, providers = _provider_save_records()
    saved, source = prepare_provider_save(
        replace(profile, api_key="other-secret"), sources, providers, create_new=True,
    )
    assert saved.model_id != profile.model_id
    assert saved.model_id == f"{saved.source_id}/first"
    assert source["key"] == ["other-secret"]
    assert providers[0]["id"] == profile.model_id
    assert sources[0]["key"] == ["old-secret"]


def test_adding_same_model_after_isolation_checks_actual_model_credentials() -> None:
    profile, sources, providers = _provider_save_records()
    isolated = replace(profile, source_id="isolated", api_key="other-secret", provider_id=profile.model_id)
    sources.append(isolated.astrbot_source())
    providers[0] = isolated.astrbot_model()
    saved, source = prepare_provider_save(profile, sources, providers, create_new=True)
    assert saved.model_id != profile.model_id
    assert saved.source_id not in {"shared", "isolated"}
    assert source["key"] == ["old-secret"]
    assert providers[0]["provider_source_id"] == "isolated"


def test_duplicate_of_isolated_model_reuses_its_existing_connection() -> None:
    profile, sources, providers = _provider_save_records()
    isolated = replace(profile, source_id="isolated", api_key="other-secret", provider_id=profile.model_id)
    sources.append({**isolated.astrbot_source(), "proxy": "http://127.0.0.1:9000"})
    providers[0] = isolated.astrbot_model()
    providers.append(replace(profile, model="second").astrbot_model())
    saved, source = prepare_provider_save(
        replace(profile, api_key="other-secret"), sources, providers, create_new=True,
    )
    assert saved.model_id == profile.model_id
    assert saved.source_id == "isolated"
    assert source["proxy"] == "http://127.0.0.1:9000"


@pytest.mark.parametrize("sources,providers", [(None, None), ({}, {}), ([], [None]), ([None], [])])
def test_provider_save_accepts_missing_or_legacy_record_lists(sources, providers) -> None:
    profile = OpenAIProviderProfile("secret", "https://example.test/v1", "model")
    saved, source = prepare_provider_save(profile, sources, providers)
    assert saved == profile.normalized()
    assert source == profile.astrbot_source()


@pytest.mark.parametrize("create_new", [False, True])
def test_matching_credentials_reuse_source_and_model_identity(create_new) -> None:
    profile, sources, providers = _provider_save_records()
    sources[0]["api_base"] = "https://example.test/"
    saved, source = prepare_provider_save(profile, sources, providers, create_new=create_new)
    assert saved.model_id == profile.model_id
    assert saved.source_id == profile.source_id
    assert source["api_base"] == "https://example.test/v1"


def test_editing_exclusive_source_updates_in_place() -> None:
    profile, sources, providers = _provider_save_records()
    saved, source = prepare_provider_save(
        replace(profile, api_key="replacement"), sources, providers,
    )
    assert saved.source_id == profile.source_id
    assert saved.model_id == profile.model_id
    assert source["key"] == ["replacement"]


def test_multiple_saved_keys_are_not_treated_as_one_matching_key() -> None:
    profile, sources, providers = _provider_save_records()
    sources[0]["key"] = [profile.api_key, "rotation-secret"]
    providers.append(replace(profile, model="second").astrbot_model())
    saved, source = prepare_provider_save(profile, sources, providers)
    assert saved.source_id != profile.source_id
    assert saved.model_id == profile.model_id
    assert source["key"] == [profile.api_key]
    assert sources[0]["key"] == [profile.api_key, "rotation-secret"]


def test_source_clone_preserves_transport_extras_but_replaces_managed_fields() -> None:
    profile, sources, providers = _provider_save_records()
    sources[0].update({
        "proxy": "http://127.0.0.1:9000", "custom_headers": {"x-route": "region"},
        "api_version": "2025-01-01", "provider": "azure", "type": "azure_openai",
        "timeout": 900, "enable": False,
    })
    providers.append(replace(profile, model="second").astrbot_model())
    saved, source = prepare_provider_save(replace(profile, api_key="replacement"), sources, providers)
    for field in ("proxy", "custom_headers", "api_version", "provider", "type"):
        assert source[field] == sources[0][field]
    assert source["timeout"] == PROVIDER_REQUEST_TIMEOUT_SECONDS
    assert source["enable"] is True
    assert source["embedding_api_key"] == "replacement"
    assert source["embedding_api_base"] == saved.api_base
    source["custom_headers"]["x-route"] = "changed"
    assert sources[0]["custom_headers"] == {"x-route": "region"}


def test_changed_endpoint_inherits_previous_model_transport_extras() -> None:
    profile, sources, providers = _provider_save_records()
    sources[0]["proxy"] = "http://127.0.0.1:9000"
    saved, source = prepare_provider_save(
        replace(profile, source_id="another", api_base="https://other.test/v1",
                provider_id=profile.model_id),
        sources, providers,
    )
    assert saved.model_id == profile.model_id
    assert source["id"] == "another"
    assert source["api_base"] == "https://other.test/v1"
    assert source["proxy"] == sources[0]["proxy"]


def test_shared_source_endpoint_change_also_isolates_credentials() -> None:
    profile, sources, providers = _provider_save_records()
    providers.append(replace(profile, model="second").astrbot_model())
    saved, source = prepare_provider_save(
        replace(profile, api_base="https://other.test/v1"), sources, providers,
    )
    assert saved.source_id != profile.source_id
    assert saved.model_id == profile.model_id
    assert source["api_base"] == "https://other.test/v1"
    assert sources[0]["api_base"] == profile.api_base


def test_generated_source_id_is_random_not_derived_from_secret(monkeypatch) -> None:
    profile, sources, providers = _provider_save_records()
    providers.append(replace(profile, model="second").astrbot_model())
    monkeypatch.setattr(provider, "uuid4", lambda: SimpleNamespace(hex="0123456789abcdef" * 2))
    generated = [
        prepare_provider_save(replace(profile, api_key=secret), sources, providers)[0]
        for secret in ("first-private-key", "second-private-key")
    ]
    assert [item.source_id for item in generated] == ["custom_0123456789abcdef01234567"] * 2
    assert all(item.validate() == () for item in generated)


def test_generated_source_id_skips_existing_source_collision(monkeypatch) -> None:
    profile, sources, providers = _provider_save_records()
    providers.append(replace(profile, model="second").astrbot_model())
    sources.append({"id": "custom_" + "a" * 24})
    identifiers = iter(("a" * 32, "b" * 32))
    monkeypatch.setattr(provider, "uuid4", lambda: SimpleNamespace(hex=next(identifiers)))
    saved, _ = prepare_provider_save(replace(profile, api_key="replacement"), sources, providers)
    assert saved.source_id == "custom_" + "b" * 24


def test_provider_profile_rejects_insecure_remote_url() -> None:
    profile = OpenAIProviderProfile(
        api_key="secret",
        api_base="http://api.example.com/v1",
        model="model",
    )
    assert "必须使用 HTTPS" in " ".join(profile.validate())


def test_provider_profile_allows_loopback_http() -> None:
    profile = OpenAIProviderProfile(
        api_key="secret",
        api_base="http://127.0.0.1:8000/v1",
        model="model",
    )
    assert profile.validate() == ()


@pytest.mark.parametrize("value,expected", [
    (" https://api.example.test/ ", "https://api.example.test/v1"),
    ("http://127.0.0.1:8000", "http://127.0.0.1:8000/v1"),
    ("https://api.example.test/proxy", "https://api.example.test/proxy/v1"),
    ("https://api.example.test/v1/", "https://api.example.test/v1"),
    ("https://api.example.test/v1/v1/v1/", "https://api.example.test/v1"),
    ("https://dashscope.aliyuncs.com/compatible-mode", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("https://dashscope.aliyuncs.com/compatible-mode/v1/v1", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
    ("https://open.bigmodel.cn/api/paas/v4/", "https://open.bigmodel.cn/api/paas/v4"),
    ("https://ark.cn-beijing.volces.com/api/v3", "https://ark.cn-beijing.volces.com/api/v3"),
    ("https://generativelanguage.googleapis.com/v1beta/openai", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("https://api.example.test/models", "https://api.example.test/models"),
])
def test_api_base_normalization_is_shared_and_idempotent(value, expected) -> None:
    assert normalize_api_base(value) == expected
    assert normalize_api_base(expected) == expected
    profile = OpenAIProviderProfile(api_key="secret", api_base=value, model="model")
    assert profile.validate() == ()
    assert profile.normalized().api_base == expected
    assert profile.astrbot_source()["api_base"] == expected
    assert profile.astrbot_source()["embedding_api_base"] == expected


@pytest.mark.parametrize("value", [
    "https://user:password@api.example.test",
    "https://api.example.test?key=secret/",
    "https://api.example.test#secret/",
    "api.example.test/v1",
])
def test_api_base_normalization_does_not_hide_invalid_configuration(value) -> None:
    assert normalize_api_base(value) == value
    assert OpenAIProviderProfile("secret", value, "model").validate()


def test_thinking_switch_is_available_for_every_model_and_maps_known_protocols() -> None:
    base = "https://token-plan-cn.xiaomimimo.com/v1"
    assert supports_thinking_toggle(base, "mimo-v2.5")
    assert supports_thinking_toggle("https://api.deepseek.com/v1", "deepseek-v4-flash")
    assert supports_thinking_toggle("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen3-max")
    assert supports_thinking_toggle("https://api.x.ai/v1", "grok-4.6")
    assert supports_thinking_toggle("https://api.openai.com/v1", "gpt-5.6")
    assert not supports_thinking_toggle("https://example.com/v1", "mimo-v2.5")
    disabled = OpenAIProviderProfile("secret", base, "mimo-v2.5", thinking_enabled=False)
    assert disabled.validate() == ()
    assert disabled.astrbot_model()["custom_extra_body"] == {"thinking": {"type": "disabled"}}
    unsupported = OpenAIProviderProfile("secret", "https://example.com/v1", "mimo-v2.5", thinking_enabled=False)
    assert unsupported.validate() == ()
    assert unsupported.astrbot_model()["custom_extra_body"] == {}
    assert with_thinking_mode(
        {"safe_extra": "keep"}, False,
        api_base="https://dashscope.aliyuncs.com/compatible-mode/v1", model="qwen3-max",
    ) == {"safe_extra": "keep", "enable_thinking": False}
    assert with_thinking_mode(
        {}, False, api_base="https://api.x.ai/v1", model="grok-4.6",
    ) == {"reasoning_effort": "low"}
    assert with_thinking_mode(
        {}, False, api_base="https://api.openai.com/v1", model="gpt-5.6",
    ) == {"reasoning_effort": "none"}
    invalid = OpenAIProviderProfile("secret", base, "mimo-v2.5", thinking_enabled="false")
    assert "只能选择" in " ".join(invalid.validate())


@pytest.mark.parametrize(
    ("api_base", "model", "adapter", "disabled_body"),
    [
        (
            "https://ark.cn-beijing.volces.com/api/v3",
            "doubao-seed-2-0-lite-260215",
            "thinking",
            {"thinking": {"type": "disabled"}},
        ),
        (
            "https://open.bigmodel.cn/api/paas/v4",
            "glm-5.2",
            "thinking",
            {"thinking": {"type": "disabled"}},
        ),
        (
            "https://api.siliconflow.cn/v1",
            "deepseek-ai/DeepSeek-V4-Flash",
            "enable_thinking",
            {"enable_thinking": False},
        ),
        (
            "https://api.moonshot.cn/v1",
            "kimi-k2.6",
            "thinking",
            {"thinking": {"type": "disabled"}},
        ),
        (
            "https://openrouter.ai/api/v1",
            "anthropic/claude-sonnet-4.7",
            "reasoning_effort_object",
            {"reasoning": {"effort": "none"}},
        ),
        (
            "https://api.together.xyz/v1",
            "Qwen/Qwen3.5-9B",
            "reasoning_enabled",
            {"reasoning": {"enabled": False}},
        ),
        (
            "https://api.fireworks.ai/inference/v1",
            "accounts/fireworks/models/deepseek-v4",
            "reasoning_effort_none",
            {"reasoning_effort": "none"},
        ),
        (
            "https://api.groq.com/openai/v1",
            "qwen/qwen3.8-27b",
            "reasoning_effort_default",
            {"reasoning_effort": "none"},
        ),
        (
            "https://api.cerebras.ai/v1",
            "zai-glm-4.7",
            "reasoning_effort_none",
            {"reasoning_effort": "none"},
        ),
        (
            "http://127.0.0.1:11434/v1",
            "qwen3:8b",
            "reasoning_effort_none",
            {"reasoning_effort": "none"},
        ),
        (
            "https://demo.openai.azure.com/openai/v1",
            "gpt-5.2",
            "reasoning_effort_none",
            {"reasoning_effort": "none"},
        ),
    ],
)
def test_thinking_adapter_covers_common_compatible_services(
    api_base: str,
    model: str,
    adapter: str,
    disabled_body: dict[str, object],
) -> None:
    assert thinking_mode_adapter(api_base, model) == adapter
    assert with_thinking_mode({}, False, api_base=api_base, model=model) == disabled_body


@pytest.mark.parametrize(
    ("api_base", "model"),
    [
        ("https://api.moonshot.cn/v1", "kimi-k2.7-code"),
        ("https://api.groq.com/openai/v1", "openai/gpt-oss-120b"),
        ("https://api.fireworks.ai/inference/v1", "accounts/fireworks/models/minimax-m2"),
        ("http://127.0.0.1:11434/v1", "gpt-oss:20b"),
        ("https://openrouter.ai/api/v1", "google/gemini-3.5-flash"),
    ],
)
def test_thinking_adapter_ignores_models_that_cannot_safely_disable_reasoning(
    api_base: str,
    model: str,
) -> None:
    assert thinking_mode_adapter(api_base, model) is None
    assert with_thinking_mode({}, False, api_base=api_base, model=model) == {}


def test_nested_reasoning_adapter_preserves_unrelated_options() -> None:
    api_base = "https://openrouter.ai/api/v1"
    model = "anthropic/claude-sonnet-4.7"
    original = {"reasoning": {"max_tokens": 4096}, "safe_extra": "keep"}

    enabled = with_thinking_mode(original, True, api_base=api_base, model=model)
    assert enabled == {
        "reasoning": {"max_tokens": 4096, "effort": "high"},
        "safe_extra": "keep",
    }
    assert without_thinking_mode(enabled, api_base=api_base, model=model) == original


def test_grok_46_defaults_expose_multimodal_capabilities() -> None:
    profile = OpenAIProviderProfile(
        api_key="customer-secret",
        api_base=DEFAULT_PROVIDER_API_BASE,
        model=DEFAULT_PROVIDER_MODEL,
        source_id=DEFAULT_PROVIDER_SOURCE_ID,
    )

    assert profile.model_id == DEFAULT_PROVIDER_MODEL_ID
    assert profile.astrbot_source()["id"] == DEFAULT_PROVIDER_SOURCE_ID
    assert profile.astrbot_model()["modalities"] == [
        "text",
        "image",
        "audio",
        "tool_use",
    ]


def test_image_provider_keeps_transport_compatible_and_is_not_a_chat_model() -> None:
    profile = OpenAIProviderProfile(
        api_key="image-secret",
        api_base="https://images.example.com/v1",
        model="image-v2",
        service_kind="image",
        modalities=("image",),
    )

    assert profile.astrbot_source()["provider_type"] == "chat_completion"
    assert profile.astrbot_model()["service_kind"] == "image"
    assert profile.astrbot_model()["modalities"] == ["image"]


def test_custom_chat_service_uses_multimodal_input_by_default() -> None:
    custom = next(item for item in MODEL_PROVIDER_PRESETS if item["id"] == "custom")
    sub2 = next(item for item in MODEL_PROVIDER_PRESETS if item["id"] == "sub2")
    assert custom["modalities"] == ("text", "image", "audio")
    assert sub2["modalities"] == ("text", "image", "audio")

    profile = OpenAIProviderProfile(
        api_key="multimodal-secret",
        api_base="https://models.example.com/v1",
        model="vendor-model",
        service_kind="chat",
        modalities=("text", "image", "audio"),
    )

    assert profile.astrbot_model()["modalities"] == [
        "text", "image", "audio", "tool_use",
    ]
    assert infer_provider_modalities(
        "chat", "vendor-model", ("text", "image", "unsupported")
    ) == ("text", "image", "audio")
    assert infer_provider_modalities(
        "chat", DEFAULT_PROVIDER_MODEL, ("text",)
    ) == ("text", "image", "audio")


def test_custom_chat_service_defaults_to_multimodal_input() -> None:
    profile = OpenAIProviderProfile(
        api_key="customer-secret",
        api_base="https://models.example.com/v1",
        model="vendor-model",
    )

    assert infer_provider_modalities("chat", "vendor-model") == (
        "text", "image", "audio",
    )
    assert profile.astrbot_model()["modalities"] == [
        "text", "image", "audio", "tool_use",
    ]


def test_voice_provider_is_classified_separately_from_chat_and_image() -> None:
    profile = OpenAIProviderProfile(
        api_key="voice-secret",
        api_base="https://voice.example.com/v1",
        model="speech-1",
        service_kind="voice",
    )
    assert profile.astrbot_model()["service_kind"] == "voice"
    assert profile.astrbot_model()["modalities"] == ["audio"]


def test_invalid_model_service_kind_returns_customer_validation_error() -> None:
    profile = OpenAIProviderProfile(
        api_key="secret",
        api_base="https://models.example.com/v1",
        model="model-v1",
        service_kind="unsupported",
    )

    assert profile.validate() == ("模型服务类型不受支持，请重新选择。",)


def test_provider_presets_have_stable_hidden_ids_and_customer_labels() -> None:
    assert provider_source_id(DEFAULT_PROVIDER_API_BASE, DEFAULT_PROVIDER_MODEL) == "jyjapi"
    assert provider_source_id(DEEPSEEK_PROVIDER_API_BASE, DEEPSEEK_PROVIDER_MODEL) == "deepseek"
    assert provider_source_id(f"{DEEPSEEK_PROVIDER_API_BASE}/v1", DEEPSEEK_PROVIDER_MODEL) == "deepseek"
    assert provider_source_id("https://models.example.com", "chat") == provider_source_id(
        "https://models.example.com/v1/v1/", "another-chat"
    )
    assert provider_source_id("https://models.example.com/v1", "chat") == provider_source_id(
        "https://models.example.com/v1/", "another-chat"
    )
    assert provider_display_name("jyjapi", "grok-4.6") == "免费 Grok 4.6"
    assert provider_display_name("deepseek", "deepseek-flash") == "DeepSeek · flash"
    assert not {"embedding_custom", "image_custom", "voice_custom"}.intersection(
        item["id"] for item in MODEL_PROVIDER_PRESETS
    )


@pytest.mark.parametrize("api_base,expected_path", [
    ("https://models.example.test", "/v1/models"),
    ("https://models.example.test/v1", "/v1/models"),
    ("https://models.example.test/v1/v1/", "/v1/models"),
    ("https://models.example.test/v1/v1/models", "/v1/models"),
    ("https://models.example.test/models", "/models"),
])
def test_model_directory_reads_openai_compatible_models(monkeypatch, api_base, expected_path) -> None:
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit):
            return b'{"data":[{"id":"chat-pro"},{"id":"chat-fast"},{"id":"chat-pro"}]}'

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["authorization"] = request.headers["Authorization"]
        captured["timeout"] = timeout
        return Response()

    monkeypatch.setattr(provider, "urlopen", fake_urlopen)

    models = discover_openai_models(
        api_key="secret", api_base=api_base
    )

    assert models == ("chat-fast", "chat-pro")
    assert captured == {
        "url": f"https://models.example.test{expected_path}",
        "authorization": "Bearer secret",
        "timeout": 15.0,
    }


@pytest.mark.parametrize(("api_key", "api_base", "message"), [
    ("", "https://models.example.test/v1", "API Key / Token Plan 不能为空"),
    ("secret", "models.example.test/v1", "HTTP(S) 绝对地址"),
    ("secret", "https://[invalid", "HTTP(S) 绝对地址"),
])
def test_model_directory_rejects_invalid_configuration_before_request(
    monkeypatch, api_key, api_base, message,
) -> None:
    def unexpected_request(*_args, **_kwargs):
        pytest.fail("invalid configuration must not send a request")

    monkeypatch.setattr(provider, "urlopen", unexpected_request)
    with pytest.raises(ProviderTestError) as caught:
        discover_openai_models(api_key=api_key, api_base=api_base)
    assert caught.value.error_code == "15004"
    assert message in str(caught.value)


@pytest.mark.parametrize(("status", "code"), [
    (401, "16002"), (403, "16003"), (404, "16009"),
    (429, "16004"), (500, "16008"), (400, "16010"),
])
def test_model_directory_preserves_http_classification_without_response_body(
    monkeypatch, status, code,
) -> None:
    def rejected_request(*_args, **_kwargs):
        raise HTTPError(
            "https://models.example.test/v1/models", status, "upstream-secret",
            {}, BytesIO(b"upstream-secret"),
        )

    monkeypatch.setattr(provider, "urlopen", rejected_request)
    with pytest.raises(ProviderTestError) as caught:
        discover_openai_models(api_key="secret", api_base="https://models.example.test/v1")
    assert caught.value.error_code == code
    assert "secret" not in str(caught.value)
    if status == 404:
        assert "手动填写模型名称" in str(caught.value)


@pytest.mark.parametrize(("failure", "code"), [
    (TimeoutError("transport-secret"), "16007"),
    (URLError(TimeoutError("transport-secret")), "16007"),
    (URLError("transport-secret"), "16006"),
    (OSError("transport-secret"), "16006"),
])
def test_model_directory_distinguishes_timeout_from_connection_failure(
    monkeypatch, failure, code,
) -> None:
    def failed_request(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(provider, "urlopen", failed_request)
    with pytest.raises(ProviderTestError) as caught:
        discover_openai_models(api_key="secret", api_base="https://models.example.test/v1")
    assert caught.value.error_code == code
    assert "secret" not in str(caught.value)


@pytest.mark.parametrize("raw", [
    b"invalid-json-secret", b'{}', b'{"data":[]}', b"x" * (2 * 1024 * 1024 + 1),
], ids=["invalid-json", "missing-models", "empty-models", "oversized-list"])
def test_model_directory_rejects_unusable_responses_with_safe_message(monkeypatch, raw) -> None:
    monkeypatch.setattr(provider, "urlopen", lambda *_args, **_kwargs: BytesIO(raw))
    with pytest.raises(ProviderTestError) as caught:
        discover_openai_models(api_key="secret", api_base="https://models.example.test/v1")
    assert caught.value.error_code == "16012"
    assert "secret" not in str(caught.value)
