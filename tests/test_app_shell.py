from pathlib import Path
from types import SimpleNamespace
import json
import sys
import threading

from wechatrebot_manager import app_shell
from wechatrebot_manager.error_catalog import load_error_catalog
from wechatrebot_manager.payload import PayloadError
from wechatrebot_manager.updates import UpdateChannel, UpdateRelease


class _Events:
    def __init__(self) -> None:
        self.items = []

    def emit(self, *args, **kwargs) -> None:
        self.items.append((args, kwargs))


def _api(tmp_path: Path) -> app_shell.ApplicationApi:
    value = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    value._install_root = tmp_path
    value._catalog = load_error_catalog()
    value._update_lock = threading.Lock()
    value._update_status_lock = threading.Lock()
    value._update_status = {
        "state": "idle",
        "phase": "idle",
        "completed": 0,
        "total": 0,
        "percent": 0,
        "message": "",
        "error": None,
    }
    value._available_update = None
    value._update_result = None
    value._update_channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    value._lifecycle = SimpleNamespace(events=_Events())
    value._window = None
    value._tray = None
    return value


def test_unified_scheduled_task_api_is_explicit_and_keeps_user_errors(tmp_path: Path) -> None:
    api = _api(tmp_path)
    calls = []

    class Service:
        def context(self):
            return {"account_id": "wxid_robot", "items": []}

        def save(self, payload):
            calls.append(payload)
            if not payload.get("name"):
                raise ValueError("请填写任务名称。")
            return {"item": payload}

    api._scheduled_tasks = Service()
    required = {
        "get_scheduled_task_context", "save_scheduled_task",
        "set_scheduled_task_enabled", "delete_scheduled_task",
        "preview_scheduled_task", "get_scheduled_task_runs",
        "test_scheduled_task",
        "parse_scheduled_command", "preview_scheduled_times",
    }

    assert required <= set(app_shell.PUBLIC_API_METHODS)
    assert api.get_scheduled_task_context()["data"]["items"] == []
    assert api.save_scheduled_task({"name": "提醒"})["ok"] is True
    failure = api.save_scheduled_task({"name": ""})
    assert failure == {
        "ok": False,
        "error": {"title": "任务尚未完成配置", "message": "请填写任务名称。"},
    }
    parsed = api.parse_scheduled_command("青岛天气 明天")
    assert parsed["data"]["arguments"]["output"] == "image"


def test_policy_errors_expose_user_validation_but_not_raw_io_failures(tmp_path):
    api = _api(tmp_path)
    for error, code in [
        (app_shell.GroupPolicyError('安静时段的时区不可用。'), '15001'),
        (app_shell.PrivatePolicyError('联系人规则已在其他窗口修改，请刷新后重试。'), '15002'),
    ]:
        result = api._failed(error, code)
        assert result['error']['code'] == code
        assert result['error']['detail'] == str(error)
    result = api._failed(PermissionError('secret filesystem path and credential'), '15001')
    assert 'detail' not in result['error']
    assert 'secret' not in str(result['error'])


def test_bootstrap_exposes_update_check_interval(monkeypatch, tmp_path: Path) -> None:
    api = _api(tmp_path)
    api._lifecycle.refresh = lambda: SimpleNamespace(to_dict=lambda: {"state": "STOPPED"})
    monkeypatch.setattr(
        app_shell,
        "load_robot_settings",
        lambda _root: SimpleNamespace(
            profile=SimpleNamespace(to_dict=lambda: {"display_name": "测试机器人"}),
            admin_id="wxid_admin",
        ),
    )
    monkeypatch.setattr(app_shell, "load_provider_profile", lambda _root: None)

    result = api.bootstrap()

    assert result["ok"] is True
    assert result["data"]["update"]["enabled"] is True
    assert result["data"]["update"]["check_interval_seconds"] == 21_600
    assert result["data"]["provider"] == {
        "configured": False,
        "api_base": "https://api.xiuxianjyj.xin/v1",
        "model": "grok-4.6",
        "api_key_configured": False,
    }


