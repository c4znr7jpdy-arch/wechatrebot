from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from .brand import BrandProfile, save_brand_profile
from .install_paths import INSTALL_MARKER, prepare_install_layout, validate_install_path
from .wechat_path import discover_wechat_executables, validate_wechat_executable


def _choose_install_root(root) -> Path | None:
    from tkinter import filedialog, messagebox

    while True:
        selected = filedialog.askdirectory(
            parent=root,
            title="选择机器人安装目录",
            mustexist=False,
        )
        if not selected:
            return None
        result = validate_install_path(selected)
        if result.ok:
            return result.path
        messagebox.showerror("安装目录不可用", "\n".join(result.errors), parent=root)


def _choose_wechat(root):
    from tkinter import filedialog, messagebox

    discovered = tuple(item for item in discover_wechat_executables() if item.supported)
    if len(discovered) == 1:
        item = discovered[0]
        if messagebox.askyesno(
            "确认微信路径",
            f"发现受支持的微信：\n{item.path}\n版本：{item.version}\n\n是否使用？",
            parent=root,
        ):
            return item

    while True:
        selected = filedialog.askopenfilename(
            parent=root,
            title="选择微信主程序 Weixin.exe",
            filetypes=(("微信主程序", "Weixin.exe"), ("可执行文件", "*.exe")),
        )
        if not selected:
            return None
        item = validate_wechat_executable(selected)
        if item.supported:
            return item
        messagebox.showerror("微信路径不可用", "\n".join(item.errors), parent=root)


def run_bootstrap() -> int:
    if os.name != "nt":
        print("首次配置向导仅支持 Windows。", file=sys.stderr)
        return 2

    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog

    root = tk.Tk()
    root.withdraw()

    install_root = _choose_install_root(root)
    if install_root is None:
        return 2

    wechat = _choose_wechat(root)
    if wechat is None:
        return 2

    display_name = simpledialog.askstring(
        "机器人名称",
        "请输入机器人名称：",
        initialvalue="智能机器人",
        parent=root,
    )
    if not display_name:
        return 2
    short_name = simpledialog.askstring(
        "机器人简称",
        "请输入机器人简称：",
        initialvalue=display_name[:8],
        parent=root,
    )
    if not short_name:
        return 2
    avatar_path = filedialog.askopenfilename(
        parent=root,
        title="选择机器人头像（可取消，稍后设置）",
        filetypes=(("图片", "*.png *.jpg *.jpeg *.webp"), ("所有文件", "*.*")),
    )

    profile = BrandProfile(
        display_name=display_name,
        short_name=short_name,
        persona_name=display_name,
        avatar_path=avatar_path,
    ).normalized()
    errors = profile.validate()
    if errors:
        messagebox.showerror("品牌配置不可用", "\n".join(errors), parent=root)
        return 2

    summary = (
        f"安装目录：{install_root}\n"
        f"微信路径：{wechat.path}\n"
        f"微信版本：{wechat.version}\n"
        f"机器人名称：{profile.display_name}\n\n"
        "确认后才会创建正式安装目录。"
    )
    if not messagebox.askyesno("确认首次配置", summary, parent=root):
        return 2

    prepare_install_layout(install_root)
    save_brand_profile(install_root / "config" / "brand.json", profile)
    manager_config = {
        "schema_version": 1,
        "install_root": str(install_root),
        "wechat_exe_path": str(wechat.path),
        "wechat_version": wechat.version,
    }
    (install_root / "config" / "manager.json").write_text(
        json.dumps(manager_config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    (install_root / INSTALL_MARKER).write_text(
        json.dumps({"schema_version": 1}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    messagebox.showinfo(
        "首次配置已保存",
        "安装位置、微信路径和机器人品牌已保存。环境展开将在下一阶段接入。",
        parent=root,
    )
    return 0

