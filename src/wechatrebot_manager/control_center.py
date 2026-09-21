from __future__ import annotations

import json
import math
import os
from pathlib import Path
import queue
import sys
import threading
import zipfile

from . import __version__
from .backup import BackupError, create_customer_backup, restore_customer_backup
from .brand import BrandProfile
from .customer_config import (
    RobotSettings,
    load_plugins,
    load_provider_profile,
    load_robot_settings,
    save_provider_profile,
    save_robot_settings,
)
from .customer_logs import log_sources, sanitize_log_text, tail_log
from .diagnostics import collect_diagnostics, diagnostic_error_messages
from .launcher import ProcessSupervisor, ServiceLaunchError, ServiceSpec, build_service_specs
from .provider import (
    MODEL_PROVIDER_PRESETS,
    OpenAIProviderProfile,
    provider_source_id,
)
from .plugin_center import launch_plugin_center
from .runtime_status import RuntimeSnapshot, runtime_snapshot
from .ui_theme import COLORS, FONT_MONO, FONT_SMALL, GEOMETRY, apply_product_theme
from .update_ui import UpdateDialog
from .updates import (
    PreparedUpdate,
    UpdateChannel,
    UpdateError,
    UpdateRelease,
    check_for_update,
    cleanup_stale_update_workdirs,
    consume_update_result,
    create_update_plan,
    launch_update_helper,
    load_update_channel,
    prepare_update,
)
from .wechat_path import validate_wechat_executable


def load_brand_profile(install_root: str | Path) -> BrandProfile:
    return load_robot_settings(install_root).profile


def apply_brand_change(install_root: str | Path, profile: BrandProfile) -> BrandProfile:
    current = load_robot_settings(install_root)
    return save_robot_settings(
        install_root,
        RobotSettings(profile=profile, admin_id=current.admin_id),
    ).profile


