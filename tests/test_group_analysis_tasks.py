from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from wechatrebot_manager.group_analysis_schedule import (
    ANALYSIS_ACTION, ANALYSIS_CONFIG, ANALYSIS_STORE, legacy_analysis_retired,
)
from wechatrebot_manager.group_analysis_task_migration import (
    GroupAnalysisTaskMigration, has_pending_group_analysis_migration,
)
from wechatrebot_manager.task_store import TaskStore, normalize_task


ACCOUNT = "wxid_robot"
GROUP = "123456@chatroom"


def _config(root: Path, mode="whitelist") -> Path:
    path = root / ANALYSIS_CONFIG
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({
        "basic": {"analysis_days": 2, "group_list_mode": "none", "llm_provider_id": "existing-provider"},
        "auto_analysis": {"scheduled_group_list_mode": mode, "scheduled_group_list": [GROUP],
                          "auto_analysis_time": ["9:00", "18:00", "09:00"], "unrelated": "keep"},
        "incremental": {"incremental_group_list_mode": "whitelist", "incremental_group_list": [GROUP]},
    }), encoding="utf-8")
    return path


def _migration(root, *, account=ACCOUNT, stopped=True):
    def authorize(expected):
        if expected != account:
            raise ValueError("账号已变化")

    return GroupAnalysisTaskMigration(root, TaskStore(root / ANALYSIS_STORE), authorize=authorize,
                                      stop=lambda: stopped, stopped=lambda: stopped)


def _task(**updates):
    return {"account_id": ACCOUNT, "action": "group_analysis", "arguments": {"days": 1},
            "targets": [{"type": "group", "id": GROUP}],
            "schedule": {"kind": "daily", "time": "09:00"}, **updates}


@pytest.mark.parametrize("days", [0, 8, True, "1", 1.0])
def test_analysis_days_are_validated_by_the_shared_task_model(days):
    with pytest.raises(ValueError, match="1 到 7"):
        normalize_task(_task(arguments={"days": days}))


def test_analysis_targets_cannot_be_private_even_for_a_draft():
    with pytest.raises(ValueError, match="被分析的群聊"):
        normalize_task(_task(draft=True, targets=[{"type": "private", "id": "wxid_friend"}]))
    assert normalize_task(_task(draft=True, targets=[]))["draft"] is True


def test_reading_legacy_context_does_not_seed_tasks_or_change_configuration(tmp_path):
    path = _config(tmp_path)
    before = path.read_bytes()
    migration = _migration(tmp_path)

    context = migration.context()
    preview = migration.preview({"account_id": ACCOUNT})

    assert context["state"] == "available"
    assert context["times"] == ["09:00", "18:00"]
    assert [item["schedule"]["time"] for item in preview["items"]] == ["09:00", "18:00"]
    assert all(item["draft"] and not item["enabled"] for item in preview["items"])
    assert migration.store.list(ACCOUNT) == []
    assert not migration.store.path.exists()
    assert path.read_bytes() == before


def test_confirmed_migration_retires_old_producer_and_preserves_other_configuration(tmp_path):
    path = _config(tmp_path)
    before = json.loads(path.read_text())
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})

    result = migration.commit(preview["token"])

    assert len(result["items"]) == 2
    assert all(item["draft"] and not item["enabled"] and item["arguments"] == {"days": 2}
               for item in result["items"])
    after = json.loads(path.read_text())
    expected = deepcopy(before)
    expected["auto_analysis"].update(scheduled_group_list_mode="whitelist", scheduled_group_list=[])
    assert after == expected
    assert migration.context()["state"] == "complete"
    assert not has_pending_group_analysis_migration(tmp_path)
    module = tmp_path / "app/astrbot/data/plugins/analysis/main.py"
    assert legacy_analysis_retired(module)

    first = result["items"][0]
    migration.store.delete(ACCOUNT, first["id"], first["revision"])
    second = result["items"][1]
    edited = migration.store.save({**second, "name": "保留人工编辑"})
    repeated = migration.commit(preview["token"])
    assert repeated["items"] == [edited]
    assert legacy_analysis_retired(module)