def test_first_provider_save_uses_jyjapi_grok_source(
    monkeypatch, tmp_path: Path
) -> None:
    api = _api(tmp_path)
    captured = {}
    monkeypatch.setattr(app_shell, "load_provider_profile", lambda _root: None)

    def fake_save(_root, profile, *, make_default=True):
        captured["profile"] = profile.normalized()
        captured["make_default"] = make_default
        return captured["profile"]

    monkeypatch.setattr(app_shell, "save_provider_profile", fake_save)

    result = api.save_provider(
        {
            "api_key": "customer-secret",
            "api_base": "https://api.xiuxianjyj.xin/v1",
            "model": "grok-4.6",
        }
    )

    assert result["ok"] is True
    assert captured["profile"].source_id == "jyjapi"
    assert captured["profile"].model_id == "jyjapi/grok-4.6"
    assert captured["make_default"] is True


def test_new_provider_keeps_the_existing_reply_model(
    monkeypatch, tmp_path: Path
) -> None:
    api = _api(tmp_path)
    captured = {}
    existing = SimpleNamespace(
        provider_id="jyjapi/grok-4.6",
        is_default=True,
    )
    monkeypatch.setattr(
        app_shell, "load_provider_services", lambda _root: (existing,)
    )
    monkeypatch.setattr(app_shell, "load_provider_profile", lambda _root: None)

    class AvailableClient:
        @staticmethod
        def is_available():
            return True

        @staticmethod
        def save_provider(profile, **kwargs):
            captured["profile"] = profile.normalized()
            captured.update(kwargs)

    monkeypatch.setattr(app_shell, "EngineApiClient", lambda _root: AvailableClient())

    result = api.save_provider(
        {
            "api_key": "second-secret",
            "api_base": "https://api.deepseek.com/v1",
            "model": "deepseek-v4-flash",
        }
    )

    assert result["ok"] is True
    assert result["data"]["is_default"] is False
    assert result["data"]["apply_state"] == "applied"
    assert captured["make_default"] is False
    assert captured["original_provider_id"] is None
    assert "当前回复模型保持不变" in result["message"]


def test_switching_an_existing_service_preset_uses_the_new_source(
    monkeypatch, tmp_path: Path
) -> None:
    api = _api(tmp_path)
    captured = {}
    selected = SimpleNamespace(
        provider_id="deepseek/deepseek-v4-flash",
        source_id="deepseek",
        api_base="https://api.deepseek.com/v1",
        model="deepseek-v4-flash",
        api_key="developer-key",
        is_default=False,
    )
    monkeypatch.setattr(app_shell, "load_provider_services", lambda _root: (selected,))
    monkeypatch.setattr(app_shell, "load_provider_profile", lambda _root: None)
    monkeypatch.setattr(
        app_shell,
        "EngineApiClient",
        lambda _root: SimpleNamespace(is_available=lambda: False),
    )

    def fake_save(_root, profile, *, make_default=True):
        captured["profile"] = profile.normalized()
        captured["make_default"] = make_default
        return captured["profile"]

    monkeypatch.setattr(app_shell, "save_provider_profile", fake_save)

    result = api.save_provider(
        {
            "provider_id": selected.provider_id,
            "api_key": "customer-secret",
            "api_base": "https://api.xiuxianjyj.xin/v1",
            "model": "grok-4.6",
        }
    )

    assert result["ok"] is True
    assert captured["profile"].source_id == "jyjapi"
    assert captured["make_default"] is False


def test_model_discovery_reuses_saved_key_without_exposing_it(
    monkeypatch, tmp_path: Path
) -> None:
    api = _api(tmp_path)
    selected = SimpleNamespace(
        provider_id="deepseek/deepseek-chat",
        api_key="saved-secret",
    )
    captured = {}
    monkeypatch.setattr(app_shell, "load_provider_services", lambda _root: (selected,))

    def fake_discover(**values):
        captured.update(values)
        return ("deepseek-chat", "deepseek-reasoner")

    monkeypatch.setattr(app_shell, "discover_openai_models", fake_discover)

    result = api.discover_provider_models(
        {
            "provider_id": selected.provider_id,
            "api_base": "https://api.deepseek.com/v1",
            "api_key": "",
        }
    )

    assert result["ok"] is True
    assert result["data"]["items"] == ["deepseek-chat", "deepseek-reasoner"]
    assert captured["api_key"] == "saved-secret"


