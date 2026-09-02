from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import threading
import webbrowser

from .brand import BrandProfile, install_brand_profile
from .diagnostics import collect_diagnostics
from .launcher import (
    ProcessSupervisor,
    ServiceLaunchError,
    build_service_specs,
    port_accepting,
)
from .runtime_config import write_runtime_configs
from .wechat_path import validate_wechat_executable


def load_brand_profile(install_root: str | Path) -> BrandProfile:
    root = Path(install_root).expanduser().resolve()
    payload = json.loads((root / "config" / "brand.json").read_text(encoding="utf-8-sig"))
    return BrandProfile(
        display_name=str(payload.get("display_name", "智能机器人")),
        short_name=str(payload.get("short_name", "机器人")),
        persona_name=str(payload.get("persona_name", "智能机器人")),
        avatar_path=str(payload.get("avatar_path", "")),
        welcome_text=str(payload.get("welcome_text", "")),
        help_title=str(payload.get("help_title", "")),
    ).normalized()


def apply_brand_change(install_root: str | Path, profile: BrandProfile) -> BrandProfile:
    root = Path(install_root).expanduser().resolve()
    manager = json.loads(
        (root / "config" / "manager.json").read_text(encoding="utf-8-sig")
    )
    wechat_path = Path(str(manager["wechat_exe_path"]))
    normalized = install_brand_profile(root, profile)
    write_runtime_configs(
        root,
        wechat_path,
        wechat_version=manager.get("wechat_version"),
        brand_profile=normalized,
    )
    return normalized


def apply_wechat_change(install_root: str | Path, wechat_path: str | Path) -> None:
    root = Path(install_root).expanduser().resolve()
    item = validate_wechat_executable(wechat_path)
    if not item.supported:
        raise ValueError("；".join(item.errors))
    write_runtime_configs(
        root,
        item.path,
        wechat_version=item.version,
        brand_profile=load_brand_profile(root),
    )


