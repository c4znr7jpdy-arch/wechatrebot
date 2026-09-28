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


def test_douyin_payload_is_sorted_and_normalized():
    module = _load_news_module()
    assert module.normalize_items({"code": 200, "data": [
        {"title": "低", "hot_value": "2"}, {"title": "高", "hot_value": "42"},
        {"title": ""}, None]}, 1) == [{"title": "高", "hot_value": 42}]


def test_news_uses_douyin_credentials_and_never_falls_back(monkeypatch):
    module = _load_news_module()
    calls = []
    class Response:
        def raise_for_status(self): pass
        def json(self): return {"code": 200, "data": [{"title": "抖音新闻", "hot_value": 8}]}
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response()
    monkeypatch.setenv("JIANG_CONTENT_API_ID", "test-id")
    monkeypatch.setenv("JIANG_CONTENT_API_KEY", "test-key")
    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    result = asyncio.run(module.fetch_hot_news(3))
    assert result == [{"title": "抖音新闻", "hot_value": 8}]
    assert calls == [(module.DOUYIN_API_URL, {"params": {"id": "test-id", "key": "test-key"}})]
    assert "抖音热榜" in module.format_news(result)
    monkeypatch.delenv("JIANG_CONTENT_API_KEY")
    import pytest
    with pytest.raises(RuntimeError, match="尚未配置"):
        asyncio.run(module.fetch_hot_news())
    assert len(calls) == 1


def test_news_failure_does_not_expose_credentials(monkeypatch):
    module = _load_news_module()
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *_args): pass
        async def get(self, *args, **kwargs): raise ValueError("secret-test-key")
    logs = []
    monkeypatch.setenv("JIANG_CONTENT_API_ID", "id")
    monkeypatch.setenv("JIANG_CONTENT_API_KEY", "secret-test-key")
    monkeypatch.setattr(module.httpx, "AsyncClient", Client)
    monkeypatch.setattr(module.logger, "warning", lambda *args: logs.append(str(args)))
    import pytest
    with pytest.raises(RuntimeError, match="暂时不可用") as exc:
        asyncio.run(module.fetch_hot_news())
    assert "secret-test-key" not in str(exc.value) + str(logs)


def test_news_schedule_uses_same_credentials_and_adapter(tmp_path: Path) -> None:
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
    service = module.ContentService(context, tmp_path)
    assert service.readiness("news")["ready"] is False
    (config_root / "jiang_commands_config.json").write_text(
        json.dumps({"content_api_id": "test-id", "content_api_key": "test-key"}), encoding="utf-8")
    assert service.readiness("news") == {"ready": True, "message": ""}
    news = _load_news_module()
    async def fetch(count):
        assert count == 5
        return [{"title": "定时抖音热榜"}]
    news.fetch_hot_news = fetch
    previous = sys.modules.get("ai_plugin.news")
    previous_package = sys.modules.get("ai_plugin")
    sys.modules["ai_plugin"] = SimpleNamespace(news=news)
    sys.modules["ai_plugin.news"] = news
    try:
        result = asyncio.run(service.generate({"action": "news", "arguments": {"count": 5}}))
        assert "抖音热榜" in result.text and "定时抖音热榜" in result.text
    finally:
        for key, old in (("ai_plugin.news", previous), ("ai_plugin", previous_package)):
            if old is None: sys.modules.pop(key, None)
            else: sys.modules[key] = old


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