def test_deleting_current_provider_requires_and_applies_replacement(
    monkeypatch, tmp_path: Path
) -> None:
    api = _api(tmp_path)
    current = SimpleNamespace(
        provider_id="jyjapi/grok-4.6",
        source_id="jyjapi",
        configured=True,
        is_default=True,
    )
    replacement = SimpleNamespace(
        provider_id="deepseek/deepseek-chat",
        source_id="deepseek",
        configured=True,
        is_default=False,
    )
    calls = []
    monkeypatch.setattr(
        app_shell, "load_provider_services", lambda _root: (current, replacement)
    )
    monkeypatch.setattr(
        app_shell,
        "EngineApiClient",
        lambda _root: SimpleNamespace(is_available=lambda: False),
    )
    monkeypatch.setattr(
        app_shell,
        "set_default_provider",
        lambda _root, provider_id: calls.append(("default", provider_id)),
    )
    monkeypatch.setattr(
        app_shell,
        "delete_provider_profile",
        lambda _root, provider_id: calls.append(("delete", provider_id)),
    )

    rejected = api.delete_provider(current.provider_id)
    assert rejected["ok"] is False

    result = api.delete_provider(current.provider_id, replacement.provider_id)
    assert result["ok"] is True
    assert calls == [
        ("default", replacement.provider_id),
        ("delete", current.provider_id),
    ]


def test_deleting_referenced_provider_hot_migrates_feature_config(
    monkeypatch, tmp_path: Path
) -> None:
    api = _api(tmp_path)
    current = SimpleNamespace(
        provider_id="jyjapi/grok-4.6",
        source_id="jyjapi",
        configured=True,
        is_default=False,
    )
    replacement = SimpleNamespace(
        provider_id="deepseek/deepseek-chat",
        source_id="deepseek",
        configured=True,
        is_default=True,
    )
    calls = []

    class FakeClient:
        @staticmethod
        def is_available() -> bool:
            return True

        @staticmethod
        def get_config(module_name: str):
            calls.append(("read", module_name))
            return {
                "config": {
                    "provider_id": current.provider_id,
                    "nested": [current.provider_id, "unchanged"],
                },
                "revision": "before",
            }

        @staticmethod
        def save_config(module_name: str, config, base_revision=None):
            calls.append(("save", module_name, config, base_revision))
            return {"revision": "after"}

        @staticmethod
        def set_default_provider(_provider_id: str) -> None:
            raise AssertionError("非默认服务不应切换机器人回复模型")

        @staticmethod
        def delete_provider(provider_id: str, source_id: str) -> None:
            calls.append(("delete", provider_id, source_id))

    monkeypatch.setattr(
        app_shell, "load_provider_services", lambda _root: (current, replacement)
    )
    monkeypatch.setattr(
        app_shell,
        "provider_reference_modules",
        lambda _root, _provider_id, strict=False: ("astrbot_plugin_livingmemory",),
    )
    monkeypatch.setattr(app_shell, "EngineApiClient", lambda _root: FakeClient())

    rejected = api.delete_provider(current.provider_id)
    assert rejected["ok"] is False

    result = api.delete_provider(current.provider_id, replacement.provider_id)
    assert result["ok"] is True
    assert result["data"]["updated_features"] == ["astrbot_plugin_livingmemory"]
    assert calls == [
        ("read", "astrbot_plugin_livingmemory"),
        (
            "save",
            "astrbot_plugin_livingmemory",
            {
                "provider_id": replacement.provider_id,
                "nested": [replacement.provider_id, "unchanged"],
            },
            "before",
        ),
        ("delete", current.provider_id, current.source_id),
    ]


