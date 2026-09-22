import ast
import asyncio
from datetime import datetime, timezone
import importlib
import ipaddress
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from urllib.parse import urlsplit


ROOT = Path(__file__).parents[1]
PROJECT = Path("E:/Project")


def _copy_runtime_package(tmp_path: Path):
    package = tmp_path / "jiang_schedule_runtime"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    for name in ("content.py", "unified_runtime.py"):
        shutil.copy2(ROOT / "runtime/jiang_schedule" / name, package / name)
    shutil.copy2(ROOT / "src/wechatrebot_manager/task_store.py", package / "task_store.py")
    sys.path.insert(0, str(tmp_path))
    try:
        return importlib.import_module("jiang_schedule_runtime.unified_runtime")
    finally:
        sys.path.remove(str(tmp_path))


class FakeContent:
    def __init__(self):
        self.generations = 0

    def readiness(self, _action):
        return {"ready": True, "message": ""}

    def require_ready(self, _action):
        return None

    async def generate(self, _task):
        self.generations += 1

        class Result:
            def segments(self):
                return [{"type": "text", "data": {"text": "提醒"}}]

        return Result()


class FakeClient:
    def __init__(self, *, group_exists=True):
        self.group_exists = group_exists

    async def get_group_info(self, group_id, self_id):
        return {"group_id": group_id} if self.group_exists and self_id == "wxid_robot" else {}

    async def get_user_info(self, user_id, self_id):
        return {"user_id": user_id} if self_id == "wxid_robot" else {}


def _task():
    return {
        "id": "runtime-task", "account_id": "wxid_robot", "name": "提醒",
        "action": "text", "arguments": {"text": "提醒"},
        "targets": [{"type": "group", "id": "123456@chatroom"}],
        "schedule": {"kind": "daily", "time": "08:30", "timezone": "Asia/Shanghai"},
        "enabled": True, "draft": False,
    }


def test_runtime_health_exposes_command_scheduling_contract(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    client = FakeClient()

    async def connection():
        return "wxid_robot", client

    runtime = module.UnifiedRuntime(
        object(), install_root=tmp_path, content=FakeContent(),
        connection=connection,
    )
    runtime._active = True

    health = asyncio.run(runtime.health())

    assert health["runtime_ready"] is True
    assert health["command_definition_version"] == 1
    catalog = health["command_catalog"]
    assert any(item["id"] == "weather" and item["status"] == "supported" for item in catalog)
    checkin = next(item for item in catalog if item["id"] == "checkin")
    assert checkin["status"] == "needs_condition"
    assert checkin["external_write"] is True


def test_runtime_generates_once_and_submits_only_live_recipient(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    client = FakeClient()
    content = FakeContent()
    sent = []

    async def connection():
        return "wxid_robot", client

    async def sender(_client, run, target, generated):
        sent.append((run["id"], target["id"], generated.segments()))
        return {"message_id": "wechat-1"}

    runtime = module.UnifiedRuntime(object(), install_root=tmp_path, content=content,
                                    connection=connection, sender=sender, clock=lambda: now)
    saved = runtime.store.save(_task(), now=now)
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])

    asyncio.run(runtime.run_once())

    assert content.generations == 1
    assert len(sent) == 1
    runs = runtime.store.runs("wxid_robot", "runtime-task")
    assert runs[0]["status"] == "submitted"
    assert runs[0]["deliveries"][0]["status"] == "submitted"


