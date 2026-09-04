from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


INSTALL_MARKER = ".wechatrebot-install.json"
DEFAULT_REQUIRED_BYTES = 5 * 1024**3
MAX_INSTALL_PATH_CHARS = 180
WINDOWS_LEGACY_MAX_PATH_CHARS = 259
_TRANSACTION_DIRECTORY_CHARS = len("updates") + 1 + len(".rollback-") + 32
_TRANSACTION_FILENAME_CHARS = 8


@dataclass(frozen=True)
class InstallPathValidation:
    path: Path
    errors: tuple[str, ...]
    warnings: tuple[str, ...]
    available_bytes: int | None

    @property
    def ok(self) -> bool:
        return not self.errors


def _nearest_existing_parent(path: Path) -> Path:
    candidate = path
    while not candidate.exists() and candidate.parent != candidate:
        candidate = candidate.parent
    return candidate


def _normalized(path: Path) -> Path:
    return Path(os.path.abspath(os.path.expandvars(os.path.expanduser(str(path)))))


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _protected_roots() -> tuple[Path, ...]:
    values = {
        os.environ.get("WINDIR"),
        os.environ.get("ProgramFiles"),
        os.environ.get("ProgramFiles(x86)"),
        os.environ.get("ProgramData"),
        tempfile.gettempdir(),
    }
    return tuple(_normalized(Path(value)) for value in values if value)


def _drive_type(path: Path) -> int | None:
    if os.name != "nt":
        return None
    anchor = path.anchor
    if not anchor:
        return None
    return int(ctypes.windll.kernel32.GetDriveTypeW(anchor))


def validate_install_path(
    value: str | Path,
    *,
    required_bytes: int = DEFAULT_REQUIRED_BYTES,
    payload_relative_paths: Iterable[str | Path] = (),
) -> InstallPathValidation:
    raw = str(value).strip()
    errors: list[str] = []
    warnings: list[str] = []

    if not raw:
        return InstallPathValidation(Path(), ("请选择安装目录。",), (), None)

    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute():
        errors.append("安装目录必须是绝对路径。")
    path = _normalized(expanded)

    if str(path).startswith("\\\\"):
        errors.append("不支持网络路径或 UNC 路径，请选择本机固定磁盘。")

    payload_lengths = tuple(
        len(Path(relative).as_posix()) for relative in payload_relative_paths
    )
    longest_payload_path = max(payload_lengths, default=0)
    final_target_overhead = 1 + longest_payload_path
    transaction_file_overhead = (
        1 + _TRANSACTION_DIRECTORY_CHARS + 1 + _TRANSACTION_FILENAME_CHARS
    )
    payload_aware_limit = (
        WINDOWS_LEGACY_MAX_PATH_CHARS
        - max(final_target_overhead, transaction_file_overhead)
        if longest_payload_path
        else MAX_INSTALL_PATH_CHARS
    )
    maximum_path_chars = min(MAX_INSTALL_PATH_CHARS, payload_aware_limit)
    if len(str(path)) > maximum_path_chars:
        errors.append(
            f"安装路径过长（{len(str(path))} 个字符），当前载荷最多支持 "
            f"{max(0, maximum_path_chars)} 个字符的安装根目录。"
        )

    if path.parent == path:
        errors.append("不能直接安装到磁盘根目录。")

    for protected in _protected_roots():
        if path == protected or _is_within(path, protected):
            errors.append(f"不能安装到系统或临时目录：{protected}")
            break

    drive_type = _drive_type(path)
    if drive_type not in (None, 3):
        errors.append("安装目录必须位于本机固定磁盘。")

    existing_parent = _nearest_existing_parent(path)
    available_bytes: int | None = None
    if not existing_parent.exists():
        errors.append("无法找到可用的上级目录。")
    else:
        if not os.access(existing_parent, os.W_OK):
            errors.append(f"上级目录不可写：{existing_parent}")
        try:
            available_bytes = shutil.disk_usage(existing_parent).free
        except OSError as exc:
            errors.append(f"无法读取磁盘剩余空间：{exc}")
        if available_bytes is not None and available_bytes < required_bytes:
            errors.append(
                f"磁盘空间不足，需要至少 {required_bytes / 1024**3:.1f} GiB，"
                f"当前可用 {available_bytes / 1024**3:.1f} GiB。"
            )

    if path.exists():
        if not path.is_dir():
            errors.append("所选路径不是文件夹。")
        else:
            entries = list(path.iterdir())
            if entries and not (path / INSTALL_MARKER).is_file():
                errors.append("所选目录非空且不是已有的机器人安装目录。")
            elif (path / INSTALL_MARKER).is_file():
                warnings.append("检测到已有安装，将进入修复或升级流程。")

    return InstallPathValidation(path, tuple(errors), tuple(warnings), available_bytes)


def prepare_install_layout(path: Path) -> None:
    """Create the confirmed install layout; callers must ask for confirmation first."""
    for child in (
        "app",
        "runtime",
        "config",
        "data",
        "logs",
        "cache",
        "backups",
        "updates",
    ):
        (path / child).mkdir(parents=True, exist_ok=True)
