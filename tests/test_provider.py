from wechatrebot_manager.provider import OpenAIProviderProfile


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
