from wechatrebot_manager.setup_wizard import (
    PROVIDER_DEEPSEEK,
    PROVIDER_LATER,
    provider_profile_for_choice,
)


def test_deepseek_wizard_choice_builds_customer_provider() -> None:
    profile = provider_profile_for_choice(
        PROVIDER_DEEPSEEK,
        api_key="secret",
        api_base="https://api.deepseek.com",
        model="deepseek-flash",
    )
    assert profile is not None
    assert profile.validate() == ()
    assert profile.model_id == "commercial_openai/deepseek-flash"


def test_later_wizard_choice_does_not_require_api_key() -> None:
    assert (
        provider_profile_for_choice(
            PROVIDER_LATER,
            api_key="",
            api_base="",
            model="",
        )
        is None
    )