@pytest.mark.parametrize("mode", ["blacklist", "inherit"])
def test_exclusion_and_inherited_lists_require_explicit_replacement_targets(tmp_path, mode):
    path = _config(tmp_path, mode)
    migration = _migration(tmp_path)
    assert migration.context()["requires_target_selection"]
    with pytest.raises(ValueError, match="明确选择"):
        migration.preview({"account_id": ACCOUNT})
    chosen = [{"type": "group", "id": "987654@chatroom"}]
    preview = migration.preview({"account_id": ACCOUNT, "targets": chosen})
    result = migration.commit(preview["token"])
    assert all(item["targets"] == chosen for item in result["items"])
    assert json.loads(path.read_text())["auto_analysis"]["scheduled_group_list"] == []


def test_migration_records_confirmation_but_keeps_tasks_and_config_unchanged_until_stopped(tmp_path):
    path = _config(tmp_path)
    before = path.read_bytes()
    migration = _migration(tmp_path, stopped=False)
    preview = migration.preview({"account_id": ACCOUNT})

    assert migration.commit(preview["token"])["pending"]
    assert migration.store.action_owner(ANALYSIS_ACTION)["phase"] == "confirmed"
    assert migration.store.list(ACCOUNT) == []
    assert has_pending_group_analysis_migration(tmp_path)
    assert not legacy_analysis_retired(tmp_path / "app/astrbot/data/plugins/analysis/main.py")
    assert path.read_bytes() == before


def test_configuration_revision_conflict_is_detected_before_retirement(tmp_path):
    path = _config(tmp_path)
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})
    changed = json.loads(path.read_text())
    changed["auto_analysis"]["auto_analysis_time"] = ["10:00"]
    path.write_text(json.dumps(changed))

    with pytest.raises(ValueError, match="已变化"):
        migration.commit(preview["token"])
    assert migration.store.action_owner(ANALYSIS_ACTION) is None
    assert json.loads(path.read_text()) == changed


def test_interrupted_migration_is_recoverable_and_blocks_startup(tmp_path, monkeypatch):
    _config(tmp_path)
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})
    import wechatrebot_manager.group_analysis_task_migration as module
    writer = module.write_json_atomic

    def unavailable(*_args):
        raise OSError("simulated unavailable disk")

    monkeypatch.setattr(module, "write_json_atomic", unavailable)
    with pytest.raises(OSError):
        migration.commit(preview["token"])
    assert has_pending_group_analysis_migration(tmp_path)
    assert migration.store.list(ACCOUNT) == []
    assert legacy_analysis_retired(tmp_path / "app/astrbot/data/plugins/analysis/main.py")

    monkeypatch.setattr(module, "write_json_atomic", writer)
    resumed = _migration(tmp_path)
    resumed_preview = resumed.preview({"account_id": ACCOUNT})
    result = resumed.commit(resumed_preview["token"])
    assert len(result["items"]) == 2
    assert not has_pending_group_analysis_migration(tmp_path)


def test_draft_batch_and_migration_completion_commit_atomically(tmp_path, monkeypatch):
    _config(tmp_path)
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})
    save = migration.store._save_task
    calls = 0

    def fail_second(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated write failure")
        return save(*args)

    monkeypatch.setattr(migration.store, "_save_task", fail_second)
    with pytest.raises(OSError):
        migration.commit(preview["token"])
    assert migration.store.list(ACCOUNT) == []
    assert has_pending_group_analysis_migration(tmp_path)
    monkeypatch.setattr(migration.store, "_save_task", save)
    assert len(migration.commit(preview["token"])["items"]) == 2


def test_another_account_cannot_resume_a_confirmed_migration(tmp_path, monkeypatch):
    _config(tmp_path)
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})
    monkeypatch.setattr(migration.store, "complete_action_migration", lambda *_args: (_ for _ in ()).throw(OSError()))
    with pytest.raises(OSError):
        migration.commit(preview["token"])
    with pytest.raises(ValueError, match="另一微信账号"):
        _migration(tmp_path, account="wxid_other").preview({"account_id": "wxid_other"})