def test_runtime_skips_recipient_removed_from_live_directory(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    client = FakeClient(group_exists=False)
    sent = []

    async def connection():
        return "wxid_robot", client

    async def sender(*args):
        sent.append(args)
        return {"message_id": "unexpected"}

    runtime = module.UnifiedRuntime(object(), install_root=tmp_path, content=FakeContent(),
                                    connection=connection, sender=sender, clock=lambda: now)
    saved = runtime.store.save(_task(), now=now)
    runtime.clock = lambda: datetime.fromisoformat(saved["next_run_time"])

    asyncio.run(runtime.run_once())

    assert sent == []
    delivery = runtime.store.runs("wxid_robot", "runtime-task")[0]["deliveries"][0]
    assert delivery["status"] == "skipped"
    assert "当前账号目录" in delivery["message"]


def test_explicit_test_send_uses_temporary_authorized_run_and_leaves_no_task(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    now = datetime(2026, 9, 14, 0, 0, tzinfo=timezone.utc)
    client = FakeClient()
    sent = []

    async def connection():
        return "wxid_robot", client

    async def sender(_client, run, target, _content):
        assert run["task_id"].startswith("test_")
        sent.append(target["id"])
        return {"message_id": "test-1"}

    runtime = module.UnifiedRuntime(object(), install_root=tmp_path, content=FakeContent(),
                                    connection=connection, sender=sender, clock=lambda: now)
    runtime._active = True
    request = {**_task(), "test_target": {"type": "group", "id": "123456@chatroom"}}

    result = asyncio.run(runtime.test(request))

    assert result["status"] == "submitted"
    assert sent == ["123456@chatroom"]
    assert runtime.store.list("wxid_robot") == []


def test_short_drama_adapter_returns_validated_clickable_card(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)

    class Drama:
        async def build_unified_recommendation(self, media_type, limit):
            assert (media_type, limit) == ("短剧", 12)
            return {
                "type": "link_card",
                "title": "最新短剧推荐｜12部",
                "description": "定时推荐 · 点击查看封面并选择播放",
                "url": "https://player.example/short-drama/player?s=abcdefghijklmnop",
                "image_url": "https://player.example/cover.jpg",
                "fallback_text": "短剧甲\n短剧乙",
            }

    (tmp_path / "app/astrbot/data/config").mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        '{"plugin_set":["jiang_short_drama"]}', encoding="utf-8"
    )
    (tmp_path / "app/astrbot/data/config/jiang_short_drama_config.json").write_text(
        '{"stateless_player_url":"https://player.example/short-drama/player"}',
        encoding="utf-8",
    )
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_short_drama", activated=True, star_cls=Drama(),
    )])
    service = module.ContentService(context, tmp_path)

    content = asyncio.run(service.generate({
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 12},
    }))

    assert content.preview()["kind"] == "link_card"
    assert content.segments() == [{"type": "wechat_link_card", "data": {
        "title": "最新短剧推荐｜12部",
        "desc": "定时推荐 · 点击查看封面并选择播放",
        "url": "https://player.example/short-drama/player?s=abcdefghijklmnop",
        "image_url": "https://player.example/cover.jpg",
    }}]


def test_short_drama_adapter_downgrades_untrusted_player_url_to_text(
    tmp_path: Path,
) -> None:
    module = _copy_runtime_package(tmp_path)

    class Drama:
        async def build_unified_recommendation(self, _media_type, _limit):
            return {
                "type": "link_card",
                "title": "最新短剧推荐｜2部",
                "description": "点击播放",
                "url": "https://untrusted.example/watch?id=1",
                "image_url": "https://images.example/cover.jpg",
                "fallback_text": "短剧甲\n短剧乙",
            }

    (tmp_path / "app/astrbot/data/config").mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        '{"plugin_set":["jiang_short_drama"]}', encoding="utf-8"
    )
    (tmp_path / "app/astrbot/data/config/jiang_short_drama_config.json").write_text(
        '{"stateless_player_url":"https://player.example/short-drama/player"}',
        encoding="utf-8",
    )
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_short_drama", activated=True, star_cls=Drama(),
    )])

    content = asyncio.run(module.ContentService(context, tmp_path).generate({
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 2},
    }))

    assert content.preview() == {"kind": "text", "type": "text", "text": "短剧甲\n短剧乙"}


