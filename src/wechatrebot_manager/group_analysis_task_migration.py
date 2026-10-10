"""Explicit transfer of plugin-owned report schedules into paused task drafts."""
from __future__ import annotations

from copy import deepcopy
import hashlib
from pathlib import Path
import secrets
import sqlite3
from typing import Callable

from .group_analysis_schedule import (
    ANALYSIS_ACTION, ANALYSIS_CONFIG, ANALYSIS_STORE,
    analysis_schedule_source, read_analysis_configuration,
)
from .json_io import write_json_atomic
from .payload import _checked_target
from .single_instance import SingleInstanceLock
from .task_store import TaskStore, normalize_task, validate_account


def has_pending_group_analysis_migration(root: Path) -> bool:
    owner = TaskStore(_checked_target(root, ANALYSIS_STORE)).action_owner(ANALYSIS_ACTION)
    return bool(owner and owner["phase"] in {"confirmed", "prepared"})


class GroupAnalysisTaskMigration:
    def __init__(self, root: Path, store: TaskStore, *, authorize: Callable[[str], None],
                 stop: Callable[[], bool], stopped: Callable[[], bool],
                 current_account: Callable[[], str] | None = None):
        self.root, self.store = root, store
        self.authorize, self.stop, self.stopped = authorize, stop, stopped
        self.current_account = current_account
        self._previews: dict[str, dict] = {}

    def _authorize(self, account: str, *, recovery: bool = False, continuing_stop: bool = False) -> None:
        current = str(self.current_account() or "") if self.current_account else account
        if current == account:
            self.authorize(account)
            return
        if current:
            raise ValueError("微信账号已切换，请回到原账号处理群分析迁移。")
        if recovery and (continuing_stop or self.stopped()):
            return  # A verified fixed-owner record authorizes only completion of maintenance.
        raise ValueError("微信账号尚未确认，请先连接所属微信账号。")

    def context(self, account: str | None = None) -> dict:
        try:
            owner = self.store.action_owner(ANALYSIS_ACTION)
            if owner:
                account = account if account is not None else str(self.current_account() or "") if self.current_account else owner["account_id"]
                if account and account != owner["account_id"]:
                    if owner["phase"] == "complete":
                        return {"state": "complete", "message": "旧群分析调度已退役。"}
                    return {"state": "blocked", "message": "群分析迁移属于其他微信账号，请切回所属账号处理。"}
                if owner["phase"] != "complete":
                    self._validate_plan(owner["plan"], owner["account_id"])
                if not account and (owner["phase"] == "complete" or not self.stopped()):
                    return {"state": owner["phase"],
                            **({"account_id": owner["account_id"]} if owner["phase"] != "complete" else {}),
                            "message": "请继续完成已确认的停止操作。" if owner["phase"] != "complete" else "旧群分析调度已退役。"}
                source = owner["plan"].get("source", {})
                return {**self._source_public(source), "state": owner["phase"],
                        "account_id": owner["account_id"],
                        "message": ("旧群聊分析计划已迁入统一任务；旧调度不会重新启用。"
                                    if owner["phase"] == "complete" else "旧群聊分析计划迁移未完成，请继续迁移。")}
            config, _revision = read_analysis_configuration(self.root)
            source = analysis_schedule_source(config)
            return {**self._source_public(source), "state": "available" if source["enabled"] else "none",
                    "message": "旧计划仍由群聊分析功能执行；确认迁移后会转为暂停草稿。" if source["enabled"] else ""}
        except (OSError, ValueError, sqlite3.Error):
            return {"state": "blocked", "message": "旧群聊分析配置或迁移记录无法确认，请保留原数据并检查。"}

    @staticmethod
    def _source_public(source: dict) -> dict:
        return {"mode": source.get("mode", "whitelist"), "times": deepcopy(source.get("times", [])),
                "target_ids": deepcopy(source.get("target_ids", [])), "days": deepcopy(source.get("days", 1)),
                "requires_target_selection": source.get("mode", "whitelist") != "whitelist"}

    def preview(self, payload: dict) -> dict:
        account = validate_account(payload.get("account_id"))
        owner = self.store.action_owner(ANALYSIS_ACTION)
        if owner:
            if owner["account_id"] != account:
                raise ValueError("旧计划的迁移属于另一微信账号，请切回原账号处理。")
            if owner["phase"] == "complete":
                raise ValueError("旧群聊分析计划已完成迁移，请在任务列表编辑。")
            plan = deepcopy(owner["plan"])
            self._validate_plan(plan, account)
            self._authorize(account, recovery=True, continuing_stop=True)
            if "targets" in payload or "days" in payload:
                raise ValueError("继续迁移只能使用已确认的目标和天数；需要修改时请先撤销尚未开始的迁移。")
            if owner["phase"] == "confirmed":
                if read_analysis_configuration(self.root)[1] != plan["revision"]:
                    raise ValueError("旧群聊分析配置已变化，请先撤销迁移确认，再重新预览。")
                if self.store.migration_conflicts(plan):
                    plan = self._renew_task_ids(plan)
        else:
            self._authorize(account)
            plan = self._build_plan(payload, account)
        token = secrets.token_urlsafe(24)
        self._previews.clear()
        self._previews[token] = {"plan": deepcopy(plan), "owner": deepcopy(owner)}
        return {"token": token, "account_id": account, "items": plan["items"],
                "source": self._source_public(plan["source"]), "requires_stop": True,
                "message": "迁移会停止机器人，停用旧群分析调度，并按各时间转为暂停草稿。统一任务按所选天数重新分析当前账号的群记录；原增量数据保留，核对后再启用。"}

    def _build_plan(self, payload: dict, account: str) -> dict:
        config, revision = read_analysis_configuration(self.root)
        source = analysis_schedule_source(config)
        if not source["enabled"] or not source["times"]:
            raise ValueError("没有可迁移的旧群聊分析计划。")
        targets = payload.get("targets")
        if targets is None:
            if source["mode"] != "whitelist":
                raise ValueError("旧计划使用排除或继承名单，请明确选择迁移后的微信群。")
            targets = [{"type": "group", "id": identity} for identity in source["target_ids"]]
        if not isinstance(targets, list) or not targets:
            raise ValueError("请明确选择至少一个迁移后的微信群。")
        plan = {"schema_version": 1, "account_id": account, "revision": revision, "source": source, "items": []}
        for clock in source["times"]:
            plan["items"].append(normalize_task({
                "account_id": account,
                "name": "群聊分析 " + clock, "action": ANALYSIS_ACTION,
                "arguments": {"days": payload.get("days", source["days"])}, "targets": targets,
                "schedule": {"kind": "daily", "time": clock, "timezone": "Asia/Shanghai"},
                "enabled": False, "draft": True,
            }))
        return self._renew_task_ids(plan)

    @staticmethod
    def _renew_task_ids(plan: dict) -> dict:
        plan = deepcopy(plan)
        plan["migration_id"] = secrets.token_hex(16)
        for item in plan["items"]:
            key = f"{plan['account_id']}:{plan['revision']}:{item['schedule']['time']}:{plan['migration_id']}"
            item["id"] = "legacy_analysis_" + hashlib.sha256(key.encode()).hexdigest()[:40]
        return plan

    @staticmethod
    def _validate_plan(plan: dict, account: str) -> None:
        if (plan.get("schema_version") != 1 or plan.get("account_id") != account
                or not isinstance(plan.get("source"), dict) or not isinstance(plan.get("items"), list)
                or not plan["items"]):
            raise ValueError("群分析迁移记录不完整，请保留原数据。")
        for item in plan["items"]:
            normalized = normalize_task(item)
            if (normalized != item or item["action"] != ANALYSIS_ACTION or item["account_id"] != account
                    or not item["draft"] or item["enabled"]):
                raise ValueError("群分析迁移记录的任务内容不一致。")
        if len({item["id"] for item in plan["items"]}) != len(plan["items"]):
            raise ValueError("群分析迁移记录存在重复任务标识。")

    def commit(self, token: str) -> dict:
        preview = deepcopy(self._previews.get(token)) if isinstance(token, str) else None
        if not preview:
            raise ValueError("请先预览并确认旧群聊分析计划。")
        plan = preview["plan"]
        account = plan["account_id"]
        self._validate_plan(plan, account)
        lock = SingleInstanceLock(self.root / "data/manager/group-analysis-migration")
        if not lock.acquire():
            raise ValueError("另一项群分析迁移正在处理，请稍后重试。")
        try:
            owner = self.store.action_owner(ANALYSIS_ACTION)
            if owner is None and preview["owner"] is not None:
                raise ValueError("迁移确认已经撤销，请重新预览。")
            if owner and (owner["account_id"] != account or owner["plan"] != plan and owner != preview["owner"]):
                raise ValueError("群分析迁移内容已变化，请重新预览。")
            self._authorize(account, recovery=owner is not None,
                            continuing_stop=bool(owner and owner["phase"] != "complete"))
            if owner and owner["phase"] == "complete":
                return self._result(plan)
            if not owner or owner["phase"] == "confirmed":
                if read_analysis_configuration(self.root)[1] != plan["revision"]:
                    raise ValueError("旧群聊分析配置已变化，请重新预览后迁移；也可撤销尚未开始的迁移。")
                confirmed = self.store.claim_action(ANALYSIS_ACTION, account, plan, phase="confirmed",
                                                    expected_plan=owner["plan"] if owner else None)
                self._previews[token]["owner"] = confirmed
            if not self.stop() or not self.stopped():
                return {"pending": True, "message": "请先关闭机器人微信，停止完成后点击继续迁移。"}
            self._authorize(account, recovery=True)
            self._transfer(plan, bool(owner and owner["phase"] == "prepared"))
            return self._result(plan)
        finally:
            lock.release()

    def _transfer(self, plan: dict, resuming: bool) -> None:
        path = _checked_target(self.root, ANALYSIS_CONFIG)
        config, revision = read_analysis_configuration(self.root)
        source = plan["source"]
        retired = {**source["original_schedule"], "scheduled_group_list_mode": "whitelist", "scheduled_group_list": []}
        current = config.get("auto_analysis", {})
        if ((not resuming and revision != plan["revision"])
                or current not in (source["original_schedule"], retired)):
            raise ValueError("旧群聊分析时间或名单已变化，迁移已暂停。")
        if not self.stopped():
            raise ValueError("无法确认机器人已经停止，请停止后继续迁移。")
        self.store.claim_action(ANALYSIS_ACTION, plan["account_id"], plan, phase="prepared")
        config["auto_analysis"] = retired
        write_json_atomic(path, config)
        if not self.stopped():
            raise ValueError("机器人状态已变化；旧调度已停用，请停止后继续迁移。")
        self.store.complete_action_migration(ANALYSIS_ACTION, plan["account_id"], plan)

    def cancel(self, account: str) -> dict:
        validate_account(account)
        lock = SingleInstanceLock(self.root / "data/manager/group-analysis-migration")
        if not lock.acquire():
            raise ValueError("另一项群分析迁移正在处理，请稍后重试。")
        try:
            owner = self.store.action_owner(ANALYSIS_ACTION)
            if not owner or owner["phase"] != "confirmed":
                raise ValueError("只能撤销尚未开始写入的迁移；已开始的迁移请继续完成。")
            if owner["account_id"] != account:
                raise ValueError("这项迁移不属于所选微信账号。")
            self._validate_plan(owner["plan"], account)
            self._authorize(account, recovery=True)
            self.store.cancel_action_migration(ANALYSIS_ACTION, account, owner["plan"])
            self._previews.clear()
            return {"cancelled": True, "message": "已撤销迁移确认，保留旧群分析配置；机器人不会自动启动。"}
        finally:
            lock.release()

    def _result(self, plan: dict) -> dict:
        identities = {item["id"] for item in plan["items"]}
        items = [item for item in self.store.list(plan["account_id"]) if item["id"] in identities]
        return {"items": items, "message": "旧群聊分析计划已迁入暂停草稿；保留后续编辑和删除结果，机器人不会自动启动。"}