def test_conversations_merge_current_group_directory_including_empty_groups(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    api._history = SimpleNamespace(
        conversations=lambda **_filters: {
            "items": [
                {
                    "conversation_id": "active@chatroom",
                    "type": "group",
                    "display_name": "旧群名",
                    "message_count": 2,
                    "last_timestamp": 100,
                    "avatar": "",
                }
            ],
            "summary": {"messages": 2},
        }
    )
    api._groups = SimpleNamespace(
        list=lambda _refresh: {
            "items": [
                {
                    "group_id": "active@chatroom",
                    "name": "现在的群名",
                    "avatar": "https://example.test/active.jpg",
                    "member_count": 3,
                },
                {
                    "group_id": "empty@chatroom",
                    "name": "还没有消息的群",
                    "avatar": "https://example.test/empty.jpg",
                    "member_count": 5,
                },
            ]
        }
    )

    result = api.get_conversations({"kind": "group"})

    assert result["ok"] is True
    items = result["data"]["items"]
    assert [item["conversation_id"] for item in items] == [
        "active@chatroom",
        "empty@chatroom",
    ]
    assert items[0]["display_name"] == "现在的群名"
    assert items[0]["avatar"] == "https://example.test/active.jpg"
    assert items[1]["message_count"] == 0
    assert items[1]["member_count"] == 5
    assert result["data"]["summary"]["directory_groups"] == 2


def test_conversation_search_matches_group_without_local_messages(tmp_path: Path) -> None:
    api = _api(tmp_path)
    api._history = SimpleNamespace(
        conversations=lambda **_filters: {"items": [], "summary": {"messages": 0}}
    )
    api._groups = SimpleNamespace(
        list=lambda _refresh: {
            "items": [
                {"group_id": "family@chatroom", "name": "爱人们"},
                {"group_id": "work@chatroom", "name": "工作群"},
            ]
        }
    )

    result = api.get_conversations({"kind": "all", "search": "爱人"})

    assert result["ok"] is True
    assert [item["conversation_id"] for item in result["data"]["items"]] == [
        "family@chatroom"
    ]


def test_dated_group_history_still_uses_directory_avatar_without_adding_empty_groups(
    tmp_path: Path,
) -> None:
    api = _api(tmp_path)
    api._history = SimpleNamespace(
        conversations=lambda **_filters: {
            "items": [{
                "conversation_id": "active@chatroom",
                "type": "group",
                "display_name": "旧群名",
                "message_count": 1,
                "last_timestamp": 100,
                "avatar": "",
            }],
            "summary": {"messages": 1},
        }
    )
    api._groups = SimpleNamespace(
        list=lambda _refresh: {
            "account_id": "wxid_bot",
            "items": [
                {"group_id": "active@chatroom", "name": "当前群名", "avatar": "https://example.test/group.jpg"},
                {"group_id": "empty@chatroom", "name": "空群", "avatar": "https://example.test/empty.jpg"},
            ],
        }
    )
    result = api.get_conversations({"kind": "group", "start": "2026-09-14", "end": "2026-09-15"})

    assert result["ok"] is True, result
    assert [item["conversation_id"] for item in result["data"]["items"]] == ["active@chatroom"]
    assert result["data"]["items"][0]["display_name"] == "当前群名"
    assert result["data"]["items"][0]["avatar"] == "https://example.test/group.jpg"


def test_check_update_returns_customer_safe_release(monkeypatch, tmp_path: Path) -> None:
    release = UpdateRelease(
        version="0.9.0",
        title="体验升级",
        notes=("聊天记录更易查找",),
        published_at="2026-09-03T00:00:00Z",
        package_url="https://updates.example.test/0.9.0.zip",
        package_size=1234,
        package_sha256="A" * 64,
        mandatory=False,
    )
    monkeypatch.setattr(app_shell, "check_for_update", lambda *_args, **_kwargs: release)
    api = _api(tmp_path)

    result = api.check_update()

    assert result["ok"] is True
    assert result["data"]["release"] == {
        "version": "0.9.0",
        "title": "体验升级",
        "notes": ["聊天记录更易查找"],
        "published_at": "2026-09-03T00:00:00Z",
        "package_size": 1234,
        "mandatory": False,
    }
    assert "package_url" not in result["data"]["release"]
    assert "package_sha256" not in result["data"]["release"]


def test_install_update_requires_stopped_robot(monkeypatch, tmp_path: Path) -> None:
    api = _api(tmp_path)
    api._lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="RUNNING"), online=True, can_stop=True
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)

    result = api.install_update("0.9.0")

    assert result["ok"] is False
    assert result["error"]["code"] == "14003"
    assert api._lifecycle.events.items[-1][0][:2] == ("APP_ERROR", "failed")


def test_install_update_runs_in_background_and_reports_progress(
    monkeypatch, tmp_path: Path
) -> None:
    release = UpdateRelease(
        version="0.9.0",
        title="体验升级",
        notes=(),
        published_at="2026-09-03T00:00:00Z",
        package_url="https://updates.example.test/0.9.0.zip",
        package_size=100,
        package_sha256="A" * 64,
        mandatory=False,
    )
    api = _api(tmp_path)
    api._available_update = release
    api._lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="STOPPED"), online=False, can_stop=False
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)
    launched = threading.Event()
    prepared = SimpleNamespace()

    def fake_prepare(*_args, progress_callback, **_kwargs):
        progress_callback("download", 50, 100)
        progress_callback("verify", 4, 4)
        return prepared

    monkeypatch.setattr(app_shell, "prepare_update", fake_prepare)
    monkeypatch.setattr(app_shell, "create_update_plan", lambda *_args: "plan")
    monkeypatch.setattr(app_shell, "launch_update_helper", lambda _plan: launched.set())

    result = api.install_update("0.9.0")

    assert result["ok"] is True
    assert result["data"]["accepted"] is True
    assert launched.wait(2)
    assert api._update_lock.acquire(timeout=2)
    api._update_lock.release()
    status = api.get_update_status()
    assert status["ok"] is True
    assert status["data"]["state"] == "restarting"
    assert status["data"]["percent"] == 100


