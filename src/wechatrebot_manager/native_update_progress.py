"""Inno display adapter; file transactions remain in the existing updater."""

import os
import logging
from pathlib import Path
import subprocess
import tempfile
import time
import uuid

from .json_io import write_text_atomic
from .update_errors import UpdateFailure
from .update_progress import UpdateOperation, _PHASE_LABELS


def native_progress_executable() -> Path:
    return Path(__file__).with_name("WeChatReBot-UpdateUI.exe")


class NativeProgress:
    def __init__(self, path: Path, session: str) -> None:
        self.path, self.session = path, session
        self.phase = ""
        self.last_write = 0.0

    def write(self, state: str, percent: int, message: str, detail: str) -> None:
        lines = (self.session, state, str(percent), message, detail)
        write_text_atomic(self.path, "\n".join(
            str(line).replace("\r", " ").replace("\n", " ")[:1200] for line in lines
        ) + "\n")

    def report(self, phase: str, completed: int, total: int) -> None:
        now = time.monotonic()
        if phase == self.phase and not (total > 0 and completed == total) and now - self.last_write < 0.25:
            return
        self.phase, self.last_write = phase, now
        completed = min(max(completed, 0), total) if total > 0 else 0
        percent = completed * 100 // total if total > 0 else -1
        self.write("running", percent, _PHASE_LABELS.get(phase, "正在处理更新"),
                   f"本阶段 {percent}% · 已处理 {completed} / {total} 个文件" if total > 0 else "请稍候…")


def _wait_for_visible_window(process, path: Path, session: str) -> None:
    deadline = time.monotonic() + 15
    ready = path.with_suffix(path.suffix + ".ready")
    while process.poll() is None and time.monotonic() < deadline:
        try:
            if ready.read_text(encoding="utf-8") == session:
                return
        except FileNotFoundError:
            pass
        time.sleep(0.05)
    raise UpdateFailure("UPD-1007", public_message="原生更新窗口未能就绪，原版本未更改。请重试更新。")


def run_native_update_progress(operation: UpdateOperation) -> int:
    executable = native_progress_executable()
    if not executable.is_file():
        raise UpdateFailure("UPD-1007", public_message="原生更新窗口组件缺失，请修复安装后重试。")
    with tempfile.TemporaryDirectory(prefix="wechatrebot-update-ui-") as temporary:
        status = NativeProgress(Path(temporary) / "progress.txt", uuid.uuid4().hex)
        status.report("waiting", 0, 0)
        process = subprocess.Popen(
            [str(executable), "/SP-", "/NORESTART", f"/STATUS={status.path}",
             f"/SESSION={status.session}", f"/OWNERPID={os.getpid()}"],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        try:
            _wait_for_visible_window(process, status.path, status.session)
        except Exception:
            # The display owns no installation files, and operation has not run.
            try:
                status.write("cancelled", 0, "更新已取消", " ")
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                process.terminate()
                process.wait(timeout=10)
            raise

        def report(phase: str, completed: int, total: int) -> None:
            try:
                status.report(phase, completed, total)
            except OSError:
                # A display/storage failure must not interrupt a live transaction.
                logging.getLogger(__name__).warning("Native update progress could not be written")

        result, error = 1, None
        try:
            result = operation(report)
            if type(result) is not int:
                raise RuntimeError("更新任务未返回有效结果，请查看更新日志。")
        except Exception as exc:
            error = exc
            result = 1
        message = (
            error.to_public_dict()["message"] if isinstance(error, UpdateFailure)
            else str(error).strip() if error else "更新未完成，请查看更新结果后重试。"
        )
        try:
            status.write("success" if result == 0 else "failed", 100 if result == 0 else 0,
                         "更新已完成" if result == 0 else message, " ")
        except OSError:
            # Exiting signals the display to show an unconfirmed outcome.
            logging.getLogger(__name__).warning("Native update outcome could not be written")
            return result
        if result == 0:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.terminate()
                process.wait(timeout=10)
        else:
            process.wait()
        return result
