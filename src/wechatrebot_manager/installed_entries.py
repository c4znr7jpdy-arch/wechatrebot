from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid


START_EXECUTABLE_NAME = "启动机器人.exe"


def copy_manager_executable(root: Path, name: str, source: Path) -> bool:
    if not source.is_file():
        return False
    target = root / name
    try:
        source_stat = source.stat()
        target_stat = target.stat()
        if (
            source_stat.st_size == target_stat.st_size
            and source_stat.st_mtime_ns == target_stat.st_mtime_ns
        ):
            return True
    except FileNotFoundError:
        pass
    temporary = root / f".{name}.{uuid.uuid4().hex}.tmp"
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return True


def ensure_launcher_executable(install_root: str | Path) -> bool:
    root = Path(install_root).expanduser().resolve()
    return copy_manager_executable(
        root, START_EXECUTABLE_NAME, root / "manager" / "WeChatReBot.exe"
    )


def is_start_entrypoint(executable: str | Path | None = None) -> bool:
    return Path(executable or sys.executable).name.casefold() == START_EXECUTABLE_NAME.casefold()


def launch_installed_manager(install_root: str | Path) -> None:
    root = Path(install_root).expanduser().resolve()
    target = root / "manager" / "WeChatReBot.exe"
    if not target.is_file():
        raise FileNotFoundError("机器人启动程序缺失，请重新安装软件。")
    environment = os.environ.copy()
    environment["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    subprocess.Popen(
        [str(target), "gui", "--install-root", str(root)],
        cwd=root,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