def test_corrupt_ownership_never_reactivates_the_old_scheduler(tmp_path):
    path = tmp_path / ANALYSIS_STORE
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a SQLite database")
    assert legacy_analysis_retired(tmp_path / "app/astrbot/data/plugins/analysis/main.py")
    assert _migration(tmp_path).context()["state"] == "blocked"


@pytest.mark.parametrize("days", [8, 30, 0, None, "invalid"])
def test_legacy_days_are_exposed_and_require_explicit_valid_replacement(tmp_path, days):
    path = _config(tmp_path)
    config = json.loads(path.read_text())
    config["basic"]["analysis_days"] = days
    path.write_text(json.dumps(config), encoding="utf-8")
    migration = _migration(tmp_path)
    assert migration.context()["days"] == days
    with pytest.raises(ValueError, match="1 到 7"):
        migration.preview({"account_id": ACCOUNT})
    preview = migration.preview({"account_id": ACCOUNT, "days": 7})
    assert preview["source"]["days"] == days
    assert all(item["arguments"]["days"] == 7 for item in preview["items"])
    assert json.loads(path.read_text()) == config


def _service_with_live_probe(root, state):
    from wechatrebot_manager.app_shell import ApplicationApi
    from wechatrebot_manager.plugin_center import EngineUnavailableError
    from wechatrebot_manager.runtime_status import RuntimeSnapshot
    from wechatrebot_manager.scheduled_task_service import ScheduledTaskService

    class Directory:
        def list(self, refresh=False):
            return {"account_id": state["account"], "stale": not bool(state["account"]),
                    "items": [{"group_id": GROUP}] if state["account"] else []}

    class Engine:
        def request(self, method, path, payload=None):
            assert method == "GET"
            if not state["account"]:
                raise EngineUnavailableError("engine stopped")
            return {"version": 1, "runtime_ready": True, "account_id": state["account"]}

    def snapshot():
        online = bool(state["account"])
        return SimpleNamespace(runtime=RuntimeSnapshot(online, online, state["account"],
                                                        online, online, online, online))

    facade = ApplicationApi.__new__(ApplicationApi)
    facade._lifecycle = SimpleNamespace(fresh_runtime_snapshot=lambda: snapshot().runtime)

    def stop():
        state["stops"] += 1
        state["account"] = ""
        state["stopped"] = state["results"].pop(0)
        return state["stopped"]

    value = ScheduledTaskService(root, groups=Directory(), contacts=Directory(),
                                 account_hint=facade._history_account_id, stop=stop,
                                 stopped=lambda: state["stopped"])
    value.engine = Engine()
    return value


def test_real_service_recovers_when_stopping_makes_the_history_account_probe_raise(tmp_path):
    path = _config(tmp_path)
    before = path.read_bytes()
    state = {"account": ACCOUNT, "stopped": False, "stops": 0, "results": [False, True]}
    service = _service_with_live_probe(tmp_path, state)
    preview = service.preview_group_analysis_migration({"account_id": ACCOUNT})
    assert service.migrate_group_analysis(preview["token"])["pending"]
    assert path.read_bytes() == before

    context = service.context()
    assert context["account_id"] == ""
    assert context["items"] == context["groups"] == context["contacts"] == []
    assert context["legacy_group_analysis"]["state"] == "confirmed"
    assert context["legacy_group_analysis"]["account_id"] == ACCOUNT
    assert "target_ids" not in context["legacy_group_analysis"]
    with pytest.raises(ValueError, match="账号"):
        service.save(_task(draft=True))

    resumed = _service_with_live_probe(tmp_path, state)
    with pytest.raises(ValueError, match="已确认"):
        resumed.preview_group_analysis_migration({"account_id": ACCOUNT, "days": 7})
    next_preview = resumed.preview_group_analysis_migration({"account_id": ACCOUNT})
    result = resumed.migrate_group_analysis(next_preview["token"])
    assert len(result["items"]) == 2
    assert all(item["arguments"]["days"] == 2 for item in result["items"])
    assert state["stops"] == 2 and state["stopped"]
    assert resumed.context()["legacy_group_analysis"]["state"] == "complete"
    assert resumed.migrate_group_analysis(next_preview["token"]) == result


