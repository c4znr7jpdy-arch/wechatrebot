import pytest

from wechatrebot_manager import provider
from wechatrebot_manager.provider import (
    DEEPSEEK_PROVIDER_API_BASE,
    DEEPSEEK_PROVIDER_MODEL,
    DEFAULT_PROVIDER_API_BASE,
    DEFAULT_PROVIDER_MODEL,
    DEFAULT_PROVIDER_MODEL_ID,
    DEFAULT_PROVIDER_SOURCE_ID,
    OpenAIProviderProfile,
    PROVIDER_REQUEST_TIMEOUT_SECONDS,
    discover_openai_models,
    provider_display_name,
    provider_source_id,
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
    assert provider_source_id("https://models.example.com/v1", "chat") == provider_source_id(
        "https://models.example.com/v1/", "another-chat"
    )
    assert provider_display_name("jyjapi", "grok-4.6") == "免费 Grok 4.6"
    assert provider_display_name("deepseek", "deepseek-flash") == "DeepSeek · flash"


def test_model_directory_reads_openai_compatible_models(monkeypatch) -> None:
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
        api_key="secret", api_base="https://models.example.test/v1"
    )

    assert models == ("chat-fast", "chat-pro")
    assert captured == {
        "url": "https://models.example.test/v1/models",
        "authorization": "Bearer secret",
        "timeout": 15.0,
    }
