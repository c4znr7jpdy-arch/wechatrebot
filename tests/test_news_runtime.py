import asyncio
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace


ROOT = Path(__file__).parents[1]


def _load_news_module():
    logger = SimpleNamespace(warning=lambda *args, **kwargs: None)
    previous = sys.modules.get("nonebot")
    sys.modules["nonebot"] = SimpleNamespace(logger=logger)
    try:
        spec = importlib.util.spec_from_file_location(
            "commercial_news", ROOT / "runtime/ai_plugin/news.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module
    finally:
        if previous is None:
            sys.modules.pop("nonebot", None)
        else:
            sys.modules["nonebot"] = previous


def test_public_news_payload_is_normalized() -> None:
    module = _load_news_module()
    items = module._normalize_toutiao_items(
        {"data": [{"Title": "热点一", "HotValue": "42", "Url": "https://example.test/1"},
                   {"Title": "", "HotValue": 3}]},
        10,
    )
    assert items == [{
        "title": "热点一", "hot_value": 42, "url": "https://example.test/1",
    }]


def test_news_uses_public_endpoint_without_hidden_credentials(monkeypatch) -> None:
    module = _load_news_module()
    calls = []

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"data": [{"Title": "公开热榜", "HotValue": "8"}]}

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()

    monkeypatch.delenv("JIANG_CONTENT_API_ID", raising=False)
    monkeypatch.delenv("JIANG_CONTENT_API_KEY", raising=False)
    monkeypatch.setattr(module.httpx, "AsyncClient", Client)

    assert asyncio.run(module.fetch_hot_news(3)) == [
        {"title": "公开热榜", "hot_value": 8, "url": ""}
    ]
    assert calls[0][0] == module._TOUTIAO_HOT_BOARD_URL


def test_news_schedule_readiness_does_not_require_hidden_credentials(tmp_path: Path) -> None:
    package = tmp_path / "runtime"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "content.py").write_text(
        (ROOT / "runtime/jiang_schedule/content.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (package / "task_store.py").write_text(
        (ROOT / "src/wechatrebot_manager/task_store.py").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    try:
        module = __import__("runtime.content", fromlist=["ContentService"])
    finally:
        sys.path.remove(str(tmp_path))

    config_root = tmp_path / "app/astrbot/data/config"
    config_root.mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        json.dumps({"plugin_set": ["jiang_commands"]}), encoding="utf-8"
    )
    (config_root / "jiang_commands_config.json").write_text("{}", encoding="utf-8")
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_commands", activated=True, star_cls=object(),
    )])
    assert module.ContentService(context, tmp_path).readiness("news") == {
        "ready": True, "message": "",
    }


def test_news_runtime_is_overlaid_after_project_source() -> None:
    manifest = json.loads(
        (ROOT / "release/source-manifest.json").read_text(encoding="utf-8")
    )
    assert {
        (entry["origin"], entry["source"], entry["target"])
        for entry in manifest["entries"]
    } >= {
        ("commercial", "runtime/ai_plugin/news.py", "app/astrbot/ai_plugin/ai_plugin/news.py"),
    }