def test_real_service_recovers_prepared_migration_offline_without_allowing_new_offline_preview(tmp_path, monkeypatch):
    _config(tmp_path)
    state = {"account": "", "stopped": True, "stops": 0, "results": [True, True]}
    service = _service_with_live_probe(tmp_path, state)
    assert service.context()["account_id"] == ""
    with pytest.raises(ValueError, match="账号尚未确认"):
        service.preview_group_analysis_migration({"account_id": ACCOUNT})
    state["account"] = ACCOUNT
    preview = service.preview_group_analysis_migration({"account_id": ACCOUNT})
    import wechatrebot_manager.group_analysis_task_migration as module
    writer = module.write_json_atomic

    def unavailable(*_args):
        raise OSError("simulated unavailable disk")

    monkeypatch.setattr(module, "write_json_atomic", unavailable)
    with pytest.raises(OSError):
        service.migrate_group_analysis(preview["token"])
    assert service.context()["legacy_group_analysis"]["state"] == "prepared"
    assert service.context()["legacy_group_analysis"]["target_ids"] == [GROUP]
    with pytest.raises(ValueError, match="只能撤销"):
        service.cancel_group_analysis_migration(ACCOUNT)
    monkeypatch.setattr(module, "write_json_atomic", writer)
    resumed = _service_with_live_probe(tmp_path, state)
    next_preview = resumed.preview_group_analysis_migration({"account_id": ACCOUNT})
    assert len(resumed.migrate_group_analysis(next_preview["token"])["items"]) == 2


def test_real_service_hides_migration_details_from_a_different_live_account(tmp_path):
    path = _config(tmp_path)
    before = path.read_bytes()
    state = {"account": ACCOUNT, "stopped": False, "stops": 0, "results": [False]}
    service = _service_with_live_probe(tmp_path, state)
    preview = service.preview_group_analysis_migration({"account_id": ACCOUNT})
    service.migrate_group_analysis(preview["token"])
    state["account"] = "wxid_other"
    context = service.context()["legacy_group_analysis"]
    assert context["state"] == "blocked"
    assert not {"account_id", "target_ids", "times", "days"} & context.keys()
    with pytest.raises(ValueError, match="账号已切换"):
        service.migrate_group_analysis(preview["token"])
    with pytest.raises(ValueError, match="账号已切换"):
        service.preview_group_analysis_migration({"account_id": ACCOUNT})
    with pytest.raises(ValueError, match="账号已切换"):
        service.cancel_group_analysis_migration(ACCOUNT)
    assert state["stops"] == 1
    assert path.read_bytes() == before


def test_completed_retirement_allows_other_accounts_without_exposing_original_plan(tmp_path):
    _config(tmp_path)
    state = {"account": ACCOUNT, "stopped": False, "stops": 0, "results": [True]}
    service = _service_with_live_probe(tmp_path, state)
    preview = service.preview_group_analysis_migration({"account_id": ACCOUNT})
    assert len(service.migrate_group_analysis(preview["token"])["items"]) == 2
    state["account"] = "wxid_other"
    context = service.context()
    assert context["account_id"] == "wxid_other"
    assert context["legacy_group_analysis"]["state"] == "complete"
    assert not {"account_id", "target_ids", "times", "days"} & context["legacy_group_analysis"].keys()
    assert context["items"] == []
    from wechatrebot_manager.group_analysis_schedule import require_analysis_ownership
    require_analysis_ownership(tmp_path, service.store, "wxid_other")
    assert service.save(_task(account_id="wxid_other", draft=True))["item"]["account_id"] == "wxid_other"


