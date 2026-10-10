"""Shared ownership and legacy configuration contract for group reports.

Copied unchanged into the schedule and group-analysis plugins during assembly.
Only an explicit save or confirmed migration claims the legacy producer.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import re
import sqlite3

from .task_store import TaskStore


ANALYSIS_ACTION = "group_analysis"
ANALYSIS_CONFIG = Path("app/astrbot/data/config/astrbot_plugin_qq_group_daily_analysis_config.json")
ANALYSIS_STORE = Path("data/manager/scheduled-tasks.db")


def analysis_install_root(module_path: str | Path) -> Path | None:
    for parent in Path(module_path).resolve().parents:
        if parent.name.casefold() == "astrbot" and parent.parent.name.casefold() == "app":
            return parent.parent.parent
    return None


def legacy_analysis_retired(module_path: str | Path) -> bool:
    root = analysis_install_root(module_path)
    if root is None:
        return False
    try:
        owner = TaskStore(root / ANALYSIS_STORE).action_owner(ANALYSIS_ACTION)
        return bool(owner and owner["phase"] in {"prepared", "complete"})
    except (OSError, ValueError, sqlite3.Error):
        return True  # Uncertain ownership must never reactivate the old producer.


def read_analysis_configuration(root: Path) -> tuple[dict, str]:
    try:
        raw = (root / ANALYSIS_CONFIG).read_bytes()
    except FileNotFoundError:
        return {}, ""
    try:
        config = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError):
        raise ValueError("群聊分析配置无法读取，请先检查原配置。") from None
    if not isinstance(config, dict):
        raise ValueError("群聊分析配置格式不正确。")
    return config, hashlib.sha256(raw).hexdigest()


def analysis_schedule_source(config: dict) -> dict:
    auto, basic = config.get("auto_analysis", {}), config.get("basic", {})
    if not isinstance(auto, dict) or not isinstance(basic, dict):
        raise ValueError("群聊分析定时配置格式不正确。")
    mode = str(auto.get("scheduled_group_list_mode", "whitelist")).lower()
    if mode not in {"whitelist", "blacklist", "inherit"}:
        raise ValueError("旧群聊分析名单模式不正确，请先检查原配置。")
    targets = auto.get("scheduled_group_list", [])
    if mode == "inherit":
        targets = basic.get("group_list", [])
    if not isinstance(targets, list) or any(not isinstance(item, str) for item in targets):
        raise ValueError("旧群聊分析名单格式不正确。")
    raw_times = auto.get("auto_analysis_time", ["09:00"])
    raw_times = [raw_times] if isinstance(raw_times, str) else raw_times
    if not isinstance(raw_times, list) or len(raw_times) > 24:
        raise ValueError("旧群聊分析时间格式不正确。")
    times = []
    for raw in raw_times:
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", str(raw).strip().replace("：", ":"))
        if not match or int(match[1]) > 23 or int(match[2]) > 59:
            raise ValueError("旧群聊分析时间格式不正确。")
        clock = f"{int(match[1]):02d}:{int(match[2]):02d}"
        if clock not in times:
            times.append(clock)
    target_ids = [item.strip().split(":GroupMessage:", 1)[-1] for item in targets if item.strip()]
    if mode == "whitelist":
        active = bool(target_ids)
    elif mode == "inherit":
        active = basic.get("group_list_mode", "none") != "whitelist" or bool(target_ids)
    else:
        active = True
    return {"mode": mode, "times": times, "target_ids": target_ids,
            "days": basic.get("analysis_days", 1), "enabled": active,
            "original_schedule": deepcopy(auto)}


def require_analysis_ownership(root: Path, store: TaskStore, account_id: str, *, claim: bool = False) -> None:
    owner = store.action_owner(ANALYSIS_ACTION)
    if owner:
        if owner["phase"] != "complete":
            raise ValueError("旧群聊分析计划迁移尚未完成，请先继续迁移。")
        return
    config, _revision = read_analysis_configuration(root)
    if analysis_schedule_source(config)["enabled"]:
        raise ValueError("旧群聊分析计划尚未迁入统一任务，请先预览并确认迁移。")
    if claim:
        store.claim_action(ANALYSIS_ACTION, account_id, {"schema_version": 1, "items": [], "source": {}})

