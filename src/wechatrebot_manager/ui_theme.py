from __future__ import annotations

from dataclasses import dataclass
import tkinter as tk


@dataclass(frozen=True, slots=True)
class ProductColors:
    ink: str = "#102A49"
    primary: str = "#1769E8"
    primary_hover: str = "#0E55C6"
    success: str = "#16845B"
    warning: str = "#92400E"
    danger: str = "#B33A3A"
    canvas: str = "#F8FAFD"
    surface: str = "#FFFFFF"
    surface_muted: str = "#F3F6FA"
    border: str = "#D9DFE8"
    text: str = "#0D1B2A"
    text_muted: str = "#59677B"
    sidebar_text: str = "#DCE7F5"


@dataclass(frozen=True, slots=True)
class ProductGeometry:
    window_width: int = 1254
    window_height: int = 802
    minimum_width: int = 1120
    minimum_height: int = 720
    titlebar_height: int = 56
    sidebar_width: int = 212
    content_padding_x: int = 25
    content_padding_y: int = 20
    home_upper_height: int = 405
    launcher_diameter: int = 332
    start_button_width: int = 172
    start_button_height: int = 50


COLORS = ProductColors()
GEOMETRY = ProductGeometry()
FONT_BODY = ("Microsoft YaHei UI", 10)
FONT_SMALL = ("Microsoft YaHei UI", 9)
FONT_TITLE = ("Microsoft YaHei UI", 20, "bold")
FONT_PAGE_TITLE = ("Microsoft YaHei UI", 20, "bold")
FONT_SECTION = ("Microsoft YaHei UI", 11, "bold")
FONT_STATUS = ("Bahnschrift", 18, "bold")
FONT_MONO = ("Cascadia Mono", 9)


def apply_product_theme(root, ttk_module) -> None:
    """Map DESIGN.md tokens to the shared Tk/ttk component styles."""
    style = ttk_module.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:  # pragma: no cover - defensive Tk fallback
        pass

    style.configure(".", font=FONT_BODY, background=COLORS.canvas)
    style.configure("App.TFrame", background=COLORS.canvas)
    style.configure("Surface.TFrame", background=COLORS.surface)
    style.configure("Sidebar.TFrame", background=COLORS.ink)
    style.configure("PageTitle.TLabel", font=FONT_PAGE_TITLE, background=COLORS.canvas, foreground=COLORS.text)
    style.configure("PageHint.TLabel", font=FONT_BODY, background=COLORS.canvas, foreground=COLORS.text_muted)
    style.configure("Surface.TLabel", background=COLORS.surface, foreground=COLORS.text)
    style.configure("SurfaceMuted.TLabel", background=COLORS.surface, foreground=COLORS.text_muted)
    style.configure("Section.TLabel", font=FONT_SECTION, background=COLORS.surface, foreground=COLORS.text)
    style.configure("Status.TLabel", font=FONT_STATUS, background=COLORS.surface, foreground=COLORS.text)
    style.configure("Hint.TLabel", foreground=COLORS.text_muted)
    style.configure("Success.TLabel", foreground=COLORS.success)
    style.configure("Warning.TLabel", foreground=COLORS.warning)
    style.configure("Danger.TLabel", foreground=COLORS.danger)

    style.configure(
        "Primary.TButton",
        font=("Microsoft YaHei UI", 11, "bold"),
        foreground="#FFFFFF",
        background=COLORS.primary,
        borderwidth=0,
        padding=(18, 11),
    )
    style.map(
        "Primary.TButton",
        background=[("active", COLORS.primary_hover), ("disabled", "#9BB8DF")],
        foreground=[("disabled", "#EEF3FA")],
    )
    style.configure("Secondary.TButton", padding=(13, 9))
    style.configure("Danger.TButton", foreground=COLORS.danger, padding=(13, 9))
    style.configure("Nav.TButton", anchor="w", padding=(18, 12), borderwidth=0)
    style.configure("NavActive.TButton", anchor="w", padding=(18, 12), borderwidth=0)
    style.map(
        "Nav.TButton",
        background=[("!disabled", COLORS.ink), ("active", "#1B314D")],
        foreground=[("!disabled", COLORS.sidebar_text)],
    )
    style.map(
        "NavActive.TButton",
        background=[("!disabled", COLORS.primary), ("active", COLORS.primary_hover)],
        foreground=[("!disabled", "#FFFFFF")],
    )
    style.configure("TEntry", padding=7)
    style.configure("TCombobox", padding=5)
    style.configure("Treeview", rowheight=32, fieldbackground=COLORS.surface)
    style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"))
    root.configure(background=COLORS.canvas)


__all__ = [
    "COLORS",
    "GEOMETRY",
    "FONT_BODY",
    "FONT_MONO",
    "FONT_PAGE_TITLE",
    "FONT_SMALL",
    "FONT_STATUS",
    "FONT_TITLE",
    "ProductColors",
    "ProductGeometry",
    "apply_product_theme",
]
