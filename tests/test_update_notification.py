from __future__ import annotations
from webui_source import manager_script

import json
from pathlib import Path


ROOT = Path(__file__).parents[1]


def test_update_popup_describes_release_and_offers_later_action() -> None:
    script = manager_script()

    assert 'cancelText: mandatory ? "稍后更新" : "稍后提醒"' in script
    assert 'intent: "primary"' in script
    assert "formatBytes(release.package_size)" in script
    assert "release.notes.join" in script
    assert "更新会强制关闭机器人程序及当前连接的微信，是否确认？" in script
    assert 'confirmText: closing ? "再次关闭管理器" : "确认关闭并安装"' in script
    assert "scheduleAutomaticUpdateCheck(1800)" in script
    assert "configured : 600" in script


def test_update_catalog_has_explicit_running_robot_prerequisite() -> None:
    catalog = json.loads(
        (ROOT / "src" / "wechatrebot_manager" / "error-codes.zh-CN.json").read_text(
            encoding="utf-8"
        )
    )

    error = catalog["errors"]["14004"]
    assert error["title"] == "请先停止机器人"
    assert "停止机器人" in error["message"]
