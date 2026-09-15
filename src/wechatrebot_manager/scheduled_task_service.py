"""Manager-facing facade for explicitly created, account-scoped scheduled tasks."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from .contact_directory import ContactDirectoryService
from .group_directory import GroupDirectoryService
from .group_policy import GroupPolicyService
from .private_policy import PrivateChatPolicyService
from .plugin_center import EngineApiClient, EngineUnavailableError, PluginCenterError
from .task_store import ACTION_FEATURES, ACTION_NAMES, TaskStore, normalize_arguments, normalize_task, validate_account


_API = "/api/plug/jiang_schedule/page/unified/"


class ScheduledTaskService:
    def __init__(self, install_root: str | Path, *, groups=None, contacts=None, account_hint: Callable[[], str] | None = None):
        self.root = Path(install_root).resolve()
        self.store = TaskStore(self.root / "data/manager/scheduled-tasks.db")
        self.engine = EngineApiClient(self.root)
        self.groups = groups or GroupDirectoryService(self.root)
        self.contacts = contacts or ContactDirectoryService(self.root)
        self.account_hint = account_hint or (lambda: "")

    def health(self) -> dict[str, Any]:
        try:
            result = self.engine.request("GET", _API + "health")
        except EngineUnavailableError:
            return {"online": False, "runtime_ready": False, "message": "机器人尚未就绪，可先保存任务草稿。"}
        except PluginCenterError:
            return {"online": True, "runtime_ready": False, "message": "定时任务功能尚未就绪，请在功能中心启用；旧版本需要更新后使用。"}
        if not isinstance(result, dict) or result.get("version", result.get("protocol_version")) != 1:
            return {"online": True, "runtime_ready": False, "message": "当前运行版本暂不支持统一任务，请更新后使用。"}
        return {**result, "online": True, "runtime_ready": result.get("runtime_ready", True) is True}

    @staticmethod
    def _directory(data: dict[str, Any], kind: str, account: str) -> list[dict[str, str]]:
        if not account or data.get("account_id") != account:
            return []
        result = []
        for raw in data.get("items", []):
            identity = str((raw.get("group_id") if kind == "group" else raw.get("user_id") or raw.get("wxid")) or "")
            if not identity:
                continue
            name = str(raw.get("remark") or raw.get("remark_name") or raw.get("group_name") or raw.get("nickname") or raw.get("nick_name") or raw.get("name") or identity)
            result.append({"id": identity, "name": name, "type": kind})
        return result

    def _snapshot(self, *, refresh: bool = False) -> tuple[dict, dict, dict]:
        with ThreadPoolExecutor(max_workers=3) as executor:
            group_future = executor.submit(self.groups.list, refresh)
            contact_future = executor.submit(self.contacts.list, refresh)
            health_future = executor.submit(self.health)
            return group_future.result(), contact_future.result(), health_future.result()

    @staticmethod
    def _names(item: dict, names: dict[tuple[str, str], str]) -> dict:
        item = deepcopy(item)
        for target in item.get("targets", []):
            target["name"] = names.get((target["type"], target["id"]), "对象待确认")
        return item

    def _policy_issue(self, item: dict) -> str:
        clock = item.get("schedule", {}).get("time", "")
        for target in item.get("targets", []):
            policy = (GroupPolicyService(self.root).get(item["account_id"], target["id"])
                      if target["type"] == "group"
                      else PrivateChatPolicyService(self.root).overview(item["account_id"], target["id"])["policy"])
            if not policy.get("bot_enabled", True):
                return "有发送对象已关闭机器人功能。"
            if not policy.get("scheduled_enabled", True):
                return "有发送对象已暂停全部定时推送。"
            if policy.get("daily_limit", 3) <= 0:
                return "有发送对象的每日推送上限为零。"
            feature = ACTION_FEATURES[item["action"]]
            if feature and feature not in policy.get("allowed_features", []):
                return "有发送对象尚未允许该内容功能。"
            quiet = policy.get("quiet_hours") or {}
            if quiet.get("enabled") and clock:
                start, end = quiet.get("start", "22:00"), quiet.get("end", "08:00")
                if start == end or (start < end and start <= clock < end) or (start > end and (clock >= start or clock < end)):
                    return "执行时间落在发送对象的安静时段。"
        return ""

    def context(self) -> dict[str, Any]:
        group, contact, health = self._snapshot()
        fresh_accounts = {str(data.get("account_id")) for data in (group, contact) if data.get("account_id") and not data.get("stale")}
        account = str(health.get("account_id") or "")
        if not account and len(fresh_accounts) == 1:
            account = next(iter(fresh_accounts))
        if not health["online"] and not account and not fresh_accounts:
            account = str(self.account_hint() or "")
        if len(fresh_accounts) > 1 or (account and fresh_accounts and fresh_accounts != {account}):
            return {"account_id": "", "items": [], "groups": [], "contacts": [], "capabilities": [], "online": health["online"], "runtime_ready": False, "message": "微信账号正在变化，请刷新后重新选择发送对象。"}
        groups = self._directory(group, "group", account)
        contacts = self._directory(contact, "private", account)
        names = {(target["type"], target["id"]): target["name"] for target in groups + contacts}
        raw_caps = {item.get("id"): item for item in health.get("capabilities", []) if isinstance(item, dict)}
        capabilities = [{"id": action, "name": name, "ready": health["runtime_ready"] and raw_caps.get(action, {}).get("ready", action == "text"),
                         "message": raw_caps.get(action, {}).get("message", "" if health["runtime_ready"] and action == "text" else "请先准备定时任务和常用指令功能。")}
                        for action, name in ACTION_NAMES.items()]
        items = [self._names(item, names) for item in self.store.list(account)] if account else []
        for item in items:
            if item["enabled"] and item["status"] != "completed":
                if not health["runtime_ready"]:
                    item["status"] = "waiting_runtime"
                    item["status_message"] = health.get("message") or "定时发送能力尚未就绪。"
                elif health.get("account_id") != account:
                    item["status"] = "waiting_account"
                    item["status_message"] = "等待任务绑定的微信账号重新登录。"
                elif not next(cap["ready"] for cap in capabilities if cap["id"] == item["action"]):
                    item["status"] = "blocked"
                    item["status_message"] = next(cap["message"] for cap in capabilities if cap["id"] == item["action"])
                elif ((group.get("stale") and any(target["type"] == "group" for target in item["targets"]))
                      or (contact.get("stale") and any(target["type"] == "private" for target in item["targets"]))):
                    item["status"] = "blocked"
                    item["status_message"] = "发送对象目录尚未确认，请刷新后核对。"
                elif any((target["type"], target["id"]) not in names for target in item["targets"]):
                    item["status"] = "blocked"
                    item["status_message"] = "有发送对象已不在当前账号目录。"
                elif issue := self._policy_issue(item):
                    item["status"] = "blocked"
                    item["status_message"] = issue
        messages = [health.get("message", "")]
        if group.get("stale") or contact.get("stale"):
            messages.append("部分聊天目录尚未更新；启用前会重新核验所选对象。")
        return {"account_id": account, "items": items, "capabilities": capabilities, "groups": groups, "contacts": contacts,
                "online": health["online"], "runtime_ready": health["runtime_ready"], "message": " ".join(filter(None, messages))}

    def _verify_live(self, task: dict, *, check_scheduled_time: bool = True) -> None:
        group, contact, health = self._snapshot(refresh=True)
        if not health["runtime_ready"]:
            raise ValueError(health.get("message") or "请先启用定时任务功能。")
        account = task["account_id"]
        if health.get("account_id") != account:
            raise ValueError("当前登录微信与任务所属账号不一致，请刷新后重试。")
        capabilities = {
            item.get("id"): item
            for item in health.get("capabilities", [])
            if isinstance(item, dict)
        }
        capability = capabilities.get(task["action"], {})
        if task["action"] != "text" and capability.get("ready") is not True:
            raise ValueError(capability.get("message") or "所需内容功能尚未就绪，请先在功能中心完成配置。")
        for target in task["targets"]:
            directory = group if target["type"] == "group" else contact
            if directory.get("stale") or directory.get("account_id") != account:
                raise ValueError("所选聊天目录尚未确认，请刷新后再启用任务。")
            choices = self._directory(directory, target["type"], account)
            if not any(item["id"] == target["id"] for item in choices):
                raise ValueError("部分发送对象已不在当前账号目录，请重新选择。")
            policy = (GroupPolicyService(self.root).get(account, target["id"]) if target["type"] == "group"
                      else PrivateChatPolicyService(self.root).overview(account, target["id"])["policy"])
            if not policy.get("bot_enabled", True) or not policy.get("scheduled_enabled", True):
                raise ValueError("所选聊天已暂停机器人或定时推送，请在消息管理中调整。")
            if policy.get("daily_limit", 3) <= 0:
                raise ValueError("所选聊天的每日发送上限为零，请在消息管理中调整。")
            feature = ACTION_FEATURES[task["action"]]
            if feature and feature not in policy.get("allowed_features", []):
                raise ValueError("所选聊天未允许该内容功能，请在消息管理中调整。")
            quiet = policy.get("quiet_hours") or {}
            clock = task["schedule"]["time"]
            if quiet.get("enabled") and check_scheduled_time:
                start, end = quiet.get("start", "22:00"), quiet.get("end", "08:00")
                if start == end or (start < end and start <= clock < end) or (start > end and (clock >= start or clock < end)):
                    raise ValueError("执行时间处于所选聊天的安静时段，请修改时间或聊天规则。")

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        task = normalize_task(payload)
        if task["draft"]:
            self._require_current_account(task["account_id"])
            item = self.store.save({**task, "revision": payload.get("revision", 0)})
            return {"item": item, "message": "任务草稿已保存，尚未启用。"}
        self._verify_live(task)
        result = self.engine.request("POST", _API + "save", {**task, "revision": payload.get("revision", 0)})
        if not isinstance(result, dict) or not isinstance(result.get("item"), dict):
            raise PluginCenterError("未收到任务保存确认，请刷新核对。")
        return result

    def set_enabled(self, account: str, task_id: str, enabled: bool, revision: int) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("任务启用状态格式不正确。")
        task = self.store.get(account, task_id)
        if enabled:
            return self.save({**task, "revision": revision, "enabled": True, "draft": False})
        self._require_current_account(account)
        # The worker and bridge re-read this authoritative store before each send.
        item = self.store.set_enabled(account, task_id, False, revision)
        return {"item": item, "message": "任务已暂停；已提交微信的消息不受影响。"}

    def delete(self, account: str, task_id: str, revision: int) -> dict[str, Any]:
        self._require_current_account(account)
        self.store.delete(account, task_id, revision)
        return {"deleted": True, "message": "任务已删除，重启后不会恢复。"}

    def _require_current_account(self, account: str) -> None:
        validate_account(account)
        if self.context()["account_id"] != account:
            raise ValueError("微信账号已变化或尚未确认，请刷新后重试。")

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        action = payload.get("action")
        arguments = normalize_arguments(action, payload.get("arguments", {}))
        if action == "text":
            return {"kind": "text", "text": arguments["text"]}
        health = self.health()
        if not health["runtime_ready"]:
            raise ValueError(health.get("message") or "内容服务尚未就绪。")
        if not payload.get("account_id") or health.get("account_id") != payload["account_id"]:
            raise ValueError("微信账号已变化，请重新读取任务。")
        result = self.engine.request("POST", _API + "preview", {**payload, "action": action, "arguments": arguments})
        if not isinstance(result, dict) or result.get("kind") not in {"text", "image", "link_card"}:
            raise PluginCenterError("内容预览暂时不可用。")
        return result

    def test(self, payload: dict[str, Any]) -> dict[str, Any]:
        target = payload.get("test_target")
        if not isinstance(target, dict):
            raise ValueError("请明确选择一个测试发送对象。")
        candidate = {
            **payload,
            "targets": [target],
            "schedule": {"kind": "daily", "time": "23:59", "timezone": "Asia/Shanghai"},
            "enabled": True,
            "draft": False,
        }
        task = normalize_task(candidate)
        self._verify_live(task, check_scheduled_time=False)
        result = self.engine.request("POST", _API + "test", {**payload, "test_target": target})
        if not isinstance(result, dict) or result.get("status") not in {"submitted", "failed", "unknown", "skipped"}:
            raise PluginCenterError("未收到测试发送结果，请检查执行记录和微信聊天。")
        return result

    def runs(self, account: str, task_id: str) -> dict[str, Any]:
        validate_account(account)
        context = self.context()
        if context["account_id"] != account:
            raise ValueError("微信账号已变化，请重新读取任务。")
        names = {(item["type"], item["id"]): item["name"] for item in context["groups"] + context["contacts"]}
        runs = self.store.runs(account, task_id)
        for run in runs:
            for result in run["deliveries"]:
                result["name"] = names.get((result["type"], result["id"]), "对象待确认")
        return {"items": runs}


__all__ = ["ScheduledTaskService"]
