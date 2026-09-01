import json
from pathlib import Path

from wechatrebot_manager.brand import BrandProfile, save_brand_profile


def test_brand_profile_is_normalized_and_saved_atomically(tmp_path: Path) -> None:
    target = tmp_path / "config" / "brand.json"
    save_brand_profile(
        target,
        BrandProfile(
            display_name="  小管家  ",
            short_name=" 管家 ",
            persona_name=" 小管家 ",
        ),
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["display_name"] == "小管家"
    assert payload["welcome_text"] == "你好，我是小管家。"
    assert payload["help_title"] == "小管家帮助"


def test_missing_avatar_is_rejected(tmp_path: Path) -> None:
    profile = BrandProfile(
        display_name="小管家",
        short_name="管家",
        persona_name="小管家",
        avatar_path=str(tmp_path / "missing.png"),
    )
    assert "头像文件不存在" in " ".join(profile.validate())