@pytest.mark.parametrize(("account", "stopped", "allowed"), [
    (ACCOUNT, False, True), ("", True, True), ("", False, False), ("wxid_other", True, False),
])
def test_cancel_only_releases_confirmed_ownership_for_a_verified_owner(tmp_path, account, stopped, allowed):
    path = _config(tmp_path)
    before = path.read_bytes()
    migration = _migration(tmp_path, stopped=False)
    preview = migration.preview({"account_id": ACCOUNT})
    assert migration.commit(preview["token"])["pending"]
    migration.current_account = lambda: account
    migration.stopped = lambda: stopped
    if allowed:
        assert migration.cancel(ACCOUNT)["cancelled"]
        assert migration.store.action_owner(ANALYSIS_ACTION) is None
        assert not has_pending_group_analysis_migration(tmp_path)
    else:
        with pytest.raises(ValueError, match="账号"):
            migration.cancel(ACCOUNT)
        assert migration.store.action_owner(ANALYSIS_ACTION)["phase"] == "confirmed"
    assert migration.store.list(ACCOUNT) == []
    assert path.read_bytes() == before


def test_stale_preview_cannot_restore_a_confirmation_cancelled_in_another_window(tmp_path):
    _config(tmp_path)
    migration = _migration(tmp_path, stopped=False)
    preview = migration.preview({"account_id": ACCOUNT})
    assert migration.commit(preview["token"])["pending"]
    assert _migration(tmp_path).cancel(ACCOUNT)["cancelled"]
    with pytest.raises(ValueError, match="已经撤销"):
        migration.commit(preview["token"])
    assert migration.store.action_owner(ANALYSIS_ACTION) is None


@pytest.mark.parametrize("deleted", [False, True])
def test_destination_collision_keeps_old_schedule_and_repreview_preserves_existing_rows(tmp_path, deleted):
    path = _config(tmp_path)
    before = path.read_bytes()
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})
    collision = migration.store.save({**preview["items"][0], "name": "保留人工任务"})
    if deleted:
        migration.store.delete(ACCOUNT, collision["id"], collision["revision"])
    with pytest.raises(ValueError, match="标识已被使用"):
        migration.commit(preview["token"])
    assert migration.store.action_owner(ANALYSIS_ACTION)["phase"] == "confirmed"
    assert path.read_bytes() == before
    renewed = migration.preview({"account_id": ACCOUNT})
    assert {item["id"] for item in preview["items"]}.isdisjoint(item["id"] for item in renewed["items"])
    assert [{k: v for k, v in item.items() if k != "id"} for item in renewed["items"]] == [
        {k: v for k, v in item.items() if k != "id"} for item in preview["items"]]
    assert len(migration.commit(renewed["token"])["items"]) == 2
    if deleted:
        with pytest.raises(ValueError, match="没有找到"):
            migration.store.get(ACCOUNT, collision["id"])
    else:
        assert migration.store.get(ACCOUNT, collision["id"]) == collision


def test_prepared_migration_reserves_destination_ids_until_atomic_completion(tmp_path, monkeypatch):
    _config(tmp_path)
    migration = _migration(tmp_path)
    preview = migration.preview({"account_id": ACCOUNT})
    import wechatrebot_manager.group_analysis_task_migration as module
    writer = module.write_json_atomic
    attempted = []

    def concurrent_save(path, config):
        with pytest.raises(ValueError, match="正在迁移"):
            TaskStore(migration.store.path).save({**preview["items"][0], "name": "其他窗口"})
        attempted.append(True)
        return writer(path, config)

    monkeypatch.setattr(module, "write_json_atomic", concurrent_save)
    assert len(migration.commit(preview["token"])["items"]) == 2
    assert attempted == [True]
