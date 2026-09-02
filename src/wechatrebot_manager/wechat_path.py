from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass
from pathlib import Path
import struct


SUPPORTED_WECHAT_VERSIONS = ("4.1.8.27",)


@dataclass(frozen=True)
class WechatExecutable:
    path: Path
    version: str | None
    source: str
    errors: tuple[str, ...]

    @property
    def supported(self) -> bool:
        return not self.errors


class _VSFixedFileInfo(ctypes.Structure):
    _fields_ = [
        ("dwSignature", ctypes.c_uint32),
        ("dwStrucVersion", ctypes.c_uint32),
        ("dwFileVersionMS", ctypes.c_uint32),
        ("dwFileVersionLS", ctypes.c_uint32),
        ("dwProductVersionMS", ctypes.c_uint32),
        ("dwProductVersionLS", ctypes.c_uint32),
        ("dwFileFlagsMask", ctypes.c_uint32),
        ("dwFileFlags", ctypes.c_uint32),
        ("dwFileOS", ctypes.c_uint32),
        ("dwFileType", ctypes.c_uint32),
        ("dwFileSubtype", ctypes.c_uint32),
        ("dwFileDateMS", ctypes.c_uint32),
        ("dwFileDateLS", ctypes.c_uint32),
    ]


def read_file_version(path: Path) -> str | None:
    if os.name != "nt" or not path.is_file():
        return None
    version_dll = ctypes.windll.version
    size = version_dll.GetFileVersionInfoSizeW(str(path), None)
    if not size:
        return None
    buffer = ctypes.create_string_buffer(size)
    if not version_dll.GetFileVersionInfoW(str(path), 0, size, buffer):
        return None
    value = ctypes.c_void_p()
    length = ctypes.c_uint()
    if not version_dll.VerQueryValueW(buffer, "\\", ctypes.byref(value), ctypes.byref(length)):
        return None
    info = ctypes.cast(value, ctypes.POINTER(_VSFixedFileInfo)).contents
    return ".".join(
        str(part)
        for part in (
            info.dwFileVersionMS >> 16,
            info.dwFileVersionMS & 0xFFFF,
            info.dwFileVersionLS >> 16,
            info.dwFileVersionLS & 0xFFFF,
        )
    )


def read_pe_machine(path: Path) -> int | None:
    try:
        with path.open("rb") as handle:
            if handle.read(2) != b"MZ":
                return None
            handle.seek(0x3C)
            offset_bytes = handle.read(4)
            if len(offset_bytes) != 4:
                return None
            handle.seek(struct.unpack("<I", offset_bytes)[0])
            if handle.read(4) != b"PE\x00\x00":
                return None
            machine_bytes = handle.read(2)
            return struct.unpack("<H", machine_bytes)[0] if len(machine_bytes) == 2 else None
    except OSError:
        return None


def validate_wechat_executable(
    value: str | Path,
    *,
    source: str = "manual",
    supported_versions: tuple[str, ...] = SUPPORTED_WECHAT_VERSIONS,
) -> WechatExecutable:
    raw_path = Path(os.path.expandvars(os.path.expanduser(str(value).strip())))
    errors: list[str] = []
    if not raw_path.is_absolute():
        errors.append("微信路径必须是绝对路径。")
    path = Path(os.path.abspath(raw_path))
    if not path.is_file():
        errors.append("所选微信程序不存在。")
    if path.name.lower() != "weixin.exe":
        errors.append("请选择微信主程序 Weixin.exe。")
    machine = read_pe_machine(path) if path.is_file() else None
    if path.is_file() and machine != 0x8664:
        actual = "无法识别" if machine is None else f"0x{machine:04X}"
        errors.append(f"微信程序必须是 x64 架构，当前 PE 架构：{actual}。")
    version = read_file_version(path)
    if path.is_file() and version is None:
        errors.append("无法读取微信文件版本。")
    elif version is not None and version not in supported_versions:
        errors.append(
            f"微信版本 {version} 不受支持，当前支持：{', '.join(supported_versions)}。"
        )
    return WechatExecutable(path, version, source, tuple(errors))


def _registry_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    import winreg

    results: list[Path] = []
    roots = (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE)
    views = (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY)
    uninstall = r"Software\Microsoft\Windows\CurrentVersion\Uninstall"

    for root in roots:
        for view in views:
            try:
                key = winreg.OpenKey(root, uninstall, 0, winreg.KEY_READ | view)
            except OSError:
                continue
            with key:
                index = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(key, index)
                    except OSError:
                        break
                    index += 1
                    try:
                        with winreg.OpenKey(key, sub_name) as sub:
                            display_name = str(winreg.QueryValueEx(sub, "DisplayName")[0])
                            if display_name not in {"微信", "Weixin", "WeChat"}:
                                continue
                            try:
                                install_location = str(
                                    winreg.QueryValueEx(sub, "InstallLocation")[0]
                                ).strip().strip('"')
                            except OSError:
                                install_location = ""
                            try:
                                display_icon = str(
                                    winreg.QueryValueEx(sub, "DisplayIcon")[0]
                                ).strip()
                            except OSError:
                                display_icon = ""
                            if install_location:
                                results.append(Path(install_location) / "Weixin.exe")
                            if display_icon:
                                icon_path = display_icon.split(",", 1)[0].strip().strip('"')
                                results.append(Path(icon_path))
                    except OSError:
                        continue
    return results


def discover_wechat_executables() -> tuple[WechatExecutable, ...]:
    candidates = _registry_candidates()
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if base:
            candidates.extend(
                (
                    Path(base) / "Tencent" / "Weixin" / "Weixin.exe",
                    Path(base) / "Tencent" / "WeChat" / "Weixin.exe",
                )
            )

    unique: dict[str, Path] = {}
    for candidate in candidates:
        normalized = Path(os.path.abspath(candidate))
        if normalized.is_file():
            unique.setdefault(str(normalized).casefold(), normalized)
    return tuple(
        validate_wechat_executable(candidate, source="auto")
        for candidate in unique.values()
    )