def test_install_update_surfaces_background_verification_failure(
    monkeypatch, tmp_path: Path
) -> None:
    release = UpdateRelease(
        version="0.9.0",
        title="体验升级",
        notes=(),
        published_at="2026-09-03T00:00:00Z",
        package_url="https://updates.example.test/0.9.0.zip",
        package_size=100,
        package_sha256="A" * 64,
        mandatory=False,
    )
    api = _api(tmp_path)
    api._available_update = release
    api._lifecycle.refresh = lambda: SimpleNamespace(
        state=SimpleNamespace(value="STOPPED"), online=False, can_stop=False
    )
    monkeypatch.setattr(app_shell.sys, "frozen", True, raising=False)
    failed = threading.Event()

    def fake_prepare(*_args, **_kwargs):
        try:
            raise PayloadError("载荷校验失败")
        finally:
            failed.set()

    monkeypatch.setattr(app_shell, "prepare_update", fake_prepare)

    result = api.install_update("0.9.0")

    assert result["ok"] is True
    assert failed.wait(2)
    assert api._update_lock.acquire(timeout=2)
    api._update_lock.release()
    status = api.get_update_status()["data"]
    assert status["state"] == "failed"
    assert status["error"]["code"] == "14002"


def test_public_api_allowlist_matches_frontend_calls() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")
    called = {
        match.split('"', 2)[1]
        for match in __import__("re").findall(r'api\("[a-z_]+"', script)
    }

    assert called | {"start_robot", "stop_robot"} == set(
        app_shell.PUBLIC_API_METHODS
    )
    assert "_attach_window" not in app_shell.PUBLIC_API_METHODS


def test_cached_feature_switch_is_instant_and_model_discovery_keeps_selection() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")
    feature_loader = script[
        script.index("async function openFeature") : script.index(
            "function getAtPath"
        )
    ]
    model_discovery = script[
        script.index("async function discoverProviderModels") : script.index(
            "function deleteSelectedProvider"
        )
    ]

    assert "if (!force) return;" in feature_loader
    assert 'await api("get_feature", featureId)' in feature_loader
    assert "form.elements.model.value = items[0]" not in model_discovery


def test_feature_details_are_warmed_in_the_background() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert "async function prefetchFeatureDetails(generation)" in script
    assert "void prefetchFeatureDetails(++state.featurePrefetch);" in script
    assert "Math.min(3, pending.length)" in script


def test_feature_filters_show_all_then_common_and_default_to_all() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    audience = markup[markup.index('id="featureAudience"'):markup.index('</div>', markup.index('id="featureAudience"'))]

    assert 'featureAudience: "all"' in script
    assert audience.index('data-audience="all"') < audience.index('data-audience="common"')
    assert ">全部<" in audience and ">常用<" in audience
    assert 'data-audience="optional"' not in audience
    assert 'data-audience="advanced"' not in audience
    assert 'previewFeature("feature-parser", "链接解析", "none"' in script
    assert 'primary_api_key: {description: "首选生图 API Key"' in script
    assert 'advanced_schema: {}' in script
    assert '.filter(([key]) => Object.keys(schemas[key]).length)' in script


def test_feature_save_actions_do_not_shift_with_status_copy() -> None:
    styles = (
        Path(app_shell.__file__).with_name("app_webui") / "app.css"
    ).read_text(encoding="utf-8")

    assert ".feature-savebar #featureNote" in styles
    assert ".feature-save-actions { display: flex; align-items: center; justify-self: start;" in styles


def test_feature_center_refreshes_enabled_state_after_specialized_settings() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert "async function refreshVisibleFeatureState()" in script
    assert 'window.addEventListener("focus", () => { refreshVisibleFeatureState().catch(reportError); });' in script
    assert "const preserveTab = state.selectedFeature === featureId;" in script
    assert "useFeatureDetail(detail, afterSave, preserveTab);" in script