def test_hosted_player_url_validation_is_exact_and_public(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)
    content_module = sys.modules[module.ContentService.__module__]
    validate = content_module._hosted_player_url
    base = "https://player.example/short-drama/player"

    assert validate(f"{base}?s=abcdefghijklmnop", base)
    assert validate(f"{base}#r=abc_DEF-123", base)
    assert validate(f"{base}#p=abc_DEF-123&ep=1&force=0", base)

    for candidate in (
        "http://player.example/short-drama/player?s=abcdefghijklmnop",
        "https://untrusted.example/short-drama/player?s=abcdefghijklmnop",
        "https://user@player.example/short-drama/player?s=abcdefghijklmnop",
        "https://player.example:444/short-drama/player?s=abcdefghijklmnop",
        "https://player.example/other?s=abcdefghijklmnop",
        base,
        f"{base}?url=https%3A%2F%2Fmedia.example%2Fvideo.m3u8",
        f"{base}?s=abcdefghijklmnop#r=abc",
        f"{base}#p=abc&force=0&ep=1",
        f"{base}#p=abc&ep=0&force=0",
        f"{base}#p=abc&ep=1&force=2",
        f"{base}#r={'a' * 1801}",
        f"{base}#p={'a' * 1801}&ep=1&force=0",
        f"{base}#r=abc\nxyz",
    ):
        assert not validate(candidate, base), candidate

    for private_base in (
        "https://localhost/short-drama/player",
        "https://player.local/short-drama/player",
        "https://127.0.0.1/short-drama/player",
        "https://192.168.1.8/short-drama/player",
        "https://[::1]/short-drama/player",
    ):
        assert not validate(f"{private_base}?s=abcdefghijklmnop", private_base)


def test_short_drama_does_not_depend_on_commands_configuration(tmp_path: Path) -> None:
    module = _copy_runtime_package(tmp_path)

    class Drama:
        async def build_unified_recommendation(self, _media_type, _limit):
            return {"type": "text", "text": "短剧甲"}

    (tmp_path / "app/astrbot/data/config").mkdir(parents=True)
    (tmp_path / "app/astrbot/data/cmd_config.json").write_text(
        '{"plugin_set":["jiang_short_drama"]}', encoding="utf-8"
    )
    (tmp_path / "app/astrbot/data/config/jiang_short_drama_config.json").write_text(
        '{"stateless_player_url":"https://player.example/short-drama/player"}',
        encoding="utf-8",
    )
    (tmp_path / "app/astrbot/data/config/jiang_commands_config.json").write_text(
        "not-json", encoding="utf-8"
    )
    context = SimpleNamespace(get_all_stars=lambda: [SimpleNamespace(
        root_dir_name="jiang_short_drama", activated=True, star_cls=Drama(),
    )])

    content = asyncio.run(module.ContentService(context, tmp_path).generate({
        "action": "short_drama",
        "arguments": {"media_type": "短剧", "limit": 1},
    }))

    assert content.preview()["text"] == "短剧甲"


def test_runtime_plugin_patches_apply_to_upstream_copy(tmp_path: Path) -> None:
    if not (PROJECT / "data/plugins/jiang_schedule/main.py").is_file():
        return
    checkout = tmp_path / "schedule-plugin"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(PROJECT / "data/plugins/jiang_schedule/main.py", checkout / "main.py")
    for patch in (
        ROOT / "patches/astrbot-plugin-jiang_schedule.patch",
        ROOT / "patches/astrbot-plugin-jiang_schedule--permissions.patch",
        ROOT / "patches/astrbot-plugin-jiang_schedule--unified.patch",
    ):
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    for name in ("content.py", "unified_runtime.py"):
        shutil.copy2(ROOT / "runtime/jiang_schedule" / name, checkout / name)
    shutil.copy2(ROOT / "src/wechatrebot_manager/task_store.py", checkout / "task_store.py")
    source = (checkout / "main.py").read_text(encoding="utf-8")
    compile(source, str(checkout / "main.py"), "exec")
    assert "UnifiedRuntime" in source