def apply_wechat_change(install_root: str | Path, wechat_path: str | Path) -> None:
    from .runtime_config import write_runtime_configs

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
    NAV_ITEMS = (
        ("home", "运行中心"),
        ("robot", "机器人设置"),
        ("model", "回复模型"),
        ("plugins", "功能扩展"),
        ("logs", "运行日志"),
    )

    def __init__(self, root, install_root: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.root = root
        self.install_root = install_root.expanduser().resolve()
        self.robot_settings = load_robot_settings(self.install_root)
        self.brand = self.robot_settings.profile
        self.supervisor: ProcessSupervisor | None = None
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.current_page = "home"
        self.start_phase = "idle"
        self.last_snapshot = RuntimeSnapshot(False, False, "")
        self.pages: dict[str, object] = {}
        self.nav_buttons: dict[str, tuple[object, object, object, object]] = {}
        self.rail_nodes: list[tuple[int, int, int]] = []
        self._drag_origin: tuple[int, int] | None = None
        self._windowed_geometry = f"{GEOMETRY.window_width}x{GEOMETRY.window_height}"
        self._maximized = False
        self.update_channel: UpdateChannel | None = None
        self.update_channel_error = ""
        self.update_checking = False
        self.available_update: UpdateRelease | None = None
        self.update_dialog: UpdateDialog | None = None
        self.notified_update_version = ""
        self._update_after_id: str | None = None
        try:
            self.update_channel = load_update_channel(self.install_root)
        except UpdateError as exc:
            self.update_channel_error = str(exc)

        root.title(f"{self.brand.display_name} · 运行中心")
        root.geometry(self._windowed_geometry)
        root.minsize(GEOMETRY.minimum_width, GEOMETRY.minimum_height)
        root.overrideredirect(True)
        root.protocol("WM_DELETE_WINDOW", self._close)
        root.bind("<Map>", lambda _event: root.after(20, self._restore_borderless), add="+")
        apply_product_theme(root, ttk)

        self._build_shell()
        self._build_home_page()
        self._build_robot_page()
        self._build_model_page()
        self._build_plugins_page()
        self._build_logs_page()
        self._show_page("home")
        self._refresh_plugins()
        self._refresh_logs()
        self._refresh_status()
        root.after(80, self._apply_windows_chrome)
        root.after(250, self._show_update_result)
        root.after(
            3000,
            lambda: threading.Thread(
                target=cleanup_stale_update_workdirs,
                args=(self.install_root,),
                daemon=True,
            ).start(),
        )
        if self.update_channel is not None and self.update_channel.enabled:
            root.after(1800, lambda: self._check_updates(manual=False))
        root.after(700, self._timer)

    def _build_shell(self) -> None:
        shell = self.tk.Frame(self.root, background="#5E6875")
        shell.pack(fill="both", expand=True)
        topbar = self.tk.Frame(
            shell,
            background=COLORS.surface,
            height=GEOMETRY.titlebar_height,
            highlightbackground=COLORS.border,
            highlightthickness=1,
        )
        topbar.pack(side="top", fill="x", padx=1, pady=(1, 0))
        topbar.pack_propagate(False)
        self.topbar = topbar
        logo = self.tk.Canvas(
            topbar,
            width=34,
            height=34,
            background=COLORS.surface,
            highlightthickness=0,
        )
        logo.pack(side="left", padx=(26, 9))
        self._draw_brand_logo(logo)
        self.brand_title = self.tk.Label(
            topbar,
            text=self.brand.display_name,
            background=COLORS.surface,
            foreground=COLORS.ink,
            font=("Microsoft YaHei UI", 14, "bold"),
        )
        self.brand_title.pack(side="left")
        badge = self.tk.Label(
            topbar,
            text="商业版",
            background="#EAF2FF",
            foreground=COLORS.primary,
            font=("Microsoft YaHei UI", 9),
            padx=8,
            pady=2,
        )
        badge.pack(side="left", padx=(10, 0))

        close_button = self._window_button(topbar, "×", self._close, close=True)
        close_button.pack(side="right", fill="y")
        maximize_button = self._window_button(topbar, "□", self._toggle_maximize)
        maximize_button.pack(side="right", fill="y")
        minimize_button = self._window_button(topbar, "—", self._minimize_window)
        minimize_button.pack(side="right", fill="y")
        divider = self.tk.Frame(topbar, width=1, height=30, background=COLORS.border)
        divider.pack(side="right", padx=(10, 12))
        avatar = self.tk.Canvas(topbar, width=38, height=38, background=COLORS.surface, highlightthickness=0)
        avatar.pack(side="right")
        self._draw_avatar(avatar)
        settings_button = self.tk.Button(
            topbar,
            text="设置",
            command=lambda: self._show_page("robot"),
            background=COLORS.surface,
            foreground=COLORS.ink,
            activebackground=COLORS.surface_muted,
            activeforeground=COLORS.ink,
            font=("Microsoft YaHei UI", 10),
            relief="flat",
            borderwidth=0,
            padx=14,
            cursor="hand2",
        )
        settings_button.pack(side="right", padx=(0, 14), fill="y")
        self.update_button = self.tk.Button(
            topbar,
            text="检查更新",
            command=lambda: self._check_updates(manual=True),
            background=COLORS.surface,
            foreground=COLORS.primary,
            activebackground=COLORS.surface_muted,
            activeforeground=COLORS.primary_hover,
            font=("Microsoft YaHei UI", 10),
            relief="flat",
            borderwidth=0,
            padx=14,
            cursor="hand2",
        )
        self.update_button.pack(side="right", fill="y")

        for widget in (topbar, logo, self.brand_title, badge):
            self._bind_title_drag(widget)
        topbar.bind("<Double-Button-1>", lambda _event: self._toggle_maximize(), add="+")

        body = self.ttk.Frame(shell, style="App.TFrame")
        body.pack(side="top", fill="both", expand=True, padx=1, pady=(0, 1))
        sidebar = self.tk.Frame(body, background=COLORS.ink, width=GEOMETRY.sidebar_width)
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)

        nav = self.tk.Frame(sidebar, background=COLORS.ink)
        nav.pack(fill="x", pady=(18, 0))
        for key, label in self.NAV_ITEMS:
            self._create_nav_item(nav, key, label)

        footer = self.tk.Frame(sidebar, background=COLORS.ink, height=170)
        footer.pack(side="bottom", fill="x")
        footer.pack_propagate(False)
        self.sidebar_state_var = self.tk.StringVar(value="当前未运行")
        self.tk.Frame(footer, background="#29415F", height=1).pack(fill="x")
        help_row = self.tk.Frame(footer, background=COLORS.ink, height=58, cursor="hand2")
        help_row.pack(fill="x", pady=(12, 0))
        help_row.pack_propagate(False)
        help_icon = self.tk.Canvas(help_row, width=32, height=32, background=COLORS.ink, highlightthickness=0)
        help_icon.pack(side="left", padx=(26, 7))
        self._draw_icon(help_icon, "help", COLORS.sidebar_text)
        help_label = self.tk.Label(help_row, text="帮助与支持", background=COLORS.ink, foreground=COLORS.sidebar_text, font=("Microsoft YaHei UI", 10))
        help_label.pack(side="left")
        for widget in (help_row, help_icon, help_label):
            widget.bind("<Button-1>", lambda _event: self._show_page("logs"))
        self.tk.Frame(footer, background="#29415F", height=1).pack(fill="x", pady=(6, 0))
        self.tk.Label(
            footer,
            text=f"v{__version__}",
            background=COLORS.ink,
            foreground="#91A9C6",
            font=FONT_SMALL,
        ).pack(pady=(16, 0))

        self.content = self.ttk.Frame(
            body,
            style="App.TFrame",
            padding=(GEOMETRY.content_padding_x, GEOMETRY.content_padding_y),
        )
        self.content.pack(side="left", fill="both", expand=True)

    def _window_button(self, parent, text: str, command, *, close: bool = False):
        normal = COLORS.surface
        hover = "#E81123" if close else COLORS.surface_muted
        foreground = COLORS.ink
        button = self.tk.Button(
            parent,
            text=text,
            command=command,
            width=4,
            background=normal,
            foreground=foreground,
            activebackground=hover,
            activeforeground="#FFFFFF" if close else foreground,
            font=("Segoe UI", 13),
            relief="flat",
            borderwidth=0,
            cursor="hand2",
        )
        button.bind("<Enter>", lambda _event: button.configure(background=hover, foreground="#FFFFFF" if close else foreground))
        button.bind("<Leave>", lambda _event: button.configure(background=normal, foreground=foreground))
        return button

    def _bind_title_drag(self, widget) -> None:
        widget.bind("<ButtonPress-1>", self._begin_window_drag, add="+")
        widget.bind("<B1-Motion>", self._drag_window, add="+")
        widget.bind("<ButtonRelease-1>", self._end_window_drag, add="+")

    def _begin_window_drag(self, event) -> None:
        if self._maximized:
            return
        self._drag_origin = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def _drag_window(self, event) -> None:
        if self._drag_origin is None or self._maximized:
            return
        offset_x, offset_y = self._drag_origin
        self.root.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _end_window_drag(self, _event) -> None:
        self._drag_origin = None

    def _minimize_window(self) -> None:
        self.root.overrideredirect(False)
        self.root.iconify()
        self.root.after(160, self._restore_borderless)

    def _restore_borderless(self) -> None:
        if self.root.state() == "normal":
            self.root.overrideredirect(True)
            self._apply_windows_chrome()

    def _toggle_maximize(self) -> None:
        if self._maximized:
            self.root.state("normal")
            self.root.geometry(self._windowed_geometry)
            self._maximized = False
        else:
            self._windowed_geometry = self.root.geometry()
            try:
                self.root.state("zoomed")
            except self.tk.TclError:
                width = self.root.winfo_screenwidth()
                height = self.root.winfo_screenheight() - 40
                self.root.geometry(f"{width}x{height}+0+0")
            self._maximized = True
        self.root.overrideredirect(True)
        self._apply_windows_chrome()

    def _apply_windows_chrome(self) -> None:
        if os.name != "nt" or not self.root.winfo_exists():
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.root.winfo_id()) or self.root.winfo_id()
            exstyle = user32.GetWindowLongW(hwnd, -20)
            user32.SetWindowLongW(hwnd, -20, (exstyle | 0x00040000) & ~0x00000080)
            preference = ctypes.c_int(2)
            ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, 33, ctypes.byref(preference), ctypes.sizeof(preference))
        except (AttributeError, OSError, self.tk.TclError):
            return

    def _draw_brand_logo(self, canvas) -> None:
        center_x = center_y = 17
        for index in range(6):
            angle = math.radians(index * 60 - 90)
            x = center_x + math.cos(angle) * 11
            y = center_y + math.sin(angle) * 11
            canvas.create_oval(x - 4.5, y - 4.5, x + 4.5, y + 4.5, fill=COLORS.primary, outline="")
        points: list[float] = []
        for index in range(6):
            angle = math.radians(index * 60 - 90)
            points.extend((center_x + math.cos(angle) * 7, center_y + math.sin(angle) * 7))
        canvas.create_polygon(points, fill=COLORS.primary, outline="")
        canvas.create_oval(14, 14, 20, 20, fill="#FFFFFF", outline="")

    def _draw_avatar(self, canvas) -> None:
        canvas.create_oval(2, 2, 36, 36, fill="#F1F3F6", outline="#D8DDE5", width=1)
        canvas.create_oval(13, 8, 25, 20, fill="#B7BEC8", outline="")
        canvas.create_arc(8, 18, 30, 35, start=0, extent=180, fill="#B7BEC8", outline="#B7BEC8")

    def _create_nav_item(self, parent, key: str, label: str) -> None:
        row = self.tk.Frame(parent, background=COLORS.ink, height=56, cursor="hand2")
        row.pack(fill="x")
        row.pack_propagate(False)
        accent = self.tk.Frame(row, width=4, background=COLORS.ink)
        accent.pack(side="left", fill="y")
        icon = self.tk.Canvas(row, width=34, height=34, background=COLORS.ink, highlightthickness=0, cursor="hand2")
        icon.pack(side="left", padx=(22, 7))
        text = self.tk.Label(row, text=label, background=COLORS.ink, foreground=COLORS.sidebar_text, font=("Microsoft YaHei UI", 11), cursor="hand2")
        text.pack(side="left")
        for widget in (row, accent, icon, text):
            widget.bind("<Button-1>", lambda _event, page=key: self._show_page(page))
            widget.bind("<Enter>", lambda _event, page=key: self._set_nav_hover(page, True))
            widget.bind("<Leave>", lambda _event, page=key: self._set_nav_hover(page, False))
        self.nav_buttons[key] = (row, accent, icon, text)
        self._draw_icon(icon, key, COLORS.sidebar_text)

    def _set_nav_hover(self, key: str, active: bool) -> None:
        if key == self.current_page or key not in self.nav_buttons:
            return
        row, accent, icon, label = self.nav_buttons[key]
        background = "#1B3656" if active else COLORS.ink
        for widget in (row, icon, label):
            widget.configure(background=background)
        accent.configure(background=background)

    def _paint_navigation(self) -> None:
        for key, (row, accent, icon, label) in self.nav_buttons.items():
            selected = key == self.current_page
            background = COLORS.primary if selected else COLORS.ink
            foreground = "#FFFFFF" if selected else COLORS.sidebar_text
            row.configure(background=background)
            accent.configure(background="#6FA8FF" if selected else COLORS.ink)
            icon.configure(background=background)
            label.configure(background=background, foreground=foreground)
            icon.delete("all")
            self._draw_icon(icon, key, foreground)

    def _draw_icon(self, canvas, kind: str, color: str) -> None:
        line = dict(fill=color, width=2, capstyle="round", joinstyle="round")
        if kind == "home":
            canvas.create_line(7, 17, 17, 8, 27, 17, **line)
            canvas.create_line(10, 16, 10, 27, 24, 27, 24, 16, **line)
            canvas.create_line(15, 27, 15, 21, 20, 21, 20, 27, **line)
        elif kind == "robot":
            canvas.create_line(17, 6, 17, 9, **line)
            canvas.create_oval(15, 3, 19, 7, outline=color, width=2)
            canvas.create_rectangle(7, 10, 27, 27, outline=color, width=2)
            canvas.create_line(7, 16, 4, 16, 4, 22, 7, 22, **line)
            canvas.create_line(27, 16, 30, 16, 30, 22, 27, 22, **line)
            canvas.create_oval(11, 15, 14, 18, fill=color, outline="")
            canvas.create_oval(20, 15, 23, 18, fill=color, outline="")
            canvas.create_line(12, 22, 22, 22, **line)
        elif kind in {"model", "wechat"}:
            canvas.create_oval(5, 7, 27, 25, outline=color, width=2)
            canvas.create_line(9, 24, 7, 29, 14, 25, **line)
            for x in (11, 17, 23):
                canvas.create_oval(x - 1, 15, x + 1, 17, fill=color, outline="")
        elif kind == "plugins":
            canvas.create_line(8, 11, 13, 11, 13, 8, **line)
            canvas.create_arc(13, 4, 21, 12, start=180, extent=-180, outline=color, width=2)
            canvas.create_line(21, 8, 21, 13, 26, 13, **line)
            canvas.create_arc(22, 13, 30, 21, start=90, extent=-180, outline=color, width=2)
            canvas.create_line(26, 21, 21, 21, 21, 26, 13, 26, 13, 21, 8, 21, 8, 11, **line)
        elif kind == "logs":
            canvas.create_rectangle(8, 5, 25, 29, outline=color, width=2)
            canvas.create_line(12, 11, 21, 11, **line)
            canvas.create_line(12, 16, 21, 16, **line)
            canvas.create_line(12, 21, 19, 21, **line)
        elif kind == "help":
            canvas.create_oval(5, 5, 29, 29, outline=color, width=2)
            canvas.create_text(17, 16, text="?", fill=color, font=("Segoe UI", 13, "bold"))
            canvas.create_oval(16, 24, 18, 26, fill=color, outline="")
        elif kind == "message":
            canvas.create_rectangle(7, 7, 27, 24, outline=color, width=2)
            canvas.create_line(11, 24, 9, 29, 16, 24, **line)
            canvas.create_line(12, 13, 22, 13, **line)
            canvas.create_line(12, 18, 20, 18, **line)

    def _new_page(self, key: str, title: str, hint: str):
        frame = self.ttk.Frame(self.content, style="App.TFrame")
        header = self.ttk.Frame(frame, style="App.TFrame", padding=(12, 0, 0, 0))
        header.pack(fill="x", pady=(0, 24))
        self.ttk.Label(header, text=title, style="PageTitle.TLabel").pack(anchor="w")
        self.ttk.Label(
            header,
            text=hint,
            style="PageHint.TLabel",
            wraplength=820,
            justify="left",
        ).pack(anchor="w", pady=(10, 0))
        self.pages[key] = frame
        return frame

    def _surface(self, parent, *, padding: tuple[int, int] | int = 20):
        return self.ttk.Frame(parent, style="Surface.TFrame", padding=padding)

    def _build_home_page(self) -> None:
        page = self._new_page(
            "home",
            "运行中心",
            "启动后登录微信，即可开始自动回复",
        )
        workspace = self.ttk.Frame(page, style="App.TFrame")
        workspace.pack(fill="x")
        workspace.columnconfigure(0, weight=8)
        workspace.columnconfigure(1, weight=5)
        workspace.rowconfigure(0, weight=1)
        launch = self.tk.Frame(workspace, background=COLORS.canvas, height=GEOMETRY.home_upper_height)
        launch.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        launch.grid_propagate(False)
        self.launch_canvas = self.tk.Canvas(
            launch,
            width=585,
            height=GEOMETRY.home_upper_height,
            background=COLORS.canvas,
            highlightthickness=0,
        )
        self.launch_canvas.pack(fill="both", expand=True)
        center_x = 298
        self.launch_canvas.create_oval(132, 4, 132 + GEOMETRY.launcher_diameter, 4 + GEOMETRY.launcher_diameter, outline="#D9DEE7", width=4, dash=(5, 6))
        self.launch_canvas.create_oval(151, 23, 445, 317, outline="#C9D1DC", width=2)
        self.power_arc = self.launch_canvas.create_arc(center_x - 38, 70, center_x + 38, 146, start=135, extent=270, style="arc", outline=COLORS.success, width=7)
        self.power_line = self.launch_canvas.create_line(center_x, 61, center_x, 103, fill=COLORS.success, width=7, capstyle="round")
        self.launch_status_item = self.launch_canvas.create_text(center_x, 170, text="等待启动", font=("Microsoft YaHei UI", 18, "bold"), fill=COLORS.ink)
        self.launch_detail_item = self.launch_canvas.create_text(center_x, 205, text="所有服务已准备就绪", font=("Microsoft YaHei UI", 10), fill=COLORS.text_muted)
        self.home_status_var = self.tk.StringVar(value="等待启动")
        self.home_detail_var = self.tk.StringVar(value="所有服务已准备就绪")
        self.start_button = self.tk.Button(
            self.launch_canvas,
            text="一键启动",
            command=self._primary_action,
            background=COLORS.primary,
            foreground="#FFFFFF",
            activebackground=COLORS.primary_hover,
            activeforeground="#FFFFFF",
            disabledforeground="#F4F7FB",
            font=("Microsoft YaHei UI", 14, "bold"),
            relief="flat",
            borderwidth=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.launch_canvas.create_window(
            center_x,
            255,
            window=self.start_button,
            width=GEOMETRY.start_button_width,
            height=GEOMETRY.start_button_height,
        )

        labels = ("启动服务", "打开微信", "登录完成")
        self.rail_nodes.clear()
        rail_y = 350
        rail_positions = (120, 298, 476)
        for left, right in zip(rail_positions, rail_positions[1:]):
            self.launch_canvas.create_line(left + 17, rail_y, right - 17, rail_y, fill="#BFC8D5", width=1)
        for index, (position, title) in enumerate(zip(rail_positions, labels)):
            node = self.launch_canvas.create_oval(position - 17, rail_y - 17, position + 17, rail_y + 17, fill=COLORS.canvas, outline=COLORS.primary, width=1)
            number = self.launch_canvas.create_text(position, rail_y, text=str(index + 1), font=("Bahnschrift", 11, "bold"), fill=COLORS.primary)
            title_item = self.launch_canvas.create_text(position, 386, text=title, font=("Microsoft YaHei UI", 10, "bold"), fill=COLORS.text)
            self.rail_nodes.append((node, number, title_item))

        status_border = self.tk.Frame(workspace, background=COLORS.border, padx=1, pady=1)
        status_border.grid(row=0, column=1, sticky="nsew")
        status = self.tk.Frame(status_border, background=COLORS.surface, padx=22, pady=18)
        status.pack(fill="both", expand=True)
        self.tk.Label(status, text="运行状态", background=COLORS.surface, foreground=COLORS.ink, font=("Microsoft YaHei UI", 14, "bold"), anchor="w").pack(fill="x")
        self.wechat_state_var = self.tk.StringVar(value="未连接")
        self.robot_state_var = self.tk.StringVar(value="已就绪")
        self.message_state_var = self.tk.StringVar(value="等待启动")
        self.wechat_state_label = self._status_row(status, "wechat", "微信状态", self.wechat_state_var)
        self.robot_state_label = self._status_row(status, "robot", "机器人状态", self.robot_state_var)
        self.message_state_label = self._status_row(status, "message", "消息服务", self.message_state_var)
        info = self.tk.Frame(status, background="#EEF6FF", padx=13, pady=11)
        info.pack(fill="x", pady=(12, 0))
        self.tk.Label(info, text="i", width=2, background=COLORS.primary, foreground="#FFFFFF", font=("Segoe UI", 9, "bold")).pack(side="left", anchor="n")
        self.tk.Label(
            info,
            text="启动后将自动打开微信，请在微信窗口\n完成登录",
            background="#EEF6FF",
            foreground="#31577E",
            font=("Microsoft YaHei UI", 9),
            justify="left",
            padx=10,
        ).pack(side="left", anchor="w")
        self.account_var = self.tk.StringVar(value="尚未登录")

        recent_border = self.tk.Frame(page, background=COLORS.border, padx=1, pady=1)
        recent_border.pack(fill="both", expand=True, pady=(14, 0))
        recent = self.tk.Frame(recent_border, background=COLORS.surface, padx=18, pady=14)
        recent.pack(fill="both", expand=True)
        self.tk.Label(recent, text="最近运行", background=COLORS.surface, foreground=COLORS.ink, font=("Microsoft YaHei UI", 11, "bold"), anchor="w").pack(fill="x")
        self.recent_title_var = self.tk.StringVar(value="还没有运行记录")
        self.recent_detail_var = self.tk.StringVar(value="启动机器人后，运行情况会显示在这里")
        recent_icon = self.tk.Canvas(recent, width=42, height=46, background=COLORS.surface, highlightthickness=0)
        recent_icon.pack(pady=(7, 1))
        recent_icon.create_rectangle(10, 4, 31, 38, outline="#B8C1CD", width=2)
        recent_icon.create_line(24, 4, 31, 11, fill="#B8C1CD", width=2)
        recent_icon.create_line(24, 4, 24, 12, 31, 12, fill="#B8C1CD", width=2)
        recent_icon.create_line(15, 20, 27, 20, fill="#B8C1CD", width=2)
        recent_icon.create_line(15, 26, 27, 26, fill="#B8C1CD", width=2)
        self.tk.Label(recent, textvariable=self.recent_title_var, background=COLORS.surface, foreground=COLORS.ink, font=("Microsoft YaHei UI", 12, "bold")).pack(pady=(0, 3))
        self.tk.Label(recent, textvariable=self.recent_detail_var, background=COLORS.surface, foreground=COLORS.text_muted, font=FONT_SMALL).pack()
        self.tk.Button(recent, text="查看运行日志  ›", command=lambda: self._show_page("logs"), background=COLORS.surface, foreground=COLORS.primary, activebackground=COLORS.surface, activeforeground=COLORS.primary_hover, relief="flat", cursor="hand2", font=("Microsoft YaHei UI", 10)).pack(pady=(8, 0))

    def _status_row(self, parent, icon_kind: str, title: str, variable):
        row = self.tk.Frame(parent, background=COLORS.surface, height=67)
        row.pack(fill="x")
        row.pack_propagate(False)
        icon_box = self.tk.Frame(row, background=COLORS.surface_muted, width=48, height=48, highlightbackground=COLORS.border, highlightthickness=1)
        icon_box.pack(side="left", pady=9)
        icon_box.pack_propagate(False)
        icon = self.tk.Canvas(icon_box, width=34, height=34, background=COLORS.surface_muted, highlightthickness=0)
        icon.pack(expand=True)
        self._draw_icon(icon, icon_kind, COLORS.ink)
        self.tk.Label(row, text=title, background=COLORS.surface, foreground=COLORS.ink, font=("Microsoft YaHei UI", 10), padx=14).pack(side="left")
        value = self.tk.Label(row, textvariable=variable, background=COLORS.surface, foreground=COLORS.primary, font=("Microsoft YaHei UI", 10, "bold"))
        value.pack(side="right")
        self.tk.Frame(parent, background=COLORS.border, height=1).pack(fill="x")
        return value

    def _form_row(self, parent, label: str, variable, *, secret: bool = False):
        row = self.ttk.Frame(parent, style="Surface.TFrame")
        row.pack(fill="x", pady=7)
        self.ttk.Label(row, text=label, style="Surface.TLabel", width=15).pack(side="left")
        entry = self.ttk.Entry(row, textvariable=variable, show="●" if secret else "")
        entry.pack(side="left", fill="x", expand=True)
        return entry, row

    def _build_robot_page(self) -> None:
        page = self._new_page(
            "robot",
            "机器人设置",
            "修改名称、对外称呼、欢迎语和管理员身份。保存后会同步到回复规则，下次启动生效。",
        )
        surface = self._surface(page, padding=24)
        surface.pack(fill="x")
        self.robot_notice_var = self.tk.StringVar()
        self.ttk.Label(surface, textvariable=self.robot_notice_var, style="SurfaceMuted.TLabel", wraplength=760).pack(anchor="w", pady=(0, 12))
        profile = self.robot_settings.profile
        self.robot_vars = {
            "display_name": self.tk.StringVar(value=profile.display_name),
            "short_name": self.tk.StringVar(value=profile.short_name),
            "persona_name": self.tk.StringVar(value=profile.persona_name),
            "welcome_text": self.tk.StringVar(value=profile.welcome_text),
            "admin_id": self.tk.StringVar(value=self.robot_settings.admin_id),
        }
        for key, label in (
            ("display_name", "机器人名称"),
            ("short_name", "机器人简称"),
            ("persona_name", "对外称呼"),
            ("welcome_text", "欢迎语（可选）"),
            ("admin_id", "管理员 wxid"),
        ):
            self._form_row(surface, label, self.robot_vars[key])
        buttons = self.ttk.Frame(surface, style="Surface.TFrame")
        buttons.pack(fill="x", pady=(16, 0))
        self.robot_save_button = self.ttk.Button(buttons, text="保存机器人设置", style="Primary.TButton", command=self._save_robot)
        self.robot_save_button.pack(side="right")

        maintenance = self._surface(page, padding=20)
        maintenance.pack(fill="x", pady=(20, 0))
        self.ttk.Label(maintenance, text="数据维护", style="Section.TLabel").pack(anchor="w")
        self.ttk.Label(
            maintenance,
            text="微信路径、备份和诊断都保存在本机。恢复数据前必须先停止机器人。",
            style="SurfaceMuted.TLabel",
        ).pack(anchor="w", pady=(4, 12))
        row = self.ttk.Frame(maintenance, style="Surface.TFrame")
        row.pack(fill="x")
        for label, command in (
            ("重新选择微信", self._select_wechat),
            ("备份配置与数据", self._backup),
            ("恢复配置与数据", self._restore),
            ("打开安装目录", self._open_install_root),
        ):
            self.ttk.Button(row, text=label, command=command, style="Secondary.TButton").pack(side="left", padx=(0, 8))

    def _build_model_page(self) -> None:
        page = self._new_page(
            "model",
            "回复模型",
            "配置用于机器人回复的 OpenAI 兼容模型。密钥只保存在本机安装目录中，界面默认遮罩。",
        )
        surface = self._surface(page, padding=24)
        surface.pack(fill="x")
        profile = load_provider_profile(self.install_root)
        self.model_presets = {
            str(item["name"]): item
            for item in MODEL_PROVIDER_PRESETS
            if item.get("service_kind") == "chat"
        }
        default_preset = next(
            item for item in MODEL_PROVIDER_PRESETS if item.get("id") == "deepseek"
        )
        selected_preset = next(
            (
                str(item["name"])
                for item in self.model_presets.values()
                if profile is not None
                and str(item.get("api_base") or "").rstrip("/") == profile.api_base.rstrip("/")
                and str(item.get("model") or "") == profile.model
            ),
            "其他兼容服务" if profile is not None else str(default_preset["name"]),
        )
        self.model_notice_var = self.tk.StringVar()
        self.ttk.Label(surface, textvariable=self.model_notice_var, style="SurfaceMuted.TLabel", wraplength=760).pack(anchor="w", pady=(0, 12))
        self.model_vars = {
            "preset": self.tk.StringVar(
                value=selected_preset
            ),
            "api_base": self.tk.StringVar(
                value=profile.api_base if profile else str(default_preset["api_base"])
            ),
            "model": self.tk.StringVar(
                value=profile.model if profile else str(default_preset["model"])
            ),
            "api_key": self.tk.StringVar(value=profile.api_key if profile else ""),
            "show_key": self.tk.BooleanVar(value=False),
        }
        preset_row = self.ttk.Frame(surface, style="Surface.TFrame")
        preset_row.pack(fill="x", pady=7)
        self.ttk.Label(preset_row, text="模型类型", style="Surface.TLabel", width=15).pack(side="left")
        combo = self.ttk.Combobox(
            preset_row,
            textvariable=self.model_vars["preset"],
            values=tuple(self.model_presets),
            state="readonly",
        )
        combo.pack(side="left", fill="x", expand=True)
        combo.bind("<<ComboboxSelected>>", self._model_preset_changed)
        self._form_row(surface, "API 地址", self.model_vars["api_base"])
        self._form_row(surface, "模型名称", self.model_vars["model"])
        self.api_key_entry, key_row = self._form_row(surface, "API Key", self.model_vars["api_key"], secret=True)
        self.ttk.Checkbutton(key_row, text="显示", variable=self.model_vars["show_key"], command=self._toggle_api_key).pack(side="left", padx=(8, 0))
        actions = self.ttk.Frame(surface, style="Surface.TFrame")
        actions.pack(fill="x", pady=(16, 0))
        self.model_save_button = self.ttk.Button(actions, text="保存模型配置", style="Primary.TButton", command=self._save_model)
        self.model_save_button.pack(side="right")

    def _build_plugins_page(self) -> None:
        page = self._new_page(
            "plugins",
            "功能管理",
            "管理已经随商业版本交付的功能。设置可单独保存，机器人运行时支持立即应用。",
        )
        surface = self._surface(page, padding=20)
        surface.pack(fill="both", expand=True)
        self.plugin_notice_var = self.tk.StringVar(value="进入功能配置中心可修改参数、独立模型和高级设置。")
        self.ttk.Label(surface, textvariable=self.plugin_notice_var, style="SurfaceMuted.TLabel").pack(anchor="w", pady=(0, 12))
        tree_box = self.ttk.Frame(surface, style="Surface.TFrame")
        tree_box.pack(fill="both", expand=True)
        self.plugin_tree = self.ttk.Treeview(tree_box, columns=("name", "state"), show="headings", selectmode="browse")
        self.plugin_tree.heading("name", text="功能名称")
        self.plugin_tree.heading("state", text="当前状态")
        self.plugin_tree.column("name", width=500, anchor="w")
        self.plugin_tree.column("state", width=140, anchor="center")
        scrollbar = self.ttk.Scrollbar(tree_box, orient="vertical", command=self.plugin_tree.yview)
        self.plugin_tree.configure(yscrollcommand=scrollbar.set)
        self.plugin_tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        actions = self.ttk.Frame(surface, style="Surface.TFrame")
        actions.pack(fill="x", pady=(12, 0))
        self.ttk.Button(actions, text="刷新列表", command=self._refresh_plugins, style="Secondary.TButton").pack(side="left")
        self.plugin_center_button = self.ttk.Button(
            actions,
            text="打开功能配置中心",
            command=self._open_plugin_center,
            style="Primary.TButton",
        )
        self.plugin_center_button.pack(side="right")

    def _build_logs_page(self) -> None:
        page = self._new_page(
            "logs",
            "运行日志",
            "查看最近运行记录。模型密钥等敏感字段会在显示和诊断导出时自动遮罩。",
        )
        toolbar = self.ttk.Frame(page, style="App.TFrame")
        toolbar.pack(fill="x", pady=(0, 10))
        self.log_catalog = log_sources(self.install_root)
        self.log_source_var = self.tk.StringVar(value=self.log_catalog[0].display_name)
        self.log_combo = self.ttk.Combobox(toolbar, textvariable=self.log_source_var, values=tuple(item.display_name for item in self.log_catalog), state="readonly", width=18)
        self.log_combo.pack(side="left")
        self.log_combo.bind("<<ComboboxSelected>>", lambda _event: self._refresh_logs())
        self.ttk.Button(toolbar, text="刷新", command=self._refresh_logs, style="Secondary.TButton").pack(side="left", padx=8)
        self.ttk.Button(toolbar, text="导出脱敏诊断", command=self._export_diagnostics, style="Secondary.TButton").pack(side="left")
        self.ttk.Button(toolbar, text="打开日志目录", command=self._open_logs_root, style="Secondary.TButton").pack(side="right")
        text_box = self.tk.Frame(page, background=COLORS.ink, padx=1, pady=1)
        text_box.pack(fill="both", expand=True)
        self.log_text = self.tk.Text(
            text_box,
            background="#0D1826",
            foreground="#D9E5F2",
            insertbackground="#FFFFFF",
            selectbackground="#275D96",
            font=FONT_MONO,
            relief="flat",
            wrap="none",
            padx=14,
            pady=12,
        )
        scroll_y = self.ttk.Scrollbar(text_box, orient="vertical", command=self.log_text.yview)
        scroll_x = self.ttk.Scrollbar(text_box, orient="horizontal", command=self.log_text.xview)
        self.log_text.configure(yscrollcommand=scroll_y.set, xscrollcommand=scroll_x.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scroll_y.grid(row=0, column=1, sticky="ns")
        scroll_x.grid(row=1, column=0, sticky="ew")
        text_box.rowconfigure(0, weight=1)
        text_box.columnconfigure(0, weight=1)

    def _show_page(self, key: str) -> None:
        if key not in self.pages:
            return
        for page in self.pages.values():
            page.pack_forget()
        self.pages[key].pack(fill="both", expand=True)
        self.current_page = key
        self._paint_navigation()
        title = next(label for item_key, label in self.NAV_ITEMS if item_key == key)
        self.root.title(f"{self.brand.display_name} · {title}")
        if key == "plugins":
            self._refresh_plugins()
        elif key == "logs":
            self._refresh_logs()

    def _schedule_next_update_check(self) -> None:
        if self.update_channel is None or not self.update_channel.enabled:
            return
        if self._update_after_id is not None:
            try:
                self.root.after_cancel(self._update_after_id)
            except self.tk.TclError:
                pass
        self._update_after_id = self.root.after(
            self.update_channel.check_interval_seconds * 1000,
            lambda: self._check_updates(manual=False),
        )

    def _check_updates(self, *, manual: bool) -> None:
        from tkinter import messagebox

        if self.update_checking:
            return
        if self.update_channel is None:
            if manual:
                messagebox.showerror(
                    "无法检查更新",
                    self.update_channel_error or "更新服务配置不可用。",
                    parent=self.root,
                )
            return
        if not self.update_channel.enabled:
            if manual:
                messagebox.showinfo(
                    "更新服务尚未启用",
                    "当前研发版本还没有绑定正式更新地址。配置发布地址后，客户会自动收到新版本弹窗。",
                    parent=self.root,
                )
            return
        self.update_checking = True
        self.update_button.configure(state="disabled", text="检查中…")

        def worker() -> None:
            try:
                release = check_for_update(
                    self.update_channel,
                    current_version=__version__,
                )
                if release is None:
                    self.events.put(("update_current", manual))
                else:
                    self.events.put(("update_available", (release, manual)))
            except (OSError, UpdateError, ValueError) as exc:
                self.events.put(("update_check_error", (str(exc), manual)))

        threading.Thread(target=worker, daemon=True).start()

    def _show_update_dialog(self, release: UpdateRelease) -> None:
        if self.update_dialog is not None and self.update_dialog.exists():
            if self.update_dialog.release.version == release.version:
                self.update_dialog.lift()
                return
            self.update_dialog.close()
        self.update_dialog = UpdateDialog(
            self.root,
            release,
            current_version=__version__,
            on_install=self._begin_update,
        )

    def _begin_update(self, release: UpdateRelease) -> None:
        from tkinter import messagebox

        if self.update_dialog is None or not self.update_dialog.exists():
            return
        if not getattr(sys, "frozen", False):
            self.update_dialog.set_error("开发运行方式不执行自更新，请使用冻结后的客户程序验证")
            return
        snapshot = runtime_snapshot()
        if snapshot.engine_ready or snapshot.connection_ready:
            if self.supervisor is None or not self.supervisor.running:
                messagebox.showwarning(
                    "请先停止机器人",
                    "检测到机器人正在其他窗口运行。请先在原控制台停止，再点击更新。",
                    parent=self.root,
                )
                return
            if not messagebox.askyesno(
                "停止并更新",
                "更新期间机器人会暂时停止，安装完成后控制台会自动重新打开。是否继续？",
                parent=self.root,
            ):
                return
            self.update_dialog.set_preparing("正在安全停止机器人…")

            def stop_worker() -> None:
                try:
                    assert self.supervisor is not None
                    self.supervisor.stop()
                    self.events.put(("update_service_stopped", release))
                except (OSError, ServiceLaunchError, ValueError) as exc:
                    self.events.put(("update_prepare_error", str(exc)))

            threading.Thread(target=stop_worker, daemon=True).start()
            return
        self.update_dialog.set_preparing()
        self._prepare_update_in_background(release)

    def _prepare_update_in_background(self, release: UpdateRelease) -> None:
        if self.update_channel is None:
            if self.update_dialog is not None:
                self.update_dialog.set_error("更新服务配置不可用")
            return

        def progress(stage: str, current: int, total: int) -> None:
            self.events.put(("update_progress", (stage, current, total)))

        def worker() -> None:
            try:
                prepared = prepare_update(
                    release,
                    self.update_channel,
                    self.install_root,
                    progress_callback=progress,
                )
                self.events.put(("update_prepared", prepared))
            except Exception as exc:
                self.events.put(("update_prepare_error", str(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def _handoff_update(self, prepared: PreparedUpdate) -> None:
        if self.update_dialog is None or not self.update_dialog.exists():
            return
        try:
            plan = create_update_plan(prepared, self.install_root)
            launch_update_helper(plan)
        except (OSError, UpdateError, ValueError) as exc:
            self.update_dialog.set_error(str(exc))
            return
        self.update_dialog.set_handoff()
        self.root.after(450, self.root.destroy)

    def _show_update_result(self) -> None:
        from tkinter import messagebox

        result = consume_update_result(self.install_root)
        if result is None:
            return
        version = str(result.get("version") or __version__)
        if result.get("ok") is True:
            messagebox.showinfo(
                "更新完成",
                f"已成功更新到 v{version}。机器人不会自动启动，请在运行中心点击“一键启动”。",
                parent=self.root,
            )
        else:
            message = str(result.get("message") or "未知错误")
            messagebox.showerror(
                "更新未完成",
                f"原版本已保留并重新打开。\n\n原因：{message}\n\n请检查磁盘空间和安全软件后重试。",
                parent=self.root,
            )

    def _timer(self) -> None:
        try:
            while True:
                kind, message = self.events.get_nowait()
                if kind == "phase":
                    self.start_phase = message
                elif kind == "error":
                    self.start_phase = "error"
                    self.home_status_var.set("启动未完成")
                    self.home_detail_var.set(message)
                    self._show_page("logs")
                elif kind == "started":
                    self.start_phase = "login"
                elif kind == "stopped":
                    self.start_phase = "idle"
                elif kind == "backup_done":
                    from tkinter import messagebox

                    messagebox.showinfo("备份完成", f"备份已保存：\n{message}\n\n文件包含客户配置和模型密钥，请妥善保管。", parent=self.root)
                elif kind == "restore_done":
                    from tkinter import messagebox

                    messagebox.showinfo("恢复完成", f"配置与数据已恢复。恢复前回滚点：\n{message}", parent=self.root)
                elif kind == "update_current":
                    from tkinter import messagebox

                    self.update_checking = False
                    self.update_button.configure(state="normal", text="检查更新")
                    if bool(message):
                        messagebox.showinfo(
                            "已是最新版本",
                            f"当前 v{__version__} 已是最新版本。",
                            parent=self.root,
                        )
                    self._schedule_next_update_check()
                elif kind == "update_available":
                    release, manual = message
                    assert isinstance(release, UpdateRelease)
                    self.update_checking = False
                    self.available_update = release
                    self.update_button.configure(state="normal", text="发现新版本")
                    if bool(manual) or self.notified_update_version != release.version:
                        self.notified_update_version = release.version
                        self._show_update_dialog(release)
                    self._schedule_next_update_check()
                elif kind == "update_check_error":
                    from tkinter import messagebox

                    error, manual = message
                    self.update_checking = False
                    self.update_button.configure(state="normal", text="检查更新")
                    if bool(manual):
                        messagebox.showerror(
                            "检查更新失败",
                            f"{error}\n\n请检查网络后重试。",
                            parent=self.root,
                        )
                    self._schedule_next_update_check()
                elif kind == "update_service_stopped":
                    assert isinstance(message, UpdateRelease)
                    if self.update_dialog is not None:
                        self.update_dialog.set_preparing()
                    self._prepare_update_in_background(message)
                elif kind == "update_progress":
                    stage, current, total = message
                    if self.update_dialog is not None and self.update_dialog.exists():
                        self.update_dialog.set_progress(
                            str(stage), int(current), int(total)
                        )
                elif kind == "update_prepared":
                    assert isinstance(message, PreparedUpdate)
                    self._handoff_update(message)
                elif kind == "update_prepare_error":
                    if self.update_dialog is not None and self.update_dialog.exists():
                        self.update_dialog.set_error(sanitize_log_text(str(message)))
        except queue.Empty:
            pass
        self._refresh_status()
        self.root.after(1000, self._timer)

    def _refresh_status(self) -> None:
        snapshot = runtime_snapshot()
        self.last_snapshot = snapshot
        any_ready = snapshot.engine_ready or snapshot.connection_ready
        owned = self.supervisor is not None and bool(self.supervisor.running)
        if snapshot.online:
            self.start_phase = "online"
            status_text = "运行中"
            detail_text = "自动回复服务正常运行"
            self.account_var.set(f"已登录：{snapshot.wechat_identity}")
            self.sidebar_state_var.set("机器人在线")
            self.wechat_state_var.set("已登录")
            self.robot_state_var.set("运行中")
            self.message_state_var.set("运行中")
            self.recent_title_var.set("机器人正在运行")
            self.recent_detail_var.set("当前微信账号已连接，自动回复服务正常")
            stage_index = 2
        elif snapshot.connection_ready:
            self.start_phase = "login"
            status_text = "等待登录"
            detail_text = "请在微信窗口完成登录"
            self.account_var.set("等待微信登录")
            self.sidebar_state_var.set("等待微信登录")
            self.wechat_state_var.set("等待登录")
            self.robot_state_var.set("已就绪")
            self.message_state_var.set("连接中")
            self.recent_title_var.set("微信已打开")
            self.recent_detail_var.set("正在等待账号登录")
            stage_index = 1
        elif snapshot.engine_ready:
            status_text = "正在启动"
            detail_text = "正在打开并连接微信"
            self.account_var.set("微信正在启动")
            self.sidebar_state_var.set("正在连接微信")
            self.wechat_state_var.set("连接中")
            self.robot_state_var.set("已就绪")
            self.message_state_var.set("正在启动")
            stage_index = 0
        elif self.start_phase in {"check", "engine", "connection"}:
            status_text = {"check": "正在检查", "engine": "正在启动", "connection": "正在打开微信"}[self.start_phase]
            detail_text = "请保持控制台开启"
            self.sidebar_state_var.set("正在启动")
            self.wechat_state_var.set("未连接" if self.start_phase != "connection" else "正在打开")
            self.robot_state_var.set("检查中" if self.start_phase == "check" else "启动中")
            self.message_state_var.set("等待启动")
            stage_index = 0
        elif self.start_phase == "error":
            status_text = "启动失败"
            detail_text = "请查看运行日志并重试"
            self.sidebar_state_var.set("启动未完成")
            self.wechat_state_var.set("未连接")
            self.robot_state_var.set("需检查")
            self.message_state_var.set("未启动")
            self.recent_title_var.set("最近一次启动未完成")
            self.recent_detail_var.set("请进入运行日志查看原因")
            stage_index = -1
        else:
            status_text = "等待启动"
            detail_text = "所有服务已准备就绪"
            self.account_var.set("尚未登录")
            self.sidebar_state_var.set("当前未运行")
            self.wechat_state_var.set("未连接")
            self.robot_state_var.set("已就绪")
            self.message_state_var.set("等待启动")
            stage_index = -1
        self.home_status_var.set(status_text)
        self.home_detail_var.set(detail_text)
        self.launch_canvas.itemconfigure(self.launch_status_item, text=status_text)
        self.launch_canvas.itemconfigure(self.launch_detail_item, text=detail_text)
        power_color = COLORS.danger if self.start_phase == "error" else (COLORS.primary if any_ready and not snapshot.online else COLORS.success)
        self.launch_canvas.itemconfigure(self.power_arc, outline=power_color)
        self.launch_canvas.itemconfigure(self.power_line, fill=power_color)
        self._paint_rail(stage_index, snapshot.online)
        busy = self.start_phase in {"check", "engine", "connection", "login"}
        self.wechat_state_label.configure(foreground=COLORS.success if snapshot.online else (COLORS.warning if snapshot.connection_ready else COLORS.text_muted))
        self.robot_state_label.configure(foreground=COLORS.success if snapshot.engine_ready or self.start_phase == "idle" else (COLORS.warning if busy else COLORS.danger))
        self.message_state_label.configure(foreground=COLORS.success if snapshot.online else (COLORS.danger if self.start_phase == "error" else COLORS.warning))
        if snapshot.online:
            button_text, button_state = ("停止运行", "normal") if owned else ("正在运行", "disabled")
        elif snapshot.connection_ready and owned:
            button_text, button_state = "停止运行", "normal"
        elif any_ready or busy:
            button_text, button_state = "启动中…", "disabled"
        else:
            button_text, button_state = "一键启动", "normal"
        button_color = COLORS.primary if button_state == "normal" else "#8DB3E8"
        self.start_button.configure(state=button_state, text=button_text, background=button_color)

    def _paint_rail(self, active_index: int, online: bool) -> None:
        for index, (node, number, title) in enumerate(self.rail_nodes):
            if online or index < active_index:
                background, foreground, text_color, outline = COLORS.success, "#FFFFFF", COLORS.success, COLORS.success
            elif index == active_index:
                background, foreground, text_color, outline = COLORS.primary, "#FFFFFF", COLORS.primary, COLORS.primary
            else:
                background, foreground, text_color, outline = COLORS.canvas, COLORS.primary, COLORS.text, COLORS.primary
            self.launch_canvas.itemconfigure(node, fill=background, outline=outline)
            self.launch_canvas.itemconfigure(number, fill=foreground)
            self.launch_canvas.itemconfigure(title, fill=text_color)

    def _primary_action(self) -> None:
        if self.supervisor and self.supervisor.running:
            self._stop()
        else:
            self._start()

    def _progress(self, event: str, spec: ServiceSpec) -> None:
        if spec.name == "astrbot":
            phase = "engine"
        else:
            phase = "connection" if event == "starting" else "login"
        self.events.put(("phase", phase))

    def _start(self) -> None:
        if self.supervisor and self.supervisor.running:
            return
        self.start_phase = "check"
        self._refresh_status()
        try:
            report = collect_diagnostics(self.install_root)
            errors = diagnostic_error_messages(report)
            if errors:
                preview = "\n".join(f"• {item}" for item in errors[:8])
                raise ValueError(f"启动前检查未通过：\n{preview}")
            self.supervisor = ProcessSupervisor(build_service_specs(self.install_root), progress_callback=self._progress)
        except (OSError, ValueError) as exc:
            self.supervisor = None
            self.start_phase = "error"
            self.home_status_var.set("启动前检查未通过")
            self.home_detail_var.set(str(exc))
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
        self.start_button.configure(state="disabled", text="正在停止…")
        self.home_status_var.set("正在停止")
        self.launch_canvas.itemconfigure(self.launch_status_item, text="正在停止")

        def worker() -> None:
            assert self.supervisor is not None
            self.supervisor.stop()
            self.events.put(("stopped", ""))

        threading.Thread(target=worker, daemon=True).start()

    def _save_robot(self) -> None:
        try:
            profile = BrandProfile(
                display_name=self.robot_vars["display_name"].get(),
                short_name=self.robot_vars["short_name"].get(),
                persona_name=self.robot_vars["persona_name"].get(),
                avatar_path=self.brand.avatar_path,
                welcome_text=self.robot_vars["welcome_text"].get(),
            ).normalized()
            saved = save_robot_settings(self.install_root, RobotSettings(profile=profile, admin_id=self.robot_vars["admin_id"].get()))
            self.robot_settings = saved
            self.brand = saved.profile
            self.brand_title.configure(text=self.brand.display_name)
            self.robot_notice_var.set("已保存。机器人正在运行时，下次重新启动后应用全部回复设置。")
            self.root.title(f"{self.brand.display_name} · 机器人设置")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self.robot_notice_var.set(f"保存失败：{exc}")

    def _model_preset_changed(self, _event=None) -> None:
        preset = self.model_presets.get(self.model_vars["preset"].get())
        if preset is not None:
            self.model_vars["api_base"].set(str(preset.get("api_base") or ""))
            self.model_vars["model"].set(str(preset.get("model") or ""))

    def _toggle_api_key(self) -> None:
        self.api_key_entry.configure(show="" if self.model_vars["show_key"].get() else "●")

    def _save_model(self) -> None:
        try:
            current = load_provider_profile(self.install_root)
            profile = OpenAIProviderProfile(
                api_key=self.model_vars["api_key"].get(),
                api_base=self.model_vars["api_base"].get(),
                model=self.model_vars["model"].get(),
                source_id=(
                    current.source_id
                    if current is not None
                    and current.api_base.rstrip("/")
                    == self.model_vars["api_base"].get().strip().rstrip("/")
                    else provider_source_id(
                        self.model_vars["api_base"].get(),
                        self.model_vars["model"].get(),
                    )
                ),
            )
            save_provider_profile(self.install_root, profile)
            self.model_notice_var.set("模型配置已保存。机器人正在运行时，下次重新启动后生效。")
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            self.model_notice_var.set(f"保存失败：{exc}")

    def _refresh_plugins(self) -> None:
        if not hasattr(self, "plugin_tree"):
            return
        self.plugin_tree.delete(*self.plugin_tree.get_children())
        plugins = load_plugins(self.install_root)
        for item in plugins:
            self.plugin_tree.insert("", "end", iid=item.module_name, values=(item.display_name, "已启用" if item.enabled else "已停用"))
        if not plugins:
            self.plugin_notice_var.set("当前版本没有可管理的扩展功能。后续交付的功能会自动出现在这里。")
        else:
            self.plugin_notice_var.set(f"已交付 {len(plugins)} 项功能。进入配置中心可管理启停、参数和重新载入设置。")

    def _open_plugin_center(self) -> None:
        try:
            launch_plugin_center(self.install_root)
            self.plugin_notice_var.set("功能配置中心已打开。若窗口已存在，请查看任务栏。")
        except OSError as exc:
            self.plugin_notice_var.set(f"配置中心打开失败：{exc}")

    def _selected_log(self):
        label = self.log_source_var.get()
        return next((item for item in self.log_catalog if item.display_name == label), self.log_catalog[0])

    def _refresh_logs(self) -> None:
        if not hasattr(self, "log_text"):
            return
        source = self._selected_log()
        content = tail_log(source.path)
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", content)
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def _open_install_root(self) -> None:
        if os.name == "nt":
            os.startfile(self.install_root)  # type: ignore[attr-defined]

    def _open_logs_root(self) -> None:
        logs = self.install_root / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(logs)  # type: ignore[attr-defined]

    def _select_wechat(self) -> None:
        from tkinter import filedialog, messagebox

        selected = filedialog.askopenfilename(parent=self.root, title="重新选择微信主程序 Weixin.exe", filetypes=(("微信主程序", "Weixin.exe"), ("可执行文件", "*.exe")))
        if not selected:
            return
        try:
            apply_wechat_change(self.install_root, selected)
            messagebox.showinfo("已保存", "微信路径与版本已更新。正在运行时请重新启动机器人后生效。", parent=self.root)
        except (OSError, ValueError, KeyError, json.JSONDecodeError) as exc:
            messagebox.showerror("微信路径不可用", str(exc), parent=self.root)

    def _export_diagnostics(self) -> None:
        from tkinter import filedialog, messagebox

        target = filedialog.asksaveasfilename(parent=self.root, title="保存脱敏诊断", defaultextension=".json", initialfile="robot-diagnostics.json", filetypes=(("JSON", "*.json"),))
        if not target:
            return
        try:
            report = collect_diagnostics(self.install_root)
            Path(target).write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            messagebox.showinfo("导出完成", "诊断文件已脱敏保存。", parent=self.root)
        except OSError as exc:
            messagebox.showerror("导出失败", str(exc), parent=self.root)

    def _runtime_ports_used(self) -> bool:
        return self.last_snapshot.engine_ready or self.last_snapshot.connection_ready

    def _backup(self) -> None:
        from tkinter import filedialog, messagebox

        if self._runtime_ports_used():
            messagebox.showwarning("请先停止机器人", "为避免备份到写入一半的数据，请先停止机器人后再备份。", parent=self.root)
            return
        target = filedialog.asksaveasfilename(parent=self.root, title="备份机器人配置与数据", defaultextension=".zip", initialfile="robot-backup.zip", filetypes=(("ZIP 备份", "*.zip"),))
        if not target:
            return

        def worker() -> None:
            try:
                create_customer_backup(self.install_root, target)
                self.events.put(("backup_done", str(Path(target).resolve())))
            except (OSError, BackupError) as exc:
                self.events.put(("error", f"备份失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _restore(self) -> None:
        from tkinter import filedialog, messagebox

        if self._runtime_ports_used():
            messagebox.showwarning("请先停止机器人", "恢复会覆盖配置和数据，请先停止机器人。", parent=self.root)
            return
        selected = filedialog.askopenfilename(parent=self.root, title="选择机器人备份", filetypes=(("ZIP 备份", "*.zip"),))
        if not selected or not messagebox.askyesno("确认恢复", "恢复会覆盖备份中包含的配置与数据。程序会先自动创建恢复前回滚点，是否继续？", parent=self.root):
            return

        def worker() -> None:
            try:
                result = restore_customer_backup(self.install_root, selected)
                self.events.put(("restore_done", str(result["recovery_point"])))
            except (OSError, BackupError, zipfile.BadZipFile) as exc:
                self.events.put(("error", f"恢复失败：{exc}"))

        threading.Thread(target=worker, daemon=True).start()

    def _close(self) -> None:
        from tkinter import messagebox

        if self.update_dialog is not None and self.update_dialog.exists() and self.update_dialog.busy:
            messagebox.showinfo(
                "正在更新",
                "请等待下载和校验完成，更新期间不要关闭控制台。",
                parent=self.root,
            )
            self.update_dialog.lift()
            return
        if self.supervisor and self.supervisor.running:
            if not messagebox.askyesno("退出控制台", "退出会同时停止机器人，是否继续？", parent=self.root):
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


__all__ = ["ControlCenter", "apply_brand_change", "apply_wechat_change", "load_brand_profile", "run_control_center"]
