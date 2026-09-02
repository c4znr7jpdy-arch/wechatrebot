from __future__ import annotations

from typing import Callable

from .ui_theme import COLORS
from .updates import UpdateRelease


class UpdateDialog:
    def __init__(
        self,
        parent,
        release: UpdateRelease,
        *,
        current_version: str,
        on_install: Callable[[UpdateRelease], None],
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.release = release
        self.on_install = on_install
        self.busy = False
        self.window = tk.Toplevel(parent)
        self.window.title("软件更新")
        self.window.geometry("610x510")
        self.window.minsize(560, 460)
        self.window.configure(background=COLORS.canvas)
        self.window.transient(parent)
        self.window.protocol("WM_DELETE_WINDOW", self.close)

        shell = ttk.Frame(self.window, style="App.TFrame", padding=(28, 24))
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text="发现新版本", style="PageTitle.TLabel").pack(anchor="w")
        ttk.Label(
            shell,
            text="更新包已通过发布签名验证，点击后会自动下载、校验、安装并重新打开。",
            style="PageHint.TLabel",
            wraplength=540,
            justify="left",
        ).pack(anchor="w", pady=(8, 18))

        summary = ttk.Frame(shell, style="Surface.TFrame", padding=18)
        summary.pack(fill="x")
        ttk.Label(
            summary,
            text=release.title,
            style="Section.TLabel",
        ).pack(anchor="w")
        version_text = f"当前版本 v{current_version}    新版本 v{release.version}"
        ttk.Label(
            summary, text=version_text, style="SurfaceMuted.TLabel"
        ).pack(anchor="w", pady=(8, 0))
        size_mb = release.package_size / 1024**2
        detail = f"安装包 {size_mb:.1f} MB · 数字签名和 SHA-256 双重校验"
        if release.mandatory:
            detail += " · 建议立即更新"
        ttk.Label(
            summary, text=detail, style="SurfaceMuted.TLabel"
        ).pack(anchor="w", pady=(4, 0))

        ttk.Label(shell, text="本次更新", style="PageHint.TLabel").pack(
            anchor="w", pady=(20, 8)
        )
        notes = tk.Text(
            shell,
            height=6,
            background=COLORS.surface,
            foreground=COLORS.text,
            font=("Microsoft YaHei UI", 10),
            relief="solid",
            borderwidth=1,
            highlightthickness=0,
            wrap="word",
            padx=12,
            pady=10,
            cursor="arrow",
        )
        notes.pack(fill="both", expand=True)
        lines = release.notes or ("性能与稳定性改进",)
        notes.insert("1.0", "\n".join(f"• {item}" for item in lines))
        notes.configure(state="disabled")

        self.status_var = tk.StringVar(value="准备就绪")
        self.status_label = ttk.Label(
            shell,
            textvariable=self.status_var,
            style="SurfaceMuted.TLabel",
            wraplength=540,
        )
        self.status_label.pack(anchor="w", pady=(14, 6))
        self.progress = ttk.Progressbar(shell, maximum=100, mode="determinate")
        self.progress.pack(fill="x")

        actions = ttk.Frame(shell, style="App.TFrame")
        actions.pack(fill="x", pady=(18, 0))
        self.cancel_button = ttk.Button(
            actions,
            text="暂不更新",
            command=self.close,
            style="Secondary.TButton",
        )
        self.cancel_button.pack(side="left")
        self.install_button = ttk.Button(
            actions,
            text="立即更新",
            command=self._install,
            style="Primary.TButton",
        )
        self.install_button.pack(side="right")

        self.window.update_idletasks()
        x = parent.winfo_rootx() + max(
            0, (parent.winfo_width() - self.window.winfo_width()) // 2
        )
        y = parent.winfo_rooty() + max(
            0, (parent.winfo_height() - self.window.winfo_height()) // 2
        )
        self.window.geometry(f"+{x}+{y}")
        self.install_button.focus_set()
        self.window.grab_set()

    def exists(self) -> bool:
        try:
            return bool(self.window.winfo_exists())
        except self.tk.TclError:
            return False

    def lift(self) -> None:
        if self.exists():
            self.window.deiconify()
            self.window.lift()
            self.install_button.focus_set()

    def close(self) -> None:
        if self.busy or not self.exists():
            return
        try:
            self.window.grab_release()
        except self.tk.TclError:
            pass
        self.window.destroy()

    def _install(self) -> None:
        if self.busy:
            return
        self.on_install(self.release)

    def set_preparing(self, message: str = "正在准备更新…") -> None:
        self.busy = True
        self.status_var.set(message)
        self.progress.configure(value=2)
        self.install_button.configure(state="disabled", text="正在更新…")
        self.cancel_button.configure(state="disabled")

    def set_progress(self, stage: str, current: int, total: int) -> None:
        total = max(total, 1)
        ratio = min(max(current / total, 0), 1)
        if stage == "download":
            value = 5 + ratio * 55
            message = f"正在下载更新包… {ratio * 100:.0f}%"
        elif stage == "extract":
            value = 60 + ratio * 18
            message = "正在安全展开更新包…"
        elif stage == "verify":
            value = 78 + ratio * 20
            message = "正在校验更新文件…"
        else:
            value = 2
            message = "正在准备更新…"
        self.progress.configure(value=value)
        self.status_var.set(message)

    def set_handoff(self) -> None:
        self.busy = True
        self.progress.configure(value=100)
        self.status_var.set("校验完成，正在安装并重新打开…")

    def set_error(self, message: str) -> None:
        self.busy = False
        self.status_var.set(f"更新未完成：{message}。请检查网络后重试。")
        self.progress.configure(value=0)
        self.install_button.configure(state="normal", text="重试")
        self.cancel_button.configure(state="normal")


__all__ = ["UpdateDialog"]
