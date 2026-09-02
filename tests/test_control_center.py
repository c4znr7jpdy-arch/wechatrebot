import json
from pathlib import Path

from wechatrebot_manager.brand import BrandProfile, save_brand_profile
from wechatrebot_manager.control_center import apply_brand_change
from wechatrebot_manager.runtime_config import write_runtime_configs


def test_brand_change_updates_profile_and_astrbot_persona(tmp_path: Path) -> None:
    install = tmp_path / "install"
    wechat = tmp_path / "Weixin.exe"
    wechat.write_bytes(b"placeholder")
    original = BrandProfile("旧名称", "旧名", "旧人格").normalized()
    save_brand_profile(install / "config" / "brand.json", original)
    write_runtime_configs(install, wechat, brand_profile=original)

    updated = BrandProfile("新名称", "新名", "新人格").normalized()
    apply_brand_change(install, updated)

    brand = json.loads(
        (install / "config" / "brand.json").read_text(encoding="utf-8")
    )
    astrbot = json.loads(
        (install / "app" / "astrbot" / "data" / "cmd_config.json").read_text(
            encoding="utf-8"
        )
    )
    assert brand["display_name"] == "新名称"
    assert "新名称" in astrbot["persona"][0]["prompt"]
    assert astrbot["provider_settings"]["default_personality"] == "commercial_brand"
