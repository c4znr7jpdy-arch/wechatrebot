import json
from pathlib import Path

from wechatrebot_manager.error_catalog import load_error_catalog


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
