import json
from pathlib import Path

import pytest

from wechatrebot_manager.error_catalog import AccountStatusUnavailableError, classify_error, load_error_catalog
from wechatrebot_manager.group_directory import GroupDirectoryUnavailable


def test_error_catalog_loads_default_and_customer_override(tmp_path: Path) -> None:
    config = tmp_path / "config"
    config.mkdir()
    (config / "error-codes.zh-CN.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "errors": {
                    "12002": {
                        "title": "定制连接提示",
                        "message": "请联系您的专属客服。",
                        "solutions": ["发送错误码"],
                        "retryable": False,
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    catalog = load_error_catalog(tmp_path)

    assert catalog.get("11001").title == "未检测到微信"
    assert catalog.get("12002").title == "定制连接提示"
    assert catalog.get("99999").code == "19000"


@pytest.mark.parametrize("error_type", [AccountStatusUnavailableError, GroupDirectoryUnavailable])
def test_unavailable_account_is_retryable_without_guessing_logout(error_type):
    code = classify_error(error_type("temporary"), "19000", "get_messages")
    definition = load_error_catalog().get(code)
    assert code == "11007"
    assert definition.retryable and definition.level == "warning"
    assert classify_error(OSError("微信账号状态暂时不可用"), "19000", "get_messages") == "19000"