def test_response_trace_is_rendered_as_a_duration_scaled_gantt_chart() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert "机器人消息发送甘特图" in markup
    assert "stage.offset_seconds" in script
    assert "const numericDuration = (stage)" in script
    assert 'class="gantt-track"' in script
    assert 'class="gantt-duration"' in script
    assert "横条位置表示阶段发生时间，长度表示耗时" in script
    assert ".gantt-bar.skipped" in styles
    assert ".gantt-bar.pending" in styles
    assert ".gantt-bar.failed" in styles
    assert "@media (max-width: 880px)" in styles


def test_group_directory_page_is_silent_account_scoped_and_beginner_friendly() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert 'data-page="groups"' in markup
    assert 'id="refreshGroups"' in markup
    assert "管理机器人所在群聊的回复规则、可用功能和成员信息" in markup
    assert 'api("get_groups", refresh)' in script
    assert 'api("get_group_members", groupId)' in script
    assert 'previousAccount !== state.groupAccount' in script
    assert "当前接口未提供" in script
    assert "从群聊列表添加" in script
    assert "无需在群里发送命令" in script
    assert "群聊数量暂不可用" in script
    assert "等待读取群聊" in markup
    assert ".group-table { min-width: 620px; table-layout: fixed; }" in styles


def test_chat_management_adds_private_directory_profile_and_feature_scope() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert '>消息管理</span>' in markup
    assert 'role="tablist" aria-label="消息管理分类"' in markup
    assert 'data-chat-management-tab="private"' in markup
    assert 'id="privateManagementPanel" role="tabpanel"' in markup
    assert 'id="contactSearch"' in markup
    assert 'id="contactInspector"' in markup
    assert 'api("get_contacts", refresh)' in script
    assert 'api("get_contact_detail", contactId)' in script
    assert 'api("get_private_policy", state.contactAccount, contactId)' in script
    assert '"save_private_policy"' in script
    assert "CONTACT_PAGE_SIZE = 100" in script
    assert "资料只在当前运行期间保存在内存中" in script
    assert ".contact-layout" in styles
    assert ".contact-facts" in styles
    assert ".private-feature-options" in styles


def test_group_counts_hydrate_in_background_and_history_scrolls_inside_chat() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert "void hydrateGroupMemberCounts" in script
    assert "Promise.all" in script
    assert "group.member_count = total" in script
    assert 'class="member-count-state loading">读取中' in script
    assert 'content.classList.toggle("history-active", page === "history")' in script
    assert ".content-scroll.history-active { overflow: hidden; }" in styles
    assert "#page-history.active" in styles
    assert "flex: 1 1 0; min-height: 0; overflow: hidden;" in styles
    assert 'if (/<emoji\\b/i.test(content)) return summary("[表情]");' in script
    assert 'if (appType === "6") return withTitle("[文件]");' in script
    assert 'return withTitle("[卡片消息]");' in script
    assert "item.last_content ? displayMessageContent(item.last_content)" in script
    assert "function quotedMessageContent(refermsg)" in script
    assert 'presentation.messageKind === "quote"' in script
    assert 'presentation.messageKind === "revoke"' in script
    assert ".message-bubble-quote blockquote" in styles
    assert ".message-bubble-notice" in styles


def test_091_history_and_group_layout_regressions_are_covered() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert "function initializeHistoryDates()" in script
    assert '$("#historyStart").value = today' in script
    assert '$("#historyEnd").value = today' in script
    assert 'class="group-inspector panel empty"' in markup
    assert 'class="group-inspector-empty"' in markup
    assert 'avatarMarkup(`group-avatar tone-${avatarTone}`, item.name || "群", item.avatar)' in script
    assert 'avatarMarkup("group-avatar tone-0", group.name || "群", group.avatar)' in script
    assert 'bindAvatarFallbacks($("#groupRows"));' in script
    assert ".group-inspector.empty" in styles
    assert '.group-policy-form input[type="time"]' in styles
    assert ".message-pane > header > .status-badge" in styles
    assert "flex-basis: 36px" in styles
    assert "width: fit-content; max-width: 100%" in styles
    assert "max-width: min(68%, 560px)" in styles


def test_history_default_date_rolls_forward_across_midnight() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert 'historyAutoDate: ""' in script
    assert "function rollHistoryDatesForward()" in script
    assert "if (!previous || previous === today) return false;" in script
    assert "if (start.value !== previous || end.value !== previous) return false;" in script
    assert "rollHistoryDatesForward();\n    const load = ++state.historyLoad;" in script


