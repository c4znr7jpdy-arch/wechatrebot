"""Prepare the pinned Office renderer inside staging, never in a live install."""
from __future__ import annotations

from pathlib import Path
import os
import shutil
import subprocess
import sys


def build_office_renderer(plugin_root: Path) -> None:
    renderer = plugin_root / "word_renderer_js"
    if not renderer.is_dir():
        raise RuntimeError("Office 助手缺少文档渲染器源码。")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not npm:
        raise RuntimeError("构建 Office 助手需要 Node.js 和 npm；客户运行时使用随包附带的 Node。")
    for arguments in (
        ["ci", "--ignore-scripts", "--no-audit", "--no-fund"],
        ["run", "build"],
        ["prune", "--omit=dev", "--ignore-scripts", "--no-audit", "--no-fund"],
    ):
        subprocess.run([npm, *arguments], cwd=renderer, check=True, timeout=300)
    if not (renderer / "dist/cli.js").is_file():
        raise RuntimeError("Office 文档渲染器构建没有生成 cli.js。")
    # Type-only declarations are build inputs, not executable dependencies.
    # In particular @types/node includes example developer paths in comments.
    for declaration in (renderer / "node_modules").rglob("*.d.ts"):
        declaration.unlink()


if __name__ == "__main__":
    build_office_renderer(Path(sys.argv[1]).resolve())
