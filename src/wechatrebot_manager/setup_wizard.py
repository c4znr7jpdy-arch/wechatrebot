from __future__ import annotations

import os
from pathlib import Path
import queue
import threading
import subprocess

from .brand import BrandProfile
from .install_paths import validate_install_path
from .installer import InstallRequest, perform_install
from .provider import OpenAIProviderProfile


PROVIDER_DEEPSEEK = "DeepSeek V4 Flash（推荐）"
PROVIDER_CUSTOM = "自定义 OpenAI 兼容"
PROVIDER_LATER = "稍后在管理后台配置"

DEFAULT_BRAND = BrandProfile(
    display_name="小妹",
    short_name="小妹",
    persona_name="小妹",
)


def default_install_root() -> Path:
    """Return a writable per-user default that can still be changed in the wizard."""
    local_app_data = os.environ.get("LOCALAPPDATA")
    base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
    return (base / "XiaomeiRobot").expanduser().resolve()


def provider_profile_for_choice(
    choice: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
) -> OpenAIProviderProfile | None:
    """Keep the legacy helper for CLI/backward compatibility.

    Provider credentials are no longer requested by the installer. Customers set
    them from the installed application's settings page instead.
    """
    if choice == PROVIDER_LATER:
        return None
    return OpenAIProviderProfile(
        api_key=api_key,
        api_base=api_base,
        model=model,
    ).normalized()


