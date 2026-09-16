from __future__ import annotations

import queue
import threading
from typing import Callable

from .ui_theme import COLORS, apply_product_theme


UninstallOperation = Callable[[Callable[[str], None]], None]


class UninstallProgressWindow:
    """Keep the independent uninstaller responsive while files are removed."""

    def __init__(self, root, operation: UninstallOperation, *, close_on_success: bool = False):
        import tkinter as tk
        from tkinter import ttk

        self.root = root
        self.operation = operation
        self.close_on_success = close_on_success
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.error: Exception | None = None
        self.busy = True
        root.title("微信机器人 · 正在卸载")
        root.geometry("600x300")
        root.minsize(540, 300)
        root.configure(background=COLORS.canvas)
        root.protocol("WM_DELETE_WINDOW", self.close)
        apply_product_theme(root, ttk)

        shell = ttk.Frame(root, style="App.TFrame", padding=(28, 24))
        shell.pack(fill="both", expand=True)
        self.heading = ttk.Label(shell, text="正在卸载微信机器人", style="PageTitle.TLabel")
        self.heading.pack(anchor="w")
        self.status = tk.StringVar(value="正在准备卸载，请稍候…")
        ttk.Label(
            shell, textvariable=self.status, style="PageHint.TLabel",
            wraplength=480, justify="left",
        ).pack(fill="x", pady=(18, 12))
        # Directory deletion has no reliable total. Show activity and the real
        # stage rather than an invented percentage or an apparent stalled bar.
        self.progress = ttk.Progressbar(shell, mode="indeterminate", maximum=100)
        self.progress.pack(fill="x")
        self.progress.start(20)
        self.hint = ttk.Label(
            shell, text="正在清理文件，请保持此窗口打开。", style="PageHint.TLabel",
            wraplength=480, justify="left",
        )
        self.hint.pack(fill="x", pady=(12, 0))
        self.close_button = ttk.Button(
            shell, text="关闭", command=self.close, state="disabled",
            style="Secondary.TButton", cursor="arrow",
        )
        self.close_button.pack(side="bottom", anchor="e", pady=(18, 0))
        root.bind("<Escape>", lambda _event: self.close())
        root.update_idletasks()
        root.geometry(
            f"+{max(0, (root.winfo_screenwidth() - root.winfo_width()) // 2)}"
            f"+{max(0, (root.winfo_screenheight() - root.winfo_height()) // 2)}"
        )
        root.lift()
        root.attributes("-topmost", True)
        root.after(500, lambda: root.attributes("-topmost", False))
        root.after(50, self.start)

    def start(self) -> None:
        threading.Thread(target=self._work, name="wechatrebot-uninstall", daemon=True).start()
        self.root.after(50, self._poll)

    def _work(self) -> None:
        try:
            self.operation(lambda message: self.events.put(("progress", message)))
        except Exception as exc:
            self.events.put(("error", exc))
        else:
            self.events.put(("done", None))

    def _poll(self) -> None:
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "progress":
                self.status.set(str(value))
            elif kind == "error":
                self.error = value
                self.busy = False
                self.progress.stop()
                self.root.destroy()
                return
            else:
                self.busy = False
                self.progress.stop()
                if self.close_on_success:
                    self.root.destroy()
                    return
                self.root.title("微信机器人 · 卸载完成")
                self.heading.configure(text="卸载完成")
                self.status.set("微信机器人及其本机数据和残留文件已全部清理。")
                self.progress.configure(mode="determinate", value=100)
                self.hint.configure(text="可以关闭此窗口。")
                self.close_button.configure(state="normal", cursor="hand2")
                self.close_button.focus_set()
                self.root.lift()
                return
        self.root.after(50, self._poll)

    def close(self) -> None:
        if not self.busy:
            self.root.destroy()


def run_uninstall_progress(operation: UninstallOperation, *, close_on_success: bool = False) -> None:
    import tkinter as tk

    root = tk.Tk()
    try:
        window = UninstallProgressWindow(root, operation, close_on_success=close_on_success)
        root.mainloop()
        if window.error is not None:
            raise window.error
    finally:
        try:
            root.destroy()
        except tk.TclError:
            pass