def test_open_uninstaller_provisions_and_launches(monkeypatch, tmp_path: Path) -> None:
    api = _api(tmp_path)
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        app_shell,
        "load_robot_settings",
        lambda _root: SimpleNamespace(
            profile=SimpleNamespace(display_name="测试机器人")
        ),
    )
    monkeypatch.setattr(
        app_shell,
        "provision_uninstaller",
        lambda root, **kwargs: calls.append(("provision", (root, kwargs))) or True,
    )
    monkeypatch.setattr(
        app_shell,
        "launch_installed_uninstaller",
        lambda root: calls.append(("launch", root)),
    )

    result = api.open_uninstaller()

    assert result["ok"] is True
    assert [name for name, _value in calls] == ["provision", "launch"]


def test_start_action_shows_pending_state_before_bridge_returns() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")
    handler = script[
        script.index("async function runPrimaryAction") : script.index(
            "async function checkEnvironment"
        )
    ]

    assert handler.index("state.startPending = true") < handler.index("await api(")
    assert 'current === "STOPPED" && status?.environment?.ready' in script
    assert '"启动中…"' in script
    assert "const wechatLoggedIn = Boolean(runtime.wechat_identity);" in script
    assert 'wechatLoggedIn ? "已登录"' in script
    assert 'wechatLoggedIn ? "已登录，功能启动中"' in script
    assert 'role="status" aria-live="polite" aria-atomic="true"' in markup
    assert '.status-orbit[data-state="starting"] .power-button' in styles


def test_webui_schedules_startup_and_recurring_update_checks() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert "function automaticUpdateIntervalMs()" in script
    assert "state.bootstrap?.update?.check_interval_seconds" in script
    assert "scheduleAutomaticUpdateCheck(automaticUpdateIntervalMs())" in script
    assert "scheduleAutomaticUpdateCheck(1800)" in script
    assert "if (state.updateChecking) return" in script


def test_webui_keeps_update_progress_and_errors_visible() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")

    assert 'api("get_update_status")' in script
    assert 'pendingText: "正在下载…"' in script
    assert 'state.status?.state !== "STOPPED"' not in script
    assert 'id="modalProgress"' in markup
    assert 'id="modalError" role="alert"' in markup
    assert 'update.result.ok === true || update.result.status === "success"' in script
    assert "failureDetail" in script


def test_model_service_starts_empty_and_opens_a_dedicated_editor() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")

    assert 'id="providerOverview"' in markup
    assert 'id="providerEditorPage" hidden' in markup
    assert "let previewProviders = [];" in script
    assert "还没有模型服务" in script
    assert "data-provider-create" in script
    assert "function showProviderEditor" in script
    assert "function requestProviderOverview" in script
    assert "放弃模型配置？" in script
    assert "showProviderOverview(true);" in script
    assert 'previewParams.get("demo") === "model-services"' in script


def test_voice_clone_only_directs_users_to_its_dedicated_configuration_page() -> None:
    root = Path(app_shell.__file__).parents[2]
    script = (Path(app_shell.__file__).with_name("app_webui") / "app.js").read_text(
        encoding="utf-8"
    )
    features = json.loads(
        (root / "release" / "feature-set.json").read_text(encoding="utf-8")
    )["features"]
    voice = next(
        item for item in features if item["module"] == "astrbot_plugin_mimo_tts_clone"
    )

    assert voice["configuration"] == "page"
    assert 'previewFeature("feature-voice-clone", "语音克隆", "page"' in script
    assert "请打开专属配置页填写服务密钥" in script


def test_model_combobox_opens_the_complete_discovered_list_before_filtering() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert "providerModelFiltering: false" in script
    assert "state.providerModelFiltering = false;\n      state.providerModelFocus = -1;\n      renderProviderModelOptions(true);" in script
    assert "const keyword = state.providerModelFiltering" in script
    assert "if (event.isComposing) return;" in script
    assert 'combobox.classList.toggle("open-above"' in script
    assert ".model-combobox.open-above .model-options" in styles
    assert 'discover_provider_models: () => ok({items: ["grok-4.6", "deepseek-chat", "gpt-5.4", "claude-sonnet-4.5", "gemini-2.5-pro", "qwen3-max"], total: 6}' in script


