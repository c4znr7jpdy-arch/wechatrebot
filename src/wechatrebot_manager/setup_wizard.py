from __future__ import annotations

import os
from pathlib import Path
import queue
import threading

from .brand import BrandProfile
from .install_paths import validate_install_path
from .installer import InstallRequest, perform_install
from .provider import OpenAIProviderProfile
from .runtime_config import normalize_admin_id
from .wechat_path import (
    WechatExecutable,
    discover_wechat_executables,
    validate_wechat_executable,
)


PROVIDER_DEEPSEEK = "DeepSeek V4 Flash（推荐）"
PROVIDER_CUSTOM = "自定义 OpenAI 兼容"
PROVIDER_LATER = "稍后在管理后台配置"


def provider_profile_for_choice(
    choice: str,
    *,
    api_key: str,
    api_base: str,
    model: str,
) -> OpenAIProviderProfile | None:
    if choice == PROVIDER_LATER:
        return None
    return OpenAIProviderProfile(
        api_key=api_key,
        api_base=api_base,
        model=model,
    ).normalized()


class SetupWizard:
    def __init__(
        self,
        root,
        *,
        payload_root: Path | None,
        allow_incomplete_payload: bool,
    ) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.payload_root = payload_root
        self.allow_incomplete_payload = allow_incomplete_payload
        self.page = 0
        self.wechat: WechatExecutable | None = None
        self.installing = False
        self.events: queue.Queue[tuple[object, ...]] = queue.Queue()

        root.title("WeChat ReBot 安装向导")
        root.geometry("820x620")
        root.minsize(760, 560)
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
        ttk.Label(header, text="WeChat ReBot", style="Title.TLabel").pack(anchor="w")
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

        self.install_var = tk.StringVar()
        self.wechat_var = tk.StringVar()
        self.wechat_status_var = tk.StringVar(value="尚未检查")
        self.display_name_var = tk.StringVar(value="智能机器人")
        self.short_name_var = tk.StringVar(value="机器人")
        self.persona_name_var = tk.StringVar(value="智能机器人")
        self.welcome_var = tk.StringVar()
        self.admin_id_var = tk.StringVar()
        self.avatar_var = tk.StringVar()
        self.provider_choice_var = tk.StringVar(value=PROVIDER_DEEPSEEK)
        self.api_base_var = tk.StringVar(value="https://api.deepseek.com")
        self.model_var = tk.StringVar(value="deepseek-v4-flash")
        self.api_key_var = tk.StringVar()
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
            wraplength=720,
            justify="left",
        ).pack(anchor="w", pady=(8, 24))

    def _show_page(self) -> None:
        self._clear_body()
        self.step_label.configure(text=f"步骤 {self.page + 1} / 4")
        self.back_button.configure(state="disabled" if self.page == 0 else "normal")
        self.next_button.configure(text="安装" if self.page == 3 else "下一步")
        if self.page == 0:
            self._page_install_root()
        elif self.page == 1:
            self._page_wechat()
        elif self.page == 2:
            self._page_brand_provider()
        else:
            self._page_summary()

    def _page_install_root(self) -> None:
        self._heading(
            "选择安装位置",
            "必须先选择机器人安装目录。确认安装前不会向该目录写入程序文件，微信客户端不会被复制进项目。",
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
            text="建议选择本机固定磁盘上的独立空目录，至少保留 5 GiB 空间。支持中文和空格路径。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(12, 0))

    def _page_wechat(self) -> None:
        self._heading(
            "选择本机微信",
            "管理器只使用客户已经安装的受支持微信。自动发现失败时，请手动选择 Weixin.exe。",
        )
        row = self.ttk.Frame(self.body)
        row.pack(fill="x")
        self.ttk.Entry(row, textvariable=self.wechat_var).pack(
            side="left", fill="x", expand=True
        )
        self.ttk.Button(row, text="浏览…", command=self._browse_wechat).pack(
            side="left", padx=(10, 0)
        )
        self.ttk.Label(self.body, textvariable=self.wechat_status_var).pack(
            anchor="w", pady=(12, 0)
        )
        if not self.wechat_var.get():
            discovered = tuple(
                item for item in discover_wechat_executables() if item.supported
            )
            if len(discovered) == 1:
                self._set_wechat(discovered[0])
            elif discovered:
                self.wechat_status_var.set("发现多个微信版本，请手动选择要使用的 Weixin.exe。")
            else:
                self.wechat_status_var.set("未自动发现受支持微信，请点击“浏览”。")

    def _entry_row(self, parent, label: str, variable, *, show: str | None = None) -> None:
        row = self.ttk.Frame(parent)
        row.pack(fill="x", pady=5)
        self.ttk.Label(row, text=label, width=14).pack(side="left")
        options: dict[str, object] = {"textvariable": variable}
        if show:
            options["show"] = show
        self.ttk.Entry(row, **options).pack(side="left", fill="x", expand=True)

    def _page_brand_provider(self) -> None:
        self._heading(
            "机器人名称与模型",
            "这里修改一次，管理器标题、欢迎语、帮助标题和 AstrBot 默认人格会同步生成。API Key 只写入客户安装目录。",
        )
        columns = self.ttk.Frame(self.body)
        columns.pack(fill="both", expand=True)
        brand = self.ttk.LabelFrame(columns, text="品牌", padding=12)
        brand.pack(side="left", fill="both", expand=True, padx=(0, 8))
        model = self.ttk.LabelFrame(columns, text="模型", padding=12)
        model.pack(side="left", fill="both", expand=True, padx=(8, 0))

        self._entry_row(brand, "机器人名称", self.display_name_var)
        self._entry_row(brand, "机器人简称", self.short_name_var)
        self._entry_row(brand, "人格称呼", self.persona_name_var)
        self._entry_row(brand, "欢迎语（可选）", self.welcome_var)
        self._entry_row(brand, "管理员 wxid（可选）", self.admin_id_var)
        avatar_row = self.ttk.Frame(brand)
        avatar_row.pack(fill="x", pady=5)
        self.ttk.Label(avatar_row, text="头像（可选）", width=14).pack(side="left")
        self.ttk.Entry(avatar_row, textvariable=self.avatar_var).pack(
            side="left", fill="x", expand=True
        )
        self.ttk.Button(avatar_row, text="选择", command=self._browse_avatar).pack(
            side="left", padx=(6, 0)
        )

        combo = self.ttk.Combobox(
            model,
            textvariable=self.provider_choice_var,
            values=(PROVIDER_DEEPSEEK, PROVIDER_CUSTOM, PROVIDER_LATER),
            state="readonly",
        )
        combo.pack(fill="x", pady=(5, 8))
        combo.bind("<<ComboboxSelected>>", self._provider_changed)
        self._entry_row(model, "API 地址", self.api_base_var)
        self._entry_row(model, "模型名称", self.model_var)
        self._entry_row(model, "API Key", self.api_key_var, show="●")
        self.ttk.Label(
            model,
            text="选择“稍后配置”时可以先完成安装，再从 AstrBot 后台设置模型。",
            style="Hint.TLabel",
            wraplength=320,
        ).pack(anchor="w", pady=(10, 0))

    def _page_summary(self) -> None:
        self._heading(
            "确认并安装",
            "确认后才会创建或更新正式安装目录。安装过程会验证完整载荷并保留客户数据。",
        )
        provider = self.provider_choice_var.get()
        values = (
            ("安装目录", self.install_var.get()),
            ("微信路径", self.wechat_var.get()),
            ("微信版本", self.wechat.version if self.wechat else "未知"),
            ("机器人名称", self.display_name_var.get().strip()),
            ("管理员", self.admin_id_var.get().strip() or "稍后配置"),
            ("模型", provider),
            ("载荷", str(self.payload_root) if self.payload_root else "仅保存配置"),
        )
        box = self.ttk.LabelFrame(self.body, text="安装摘要", padding=16)
        box.pack(fill="x")
        for label, value in values:
            row = self.ttk.Frame(box)
            row.pack(fill="x", pady=4)
            self.ttk.Label(row, text=label, width=14).pack(side="left", anchor="n")
            self.ttk.Label(row, text=value, wraplength=570, justify="left").pack(
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

    def _browse_wechat(self) -> None:
        from tkinter import filedialog, messagebox

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择微信主程序 Weixin.exe",
            filetypes=(("微信主程序", "Weixin.exe"), ("可执行文件", "*.exe")),
        )
        if not selected:
            return
        item = validate_wechat_executable(selected)
        if not item.supported:
            messagebox.showerror("微信路径不可用", "\n".join(item.errors), parent=self.root)
            return
        self._set_wechat(item)

    def _set_wechat(self, item: WechatExecutable) -> None:
        self.wechat = item
        self.wechat_var.set(str(item.path))
        self.wechat_status_var.set(f"已通过版本检查：{item.version}")

    def _browse_avatar(self) -> None:
        from tkinter import filedialog

        selected = filedialog.askopenfilename(
            parent=self.root,
            title="选择机器人头像",
            filetypes=(("图片", "*.png *.jpg *.jpeg *.webp"),),
        )
        if selected:
            self.avatar_var.set(selected)

    def _provider_changed(self, _event=None) -> None:
        choice = self.provider_choice_var.get()
        if choice == PROVIDER_DEEPSEEK:
            self.api_base_var.set("https://api.deepseek.com")
            self.model_var.set("deepseek-v4-flash")
        elif choice == PROVIDER_CUSTOM:
            self.api_base_var.set("")
            self.model_var.set("")
        else:
            self.api_base_var.set("")
            self.model_var.set("")
            self.api_key_var.set("")

    def _brand(self) -> BrandProfile:
        return BrandProfile(
            display_name=self.display_name_var.get(),
            short_name=self.short_name_var.get(),
            persona_name=self.persona_name_var.get(),
            avatar_path=self.avatar_var.get(),
            welcome_text=self.welcome_var.get(),
        ).normalized()

    def _provider(self) -> OpenAIProviderProfile | None:
        return provider_profile_for_choice(
            self.provider_choice_var.get(),
            api_key=self.api_key_var.get(),
            api_base=self.api_base_var.get(),
            model=self.model_var.get(),
        )

    def _validate_page(self) -> bool:
        from tkinter import messagebox

        errors: tuple[str, ...] = ()
        if self.page == 0:
            result = validate_install_path(self.install_var.get())
            errors = result.errors
            if result.ok:
                self.install_var.set(str(result.path))
        elif self.page == 1:
            item = validate_wechat_executable(self.wechat_var.get())
            errors = item.errors
            if item.supported:
                self._set_wechat(item)
        elif self.page == 2:
            errors = self._brand().validate()
            try:
                normalize_admin_id(self.admin_id_var.get())
            except ValueError as exc:
                errors = (*errors, str(exc))
            provider = self._provider()
            if provider is not None:
                errors = (*errors, *provider.validate())
        if errors:
            messagebox.showerror("无法继续", "\n".join(errors), parent=self.root)
            return False
        return True

    def _next(self) -> None:
        if self.installing or not self._validate_page():
            return
        if self.page < 3:
            self.page += 1
            self._show_page()
            return
        self._start_install()

    def _back(self) -> None:
        if not self.installing and self.page > 0:
            self.page -= 1
            self._show_page()

    def _request(self) -> InstallRequest:
        assert self.wechat is not None
        return InstallRequest(
            install_root=Path(self.install_var.get()),
            payload_root=self.payload_root,
            wechat=self.wechat,
            brand=self._brand(),
            provider=self._provider(),
            admin_id=self.admin_id_var.get(),
            allow_incomplete_payload=self.allow_incomplete_payload,
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
        self._heading("正在安装", "请保持窗口开启。机器人、微信和现有服务不会在安装阶段自动启动。")
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
                    self.page = 3
                    self._show_page()
                    self.cancel_button.configure(state="normal")
                    return
        except queue.Empty:
            pass
        if self.installing:
            self.root.after(100, self._poll_events)

    def _show_progress(self, phase: str, current: int, total: int) -> None:
        labels = {
            "verify": "正在验证载荷",
            "stage": "正在准备安装文件",
            "install": "正在写入程序文件",
            "configure": "正在生成客户配置",
        }
        ranges = {
            "verify": (0.0, 25.0),
            "stage": (25.0, 50.0),
            "install": (50.0, 95.0),
            "configure": (95.0, 100.0),
        }
        start, end = ranges.get(phase, (0.0, 100.0))
        ratio = current / max(total, 1)
        self.progress_var.set(start + (end - start) * ratio)
        self.progress_text_var.set(f"{labels.get(phase, phase)}：{current} / {total}")

    def _show_complete(self, install_root: Path) -> None:
        self.installing = False
        self._clear_body()
        self.step_label.configure(text="安装完成")
        self._heading(
            "安装已完成",
            "程序、运行时、微信路径、机器人品牌和模型设置已保存。首次启动请从安装目录打开机器人管理器。",
        )
        self.ttk.Label(
            self.body,
            text=str(install_root),
            font=("Consolas", 11),
            wraplength=720,
        ).pack(anchor="w", pady=(8, 0))
        self.progress_var.set(100)
        self.back_button.configure(state="disabled")
        self.next_button.configure(text="完成", state="normal", command=self.root.destroy)
        self.cancel_button.configure(state="disabled")

    def _close(self) -> None:
        if self.installing:
            return
        self.root.destroy()


def run_setup_wizard(
    payload_root: Path | None,
    *,
    allow_incomplete_payload: bool = False,
) -> int:
    if os.name != "nt":
        return 2
    import tkinter as tk

    root = tk.Tk()
    SetupWizard(
        root,
        payload_root=payload_root,
        allow_incomplete_payload=allow_incomplete_payload,
    )
    root.mainloop()
    return 0


__all__ = [
    "PROVIDER_CUSTOM",
    "PROVIDER_DEEPSEEK",
    "PROVIDER_LATER",
    "SetupWizard",
    "provider_profile_for_choice",
    "run_setup_wizard",
]
