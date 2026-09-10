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


def test_provider_presets_have_stable_hidden_ids_and_customer_labels() -> None:
    assert provider_source_id(DEFAULT_PROVIDER_API_BASE, DEFAULT_PROVIDER_MODEL) == "jyjapi"
    assert provider_source_id(DEEPSEEK_PROVIDER_API_BASE, DEEPSEEK_PROVIDER_MODEL) == "deepseek"
    assert provider_source_id("https://models.example.com/v1", "chat") == provider_source_id(
        "https://models.example.com/v1/", "another-chat"
    )
    assert provider_display_name("jyjapi", "grok-4.6") == "免费 Grok 4.6"
    assert provider_display_name("deepseek", "deepseek-v4-flash") == "DeepSeek · v4-flash"


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