def test_model_editor_keeps_natural_height_and_explains_default_selection() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")
    markup = (webui / "index.html").read_text(encoding="utf-8")

    assert "#page-settings .provider-editor-layout { flex: none; min-height: auto; align-items: start; }" in styles
    assert 'id="providerSaveIntentTitle"' in markup
    assert 'id="saveProvider"' in markup
    assert 'renderProviderSaveIntent(!(state.bootstrap?.providers || []).length);' in script
    assert 'renderProviderSaveIntent(Boolean(service.is_default));' in script
    assert '"模型服务已保存，当前回复模型保持不变。"' in script


def test_online_stop_action_uses_a_quiet_default_and_danger_hover() -> None:
    webui = Path(app_shell.__file__).with_name("app_webui")
    script = (webui / "app.js").read_text(encoding="utf-8")
    styles = (webui / "app.css").read_text(encoding="utf-8")

    assert '"停止运行"' in script
    assert 'primary.title = online ? "安全停止机器人" : primaryLabel;' in script
    assert '.status-orbit[data-state="online"] .power-button:hover:not(:disabled)' in styles


def test_088_preview_lists_simplified_group_analysis_without_checkin_task() -> None:
    script = (
        Path(app_shell.__file__).with_name("app_webui") / "app.js"
    ).read_text(encoding="utf-8")

    assert 'previewFeature("feature-group-analysis", "群聊分析"' in script
    assert 'fieldCount: 10, modelFieldCount: 1' in script
    assert 'simplified: true' in script
    assert '_ui_control: "line-list"' in script
    assert '_ui_control: "time-list"' in script
    assert '手账风（示例样式）' in script
    assert '渲染、重试、并发和提示词等专业参数继续使用推荐值' in script
    assert 'name: "每日签到"' not in script
    assert 'version: "0.9.2"' in script


def test_run_application_uses_explicit_webview_api_allowlist(
    monkeypatch, tmp_path: Path
) -> None:
    exposed: list[str] = []
    create_options: dict[str, object] = {}
    handlers: dict[str, list] = {"shown": [], "closing": [], "closed": []}

    class FakeHook:
        def __init__(self, name: str) -> None:
            self.name = name

        def __iadd__(self, callback):
            handlers[self.name].append(callback)
            return self

    class FakeWindow:
        def __init__(self) -> None:
            self.events = SimpleNamespace(
                shown=FakeHook("shown"),
                closing=FakeHook("closing"),
                closed=FakeHook("closed"),
            )

        def expose(self, *functions) -> None:
            exposed.extend(item.__name__ for item in functions)

    class FakeTray:
        def __init__(self, window, title: str) -> None:
            self.window = window
            self.title = title

        def start(self) -> None:
            return None

        def handle_window_closing(self):
            return False

        def stop(self) -> None:
            return None

    class FakeWebview:
        @staticmethod
        def create_window(*_args, **kwargs):
            create_options.update(kwargs)
            return FakeWindow()

        @staticmethod
        def start(**_kwargs) -> None:
            return None

    api = app_shell.ApplicationApi.__new__(app_shell.ApplicationApi)
    monkeypatch.setattr(app_shell, "ApplicationApi", lambda _root: api)
    monkeypatch.setattr(
        app_shell,
        "load_robot_settings",
        lambda _root: SimpleNamespace(
            profile=SimpleNamespace(display_name="测试机器人")
        ),
    )
    monkeypatch.setitem(sys.modules, "webview", FakeWebview)
    monkeypatch.setattr(app_shell, "WindowsTrayController", FakeTray)

    assert app_shell.run_application(tmp_path) == 0
    assert "js_api" not in create_options
    assert exposed == list(app_shell.PUBLIC_API_METHODS)
    assert api._window is not None
    assert isinstance(api._tray, FakeTray)
    assert api._tray.title == "测试机器人"
    assert handlers == {
        "shown": [api._tray.start],
        "closing": [api._tray.handle_window_closing],
        "closed": [api._tray.stop],
    }


def test_update_close_bypasses_minimize_to_tray(tmp_path: Path) -> None:
    api = _api(tmp_path)
    calls: list[str] = []
    api._tray = SimpleNamespace(
        permit_programmatic_close=lambda: calls.append("permit")
    )
    api._window = SimpleNamespace(destroy=lambda: calls.append("destroy"))

    api._close_window_for_update()

    assert calls == ["permit", "destroy"]
