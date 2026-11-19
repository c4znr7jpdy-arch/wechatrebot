"""Two-phase handoff from the running manager to its visible installer."""

import json
import os
import subprocess
from pathlib import Path
import time

from .json_io import write_json_atomic
from .update_errors import UpdateFailure


class HandoffCancelled(RuntimeError):
    pass


def _signal_path(plan_path: Path, update_id: str, kind: str) -> Path:
    if len(update_id) != 32 or any(char not in "0123456789abcdef" for char in update_id):
        raise ValueError("更新交接编号无效。")
    return plan_path.parent / f"handoff-{update_id}-{kind}.json"


def _matches(path: Path, update_id: str) -> bool:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return isinstance(value, dict) and value.get("update_id") == update_id
    except (OSError, ValueError):
        return False


def signal_handoff(plan_path: Path, update_id: str, kind: str) -> None:
    write_json_atomic(_signal_path(plan_path, update_id, kind), {
        "update_id": update_id, "pid": os.getpid(),
    })


def wait_for_installer(
    plan_path: Path, update_id: str, process, *, timeout: float = 45, signal: str = "ready",
) -> None:
    deadline = time.monotonic() + timeout
    ready = _signal_path(plan_path, update_id, signal)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            break
        if _matches(ready, update_id):
            return
        time.sleep(0.1)
    raise UpdateFailure("UPD-1007", public_message="更新进度窗口未能接管，原版本仍保留。请重试更新。")


def commit_handoff(plan_path: Path, update_id: str, process) -> None:
    if process.poll() is not None:
        raise UpdateFailure("UPD-1007", public_message="更新窗口已退出，原版本仍保留，请重试更新。")
    signal_handoff(plan_path, update_id, "commit")
    wait_for_installer(plan_path, update_id, process, timeout=10, signal="committed")


def wait_for_commit(plan_path: Path, update_id: str, *, timeout: float = 120) -> None:
    """Called only after window drawing, plan validation and helper locking."""
    signal_handoff(plan_path, update_id, "ready")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        check_cancelled(plan_path, update_id)
        if _matches(_signal_path(plan_path, update_id, "commit"), update_id):
            signal_handoff(plan_path, update_id, "committed")
            return
        time.sleep(0.1)
    raise HandoffCancelled("未收到旧版客户端的安装许可，本次更新已取消。")


def check_cancelled(plan_path: Path, update_id: str) -> None:
    if _matches(_signal_path(plan_path, update_id, "cancel"), update_id):
        raise HandoffCancelled("旧版客户端已取消本次交接。")


def cancel_handoff(plan_path: Path, update_id: str, process) -> None:
    signal_handoff(plan_path, update_id, "cancel")
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        raise UpdateFailure("UPD-1007", public_message="更新窗口仍在退出，请等待窗口关闭后重试。")


def run_automatic_update(plan_path: Path) -> int:
    from .update_progress import run_update_progress
    from .updates import UPDATE_RESULT_FILE, _read_json_object, _validated_plan, apply_update_plan

    plan, root, _payload, _target = _validated_plan(plan_path)
    if plan.get("automatic_install") != 1:
        raise UpdateFailure("UPD-1003", "更新计划不支持自动安装交接。")

    def operation(progress):
        code = apply_update_plan(
            plan_path, progress_callback=progress,
            before_wait=lambda: wait_for_commit(plan_path, str(plan["update_id"])),
        )
        if code:
            if code == 2:
                raise RuntimeError("已有其他更新任务正在处理，请等待该任务完成。")
            try:
                result = _read_json_object(root / "updates" / UPDATE_RESULT_FILE)
                if result.get("update_id") != plan["update_id"]:
                    raise RuntimeError("更新结果不属于当前任务。")
                message = str(result.get("message") or "更新未完成，请查看更新日志。")
                if result.get("recovery_status") == "restored":
                    message = str(result.get("title") or "更新未完成") + "；原版本文件已恢复。"
            except RuntimeError:
                message = "更新未完成，请查看更新日志。"
            raise RuntimeError(message)
        return code

    return run_update_progress(operation)
