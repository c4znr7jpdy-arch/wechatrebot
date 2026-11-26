from __future__ import annotations

import queue
import threading
from typing import Callable

from .ui_theme import COLORS, apply_product_theme
from .update_errors import UpdateFailure


ProgressCallback = Callable[[str, int, int], None]
UpdateOperation = Callable[[ProgressCallback], int]

_PHASE_LABELS = {
    "waiting": "正在等待旧版退出",
    "stopping_services": "正在关闭机器人及当前连接的微信",
    "verify": "正在校验更新文件",
    "checking_processes": "正在确认待更新文件已释放",
    "runtime_check": "正在检查可复用的运行环境",
    "runtime_download": "正在下载运行环境",
    "prepare": "正在准备安装文件",
    "backup": "正在检查原版本并准备恢复文件",
    "validate": "正在检查安装路径",
    "recheck": "正在复核安装文件",
    "install": "正在覆盖安装",
    "remove": "正在清理已停用的程序文件",
    "restarting": "正在打开新版客户端",
    "awaiting_startup": "正在等待新版窗口就绪",
    "rolling_back": "更新未完成，正在恢复原版本",
    "cleanup": "新版已就绪，正在清理旧文件",
}


class UpdateProgressWindow:
    """Run one automatic update without wizard pages or additional approval."""

    def __init__(self, root, operation: UpdateOperation) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.operation = operation
        self.events: queue.SimpleQueue[tuple[int, Exception | None]] = queue.SimpleQueue()
        self._progress_lock = threading.Lock()
        self._pending_progress: tuple[str, int, int] | None = None
        self._started = False
        self._indeterminate = True
        self.busy = True
        self.result = 1
        self.error: Exception | None = None

        root.title("微信机器人 · 自动更新")
        root.geometry("620x320")
        root.minsize(560, 320)
        root.configure(background=COLORS.canvas)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.bind("<Escape>", lambda _event: self.close())
        apply_product_theme(root, ttk)

        shell = ttk.Frame(root, style="App.TFrame", padding=(28, 24))
        shell.pack(fill="both", expand=True)
        self.heading = ttk.Label(shell, text="正在自动更新", style="PageTitle.TLabel")
        self.heading.pack(anchor="w")
        self.status = tk.StringVar(value="正在连接更新任务，请稍候…")
        ttk.Label(
            shell, textvariable=self.status, style="PageHint.TLabel",
            wraplength=520, justify="left",
        ).pack(fill="x", pady=(18, 12))
        self.progress = ttk.Progressbar(shell, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x")
        self.progress.start(20)
        self.hint = ttk.Label(
            shell, text="安装会自动完成并打开新版客户端，请保持此窗口打开。",
            style="PageHint.TLabel", wraplength=520, justify="left",
        )
        self.hint.pack(fill="x", pady=(12, 0))
        # There is no next/install/finish action. Only a failed operation reveals
        # a close button, after the transaction has stopped changing files.
        self.close_button = ttk.Button(
            shell, text="关闭", command=self.close, style="Secondary.TButton",
        )
        root.update_idletasks()
        root.geometry(
            f"+{max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)}"
            f"+{max(0, (root.winfo_screenheight() - root.winfo_height()) // 2)}"
        )
        root.lift()

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        # A closing UI must never terminate an active file transaction.
        threading.Thread(target=self._work, name="wechatrebot-update", daemon=False).start()
        self.root.after(50, self._poll)

    def _report_progress(self, phase: str, completed: int, total: int) -> None:
        # Tens of thousands of file callbacks must not fill an unbounded UI
        # queue. Only the latest progress matters; the result is never dropped.
        with self._progress_lock:
            self._pending_progress = (phase, completed, total)

    def _work(self) -> None:
        try:
            result = self.operation(self._report_progress)
            if type(result) is not int:
                raise RuntimeError("更新任务未返回有效结果，请查看更新日志。")
        except Exception as exc:
            self.events.put((1, exc))
        else:
            self.events.put((result, None))

    def _display_progress(self, phase: str, completed: int, total: int) -> None:
        message = _PHASE_LABELS.get(phase, "正在处理更新")
        determinate = total > 0
        if determinate:
            completed = min(max(completed, 0), total)
            if self._indeterminate:
                self.progress.stop()
            self.progress.configure(mode="determinate", value=completed * 100 / total)
            message += f" · {completed} / {total}"
        elif not self._indeterminate:
            self.progress.configure(mode="indeterminate", value=0)
            self.progress.start(20)
        self._indeterminate = not determinate
        self.status.set(message)

    def _poll(self) -> None:
        with self._progress_lock:
            progress, self._pending_progress = self._pending_progress, None
        if progress is not None:
            self._display_progress(*progress)
        try:
            result, error = self.events.get_nowait()
        except queue.Empty:
            self.root.after(50, self._poll)
            return
        self.result, self.error, self.busy = result, error, False
        self.progress.stop()
        if result == 0:
            self.root.destroy()
            return
        self.root.title("微信机器人 · 更新未完成")
        self.heading.configure(text="更新未完成")
        message = (
            error.to_public_dict()["message"] if isinstance(error, UpdateFailure)
            else str(error).strip() if error is not None else ""
        )
        self.status.set(message or "更新任务未能完成，请查看更新结果后重试。")
        self.hint.configure(text="请按上方提示处理。关闭此窗口不会再次安装。")
        self.progress.configure(mode="determinate", value=0)
        self.close_button.pack(side="bottom", anchor="e", pady=(18, 0))
        self.close_button.focus_set()
        self.root.lift()

    def close(self) -> None:
        if not self.busy:
            self.root.destroy()


def run_update_progress(
    operation: UpdateOperation, *, on_ready: Callable[[], None] | None = None,
) -> int:
    """Show the window before acknowledging handoff or running any mutation.

    The operation returns zero only after startup confirmation and commit. It
    should raise with a public failure/recovery message for a useful error UI.
    UI construction and ready-handshake errors propagate without installation.
    """
    import tkinter as tk

    root = tk.Tk()
    try:
        window = UpdateProgressWindow(root, operation)
        if not root.winfo_viewable():
            # Do not leave the initiating manager waiting forever if the
            # desktop cannot map the independent window.
            visibility_timeout = root.after(10_000, root.destroy)
            try:
                root.wait_visibility()
            finally:
                try:
                    root.after_cancel(visibility_timeout)
                except tk.TclError:
                    pass
        if on_ready is not None:
            on_ready()
        window.start()
        root.mainloop()
        return window.result
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
