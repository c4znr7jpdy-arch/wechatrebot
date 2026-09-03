from pathlib import Path

from wechatrebot_manager.startup_errors import (
    customer_startup_error_message,
    record_startup_error,
)


def test_missing_payload_error_explains_that_the_full_directory_is_required() -> None:
    message = customer_startup_error_message(
        FileNotFoundError("未找到与安装程序配套的 payload.wrb。")
    )
    assert "不要单独复制" in message
    assert "DISTRIBUTION-MANIFEST.json" in message


def test_startup_error_is_recorded_without_internal_engine_brand(tmp_path: Path) -> None:
    target = record_startup_error(
        RuntimeError("AstrBot failed with token=private-value"),
        local_app_data=tmp_path,
    )
    assert target is not None
    text = target.read_text(encoding="utf-8")
    assert "AstrBot" not in text
    assert "回复引擎" in text
    assert "private-value" not in text