class SetupWizard:
    """A deliberately short installer: choose a directory, confirm, install."""

    def __init__(
        self,
        root,
        *,
        payload_root: Path | None,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.payload_root = payload_root
        self.page = 0
        self.installing = False
        self.events: queue.Queue[tuple[object, ...]] = queue.Queue()

        root.title("小妹安装向导")
        root.geometry("760x520")
        root.minsize(700, 500)
        root.configure(background="#f4f6fa")
        root.protocol("WM_DELETE_WINDOW", self._close)

        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Step.TLabel", foreground="#4169e1")
        style.configure("Hint.TLabel", foreground="#667085")
        style.configure("Primary.TButton", font=("Microsoft YaHei UI", 10, "bold"))

        header = ttk.Frame(root, padding=(32, 24, 32, 12))
        header.pack(fill="x")
        ttk.Label(header, text="小妹 · 商业版", style="Title.TLabel").pack(
            anchor="w"
        )
        self.step_label = ttk.Label(header, style="Step.TLabel")
        self.step_label.pack(anchor="w", pady=(6, 0))

        self.body = ttk.Frame(root, padding=(32, 16))
        self.body.pack(fill="both", expand=True)

        footer = ttk.Frame(root, padding=(32, 14, 32, 24))
        footer.pack(fill="x")
        self.cancel_button = ttk.Button(footer, text="取消", command=self._close)
        self.cancel_button.pack(side="left")
        self.next_button = ttk.Button(
            footer,
            text="下一步",
            command=self._next,
            style="Primary.TButton",
        )
        self.next_button.pack(side="right")
        self.back_button = ttk.Button(footer, text="上一步", command=self._back)
        self.back_button.pack(side="right", padx=(0, 10))

        self.install_var = tk.StringVar(value=str(default_install_root()))
        self.launch_after_install_var = tk.BooleanVar(value=True)
        self.progress_var = tk.DoubleVar(value=0)
        self.progress_text_var = tk.StringVar(value="等待开始")
        self._show_page()

    def _clear_body(self) -> None:
        for child in self.body.winfo_children():
            child.destroy()

    def _heading(self, title: str, description: str) -> None:
        self.ttk.Label(
            self.body,
            text=title,
            font=("Microsoft YaHei UI", 15, "bold"),
        ).pack(anchor="w")
        self.ttk.Label(
            self.body,
            text=description,
            style="Hint.TLabel",
            wraplength=660,
            justify="left",
        ).pack(anchor="w", pady=(8, 24))

    def _show_page(self) -> None:
        self._clear_body()
        self.step_label.configure(text=f"步骤 {self.page + 1} / 2")
        self.back_button.configure(state="disabled" if self.page == 0 else "normal")
        self.next_button.configure(text="安装" if self.page == 1 else "下一步")
        if self.page == 0:
            self._page_install_root()
        else:
            self._page_summary()

    def _page_install_root(self) -> None:
        self._heading(
            "选择安装位置",
            "选择一个本机目录即可。安装程序会自动准备运行环境，不会捆绑或修改微信客户端。",
        )
        row = self.ttk.Frame(self.body)
        row.pack(fill="x")
        self.ttk.Entry(row, textvariable=self.install_var).pack(
            side="left", fill="x", expand=True
        )
        self.ttk.Button(row, text="浏览…", command=self._browse_install).pack(
            side="left", padx=(10, 0)
        )
        self.ttk.Label(
            self.body,
            text="已为您填写推荐目录，可直接点击“下一步”；如需更改，点击“浏览”。至少保留 5 GiB 空间。",
            style="Hint.TLabel",
            wraplength=660,
        ).pack(anchor="w", pady=(12, 0))

    def _page_summary(self) -> None:
        self._heading(
            "准备安装",
            "点击“安装”后会自动完成程序和运行环境部署。微信与机器人个性化设置将在首次打开软件时引导完成。",
        )
        box = self.ttk.LabelFrame(self.body, text="安装摘要", padding=16)
        box.pack(fill="x")
        values = (
            ("安装目录", self.install_var.get()),
            ("微信客户端", "不包含，首次启动时自动检测"),
            ("机器人名称", "安装后可一键修改"),
            ("运行环境", "自动安装和配置"),
        )
        for label, value in values:
            row = self.ttk.Frame(box)
            row.pack(fill="x", pady=5)
            self.ttk.Label(row, text=label, width=14).pack(side="left", anchor="n")
            self.ttk.Label(row, text=value, wraplength=520, justify="left").pack(
                side="left", anchor="w"
            )

    def _browse_install(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askdirectory(
            parent=self.root,
            title="选择机器人安装目录",
            mustexist=False,
        )
        if selected:
            self.install_var.set(selected)

    def _validate_page(self) -> bool:
        from tkinter import messagebox

        if self.page != 0:
            return True
        request_errors = self._request().validate()
        if request_errors:
            messagebox.showerror(
                "无法继续", "\n".join(request_errors), parent=self.root
            )
            return False
        self.install_var.set(
            str(validate_install_path(self.install_var.get()).path)
        )
        return True

    def _next(self) -> None:
        if self.installing or not self._validate_page():
            return
        if self.page == 0:
            self.page = 1
            self._show_page()
            return
        self._start_install()

    def _back(self) -> None:
        if not self.installing and self.page > 0:
            self.page -= 1
            self._show_page()

    def _request(self) -> InstallRequest:
        return InstallRequest(
            install_root=Path(self.install_var.get()),
            payload_root=self.payload_root,
            brand=DEFAULT_BRAND,
        )

    def _start_install(self) -> None:
        from tkinter import messagebox

        request = self._request()
        errors = request.validate()
        if errors:
            messagebox.showerror("无法安装", "\n".join(errors), parent=self.root)
            return
        self.installing = True
        self._clear_body()
        self._heading("正在安装", "请保持窗口开启，安装过程不会自动启动微信或机器人。")
        self.ttk.Progressbar(
            self.body,
            variable=self.progress_var,
            maximum=100,
        ).pack(fill="x", pady=(20, 12))
        self.ttk.Label(self.body, textvariable=self.progress_text_var).pack(anchor="w")
        self.back_button.configure(state="disabled")
        self.next_button.configure(state="disabled")
        self.cancel_button.configure(state="disabled")

        def worker() -> None:
            try:
                perform_install(
                    request,
                    progress_callback=lambda phase, current, total: self.events.put(
                        ("progress", phase, current, total)
                    ),
                )
                self.events.put(("done", request.install_root.resolve()))
            except Exception as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(100, self._poll_events)

    def _poll_events(self) -> None:
        from tkinter import messagebox

        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == "progress":
                    _, phase, current, total = event
                    self._show_progress(str(phase), int(current), int(total))
                elif event[0] == "done":
                    self._show_complete(Path(event[1]))
                    return
                else:
                    self.installing = False
                    messagebox.showerror("安装失败", str(event[1]), parent=self.root)
                    self.page = 1
                    self._show_page()
                    self.cancel_button.configure(state="normal")
                    return
        except queue.Empty:
            pass
        if self.installing:
            self.root.after(100, self._poll_events)

    def _show_progress(self, phase: str, current: int, total: int) -> None:
        labels = {
            "prepare": "正在检查并准备程序",
            "install": "正在写入程序文件",
            "configure": "正在生成初始配置",
        }
        ranges = {
            "prepare": (0.0, 65.0),
            "install": (65.0, 95.0),
            "configure": (95.0, 100.0),
        }
        start, end = ranges.get(phase, (0.0, 100.0))
        ratio = current / max(total, 1)
        self.progress_var.set(start + (end - start) * ratio)
        percent = min(100, max(0, round(ratio * 100)))
        self.progress_text_var.set(f"{labels.get(phase, phase)}：{percent}%")

    def _show_complete(self, install_root: Path) -> None:
        self.installing = False
        self._clear_body()
        self.step_label.configure(text="安装完成")
        self._heading(
            "安装已完成",
            "打开软件后会自动检查微信。未安装或版本不支持时，页面会给出处理方式，并可随时重新检测。",
        )
        self.ttk.Label(
            self.body,
            text=str(install_root),
            font=("Consolas", 11),
            wraplength=660,
        ).pack(anchor="w", pady=(8, 0))
        self.ttk.Checkbutton(
            self.body,
            text="完成后立即打开机器人管理软件",
            variable=self.launch_after_install_var,
        ).pack(anchor="w", pady=(24, 0))
        self.progress_var.set(100)
        self.back_button.configure(state="disabled")
        self.next_button.configure(
            text="完成",
            state="normal",
            command=lambda: self._finish(install_root),
        )
        self.cancel_button.configure(state="disabled")

    def _finish(self, install_root: Path) -> None:
        if self.launch_after_install_var.get():
            executable = install_root / "manager" / "WeChatReBot.exe"
            try:
                subprocess.Popen(
                    [str(executable), "gui", "--install-root", str(install_root)],
                    cwd=install_root,
                    close_fds=True,
                )
            except OSError as exc:
                from tkinter import messagebox

                messagebox.showwarning(
                    "安装完成",
                    f"软件已安装，但暂时无法自动打开：{exc}\n您可以从桌面快捷方式打开。",
                    parent=self.root,
                )
        self.root.destroy()

    def _close(self) -> None:
        if self.installing:
            return
        self.root.destroy()


def run_setup_wizard(
    payload_root: Path | None,
) -> int:
    if os.name != "nt":
        return 2
    import tkinter as tk

    root = tk.Tk()
    SetupWizard(
        root,
        payload_root=payload_root,
    )
    root.mainloop()
    return 0


__all__ = [
    "DEFAULT_BRAND",
    "PROVIDER_CUSTOM",
    "PROVIDER_DEEPSEEK",
    "PROVIDER_LATER",
    "SetupWizard",
    "default_install_root",
    "provider_profile_for_choice",
    "run_setup_wizard",
]