class ControlCenter:
    def __init__(self, root, install_root: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.install_root = install_root.expanduser().resolve()
        self.brand = load_brand_profile(self.install_root)
        self.supervisor: ProcessSupervisor | None = None
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()

        root.title(f"{self.brand.display_name} · 机器人管理器")
        root.geometry("760x520")
        root.minsize(700, 480)
        root.configure(background="#f4f6fa")
        root.protocol("WM_DELETE_WINDOW", self._close)

        style = ttk.Style(root)
        try:
            style.theme_use("vista")
        except tk.TclError:
            pass
        style.configure("ManagerTitle.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Status.TLabel", font=("Microsoft YaHei UI", 12, "bold"))
        style.configure("Hint.TLabel", foreground="#667085")

        shell = ttk.Frame(root, padding=28)
        shell.pack(fill="both", expand=True)
        ttk.Label(shell, text=self.brand.display_name, style="ManagerTitle.TLabel").pack(
            anchor="w"
        )
        ttk.Label(
            shell,
            text=str(self.install_root),
            style="Hint.TLabel",
            wraplength=680,
        ).pack(anchor="w", pady=(4, 20))

        status_box = ttk.LabelFrame(shell, text="运行状态", padding=18)
        status_box.pack(fill="x")
        self.status_var = tk.StringVar()
        ttk.Label(status_box, textvariable=self.status_var, style="Status.TLabel").pack(
            anchor="w"
        )
        self.detail_var = tk.StringVar()
        ttk.Label(status_box, textvariable=self.detail_var, style="Hint.TLabel").pack(
            anchor="w", pady=(6, 0)
        )

        actions = ttk.Frame(shell)
        actions.pack(fill="x", pady=20)
        self.start_button = ttk.Button(actions, text="启动机器人", command=self._start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止机器人", command=self._stop)
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="打开 AstrBot 后台", command=self._open_dashboard).pack(
            side="left", padx=8
        )

        settings = ttk.LabelFrame(shell, text="常用设置", padding=14)
        settings.pack(fill="x")
        ttk.Button(settings, text="一键修改机器人名称", command=self._edit_brand).pack(
            side="left"
        )
        ttk.Button(settings, text="重新选择微信", command=self._select_wechat).pack(
            side="left", padx=8
        )
        ttk.Button(settings, text="导出脱敏诊断", command=self._export_diagnostics).pack(
            side="left", padx=8
        )
        ttk.Button(settings, text="打开安装目录", command=self._open_install_root).pack(
            side="left", padx=8
        )

        self._refresh_status()
        self.root.after(1000, self._timer)

    def _refresh_status(self) -> None:
        astrbot = port_accepting("127.0.0.1", 6200)
        bridge = port_accepting("127.0.0.1", 5000)
        if astrbot and bridge:
            self.status_var.set("机器人正在运行")
            self.detail_var.set("AstrBot 与新 HOOK 桥接均已就绪")
        elif astrbot:
            self.status_var.set("正在启动或桥接异常")
            self.detail_var.set("AstrBot 已就绪，新 HOOK 桥接尚未就绪")
        else:
            self.status_var.set("机器人未运行")
            self.detail_var.set("点击“启动机器人”按正确顺序启动全部服务")
        self.start_button.configure(state="disabled" if astrbot or bridge else "normal")
        owned = self.supervisor is not None and bool(self.supervisor.running)
        self.stop_button.configure(state="normal" if owned else "disabled")

    def _timer(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "error":
                    from tkinter import messagebox

                    messagebox.showerror("操作失败", message, parent=self.root)
                self._refresh_status()
        except queue.Empty:
            pass
        self._refresh_status()
        self.root.after(1000, self._timer)

    def _start(self) -> None:
        if self.supervisor and self.supervisor.running:
            return
        self.start_button.configure(state="disabled")
        self.status_var.set("正在启动…")
        try:
            self.supervisor = ProcessSupervisor(build_service_specs(self.install_root))
        except (OSError, ValueError) as exc:
            from tkinter import messagebox

            messagebox.showerror("启动前检查失败", str(exc), parent=self.root)
            self.supervisor = None
            self._refresh_status()
            return

        def worker() -> None:
            try:
                assert self.supervisor is not None
                self.supervisor.start()
                self.events.put(("started", ""))
            except (OSError, ServiceLaunchError, ValueError) as exc:
                self.events.put(("error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _stop(self) -> None:
        if not self.supervisor:
            return
        self.stop_button.configure(state="disabled")
        self.status_var.set("正在停止…")

        def worker() -> None:
            assert self.supervisor is not None
            self.supervisor.stop()
            self.events.put(("stopped", ""))

        threading.Thread(target=worker, daemon=True).start()

    def _open_dashboard(self) -> None:
        webbrowser.open("http://127.0.0.1:6185")

    def _open_install_root(self) -> None:
        if os.name == "nt":
            os.startfile(self.install_root)  # type: ignore[attr-defined]

    def _edit_brand(self) -> None:
        from tkinter import messagebox

        dialog = self.tk.Toplevel(self.root)
        dialog.title("一键修改机器人名称")
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.resizable(False, False)
        frame = self.ttk.Frame(dialog, padding=20)
        frame.pack(fill="both", expand=True)
        values = {
            "display_name": self.tk.StringVar(value=self.brand.display_name),
            "short_name": self.tk.StringVar(value=self.brand.short_name),
            "persona_name": self.tk.StringVar(value=self.brand.persona_name),
            "welcome_text": self.tk.StringVar(value=self.brand.welcome_text),
        }
        labels = (
            ("display_name", "机器人名称"),
            ("short_name", "机器人简称"),
            ("persona_name", "人格称呼"),
            ("welcome_text", "欢迎语"),
        )
        for row_index, (key, label) in enumerate(labels):
            self.ttk.Label(frame, text=label).grid(row=row_index, column=0, sticky="w", pady=5)
            self.ttk.Entry(frame, textvariable=values[key], width=42).grid(
                row=row_index, column=1, sticky="ew", padx=(12, 0), pady=5
            )

        def save() -> None:
            try:
                profile = BrandProfile(
                    display_name=values["display_name"].get(),
                    short_name=values["short_name"].get(),
                    persona_name=values["persona_name"].get(),
                    avatar_path=self.brand.avatar_path,
                    welcome_text=values["welcome_text"].get(),
                ).normalized()
                self.brand = apply_brand_change(self.install_root, profile)
                self.root.title(f"{self.brand.display_name} · 机器人管理器")
                messagebox.showinfo(
                    "已应用",
                    "名称、欢迎语和 AstrBot 默认人格已同步。正在运行时请重启机器人后生效。",
                    parent=dialog,
                )
                dialog.destroy()
            except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
                messagebox.showerror("保存失败", str(exc), parent=dialog)

        buttons = self.ttk.Frame(frame)
        buttons.grid(row=len(labels), column=0, columnspan=2, sticky="e", pady=(16, 0))
        self.ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left")
        self.ttk.Button(buttons, text="一键应用", command=save).pack(
            side="left", padx=(8, 0)
        )

    def _select_wechat(self) -> None:
        from tkinter import filedialog, messagebox

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="重新选择微信主程序 Weixin.exe",
            filetypes=(("微信主程序", "Weixin.exe"), ("可执行文件", "*.exe")),
        )
        if not selected:
            return
        try:
            apply_wechat_change(self.install_root, selected)
            messagebox.showinfo(
                "已保存",
                "微信路径与版本已更新。正在运行时请重启机器人后生效。",
                parent=self.root,
            )
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            messagebox.showerror("微信路径不可用", str(exc), parent=self.root)

    def _export_diagnostics(self) -> None:
        from tkinter import filedialog, messagebox

        target = filedialog.asksaveasfilename(
            parent=self.root,
            title="保存脱敏诊断",
            defaultextension=".json",
            initialfile="wechatrebot-diagnostics.json",
            filetypes=(("JSON", "*.json"),),
        )
        if not target:
            return
        try:
            report = collect_diagnostics(self.install_root)
            Path(target).write_text(
                json.dumps(report, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            messagebox.showinfo("导出完成", "诊断文件已脱敏保存。", parent=self.root)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def _close(self) -> None:
        from tkinter import messagebox

        if self.supervisor and self.supervisor.running:
            if not messagebox.askyesno(
                "退出管理器",
                "退出会同时停止机器人，是否继续？",
                parent=self.root,
            ):
                return
            self.supervisor.stop()
        self.root.destroy()


def run_control_center(install_root: str | Path) -> int:
    if os.name != "nt":
        return 2
    import tkinter as tk

    root = tk.Tk()
    ControlCenter(root, Path(install_root))
    root.mainloop()
    return 0


__all__ = [
    "ControlCenter",
    "apply_brand_change",
    "apply_wechat_change",
    "load_brand_profile",
    "run_control_center",
]