def test_manual_weather_patch_reuses_the_scheduled_card_renderer(tmp_path: Path) -> None:
    source = PROJECT / "data/plugins/jiang_commands/main.py"
    if not source.is_file():
        return
    checkout = tmp_path / "commands-plugin"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(source, checkout / "main.py")
    patch = ROOT / "patches/astrbot-plugin-jiang_commands--scheduled-weather.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    patched = (checkout / "main.py").read_text(encoding="utf-8")
    compile(patched, str(checkout / "main.py"), "exec")
    assert "generate_weather" in patched
    assert "render_weather_target_image = getattr(" in patched
    assert '"render_weather_target_image", render_weather_image' in patched


def test_short_drama_patch_exposes_unified_content_adapter(tmp_path: Path) -> None:
    source = PROJECT / "data/plugins/jiang_short_drama/main.py"
    if not source.is_file():
        return
    checkout = tmp_path / "short-drama-plugin"
    checkout.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=checkout, check=True)
    shutil.copy2(source, checkout / "main.py")
    for patch in (
        ROOT / "patches/astrbot-plugin-jiang_short_drama.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--unified.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--zz-hosted-player.patch",
        ROOT / "patches/astrbot-plugin-jiang_short_drama--zzz-all-card-links.patch",
    ):
        subprocess.run(["git", "apply", "--check", str(patch)], cwd=checkout, check=True)
        subprocess.run(["git", "apply", str(patch)], cwd=checkout, check=True)
    patched = (checkout / "main.py").read_text(encoding="utf-8")
    compile(patched, str(checkout / "main.py"), "exec")
    assert "build_unified_recommendation" in patched
    assert "await self.player_server.start()" not in patched
    assert "build_watch_url(" not in patched
    assert "self.player_server.create_" not in patched
    assert "candidate.hostname == base.hostname" in patched

    tree = ast.parse(patched)
    main_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "Main"
    )
    hosted_method = next(
        node for node in main_class.body
        if isinstance(node, ast.FunctionDef) and node.name == "_hosted_player_url"
    )
    method_source = ast.get_source_segment(patched, hosted_method)
    assert method_source is not None
    namespace = {"ipaddress": ipaddress, "re": re, "urlsplit": urlsplit}
    exec(
        "class Harness:\n" + textwrap.indent(textwrap.dedent(method_source), "    "),
        namespace,
    )
    harness = namespace["Harness"]()
    harness.config = {
        "stateless_player_url": "https://player.example/short-drama/player"
    }
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player?s=abcdefghijklmnop"
    )
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player#p=abc&ep=1&force=0"
    )
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player?url=https://evil.example/x.m3u8"
    ) is None
    assert harness._hosted_player_url(
        "https://untrusted.example/short-drama/player?s=abcdefghijklmnop"
    ) is None
    assert harness._hosted_player_url(
        "https://player.example/short-drama/player#p="
        + ("a" * 1801)
        + "&ep=1&force=0"
    ) is None
    harness.config["stateless_player_url"] = "https://127.0.0.1/player"
    assert harness._hosted_player_url(
        "https://127.0.0.1/player?s=abcdefghijklmnop"
    ) is None

    cover_function = next(
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_safe_card_image_url"
    )
    cover_source = ast.get_source_segment(patched, cover_function)
    assert cover_source is not None
    cover_namespace = {
        "_DEFAULT_COVER": "https://player.example/default.ico",
        "ipaddress": ipaddress,
        "re": re,
        "urlsplit": urlsplit,
    }
    exec(textwrap.dedent(cover_source), cover_namespace)
    safe_cover = cover_namespace["_safe_card_image_url"]
    assert safe_cover("https://images.example/cover.jpg") == (
        "https://images.example/cover.jpg"
    )
    assert safe_cover("http://images.example/cover.jpg") == cover_namespace["_DEFAULT_COVER"]
    assert safe_cover("https://127.0.0.1/cover.jpg") == cover_namespace["_DEFAULT_COVER"]
