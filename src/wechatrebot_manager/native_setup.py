from __future__ import annotations

from pathlib import Path
import shutil

from .brand import BrandProfile
from .install_paths import INSTALL_MARKER
from .installer import InstallRequest, perform_install
from .location import load_install_location, locator_path
from .uninstaller import stop_installation_processes, validate_uninstall_root


DEFAULT_NATIVE_BRAND = BrandProfile(
    display_name="小妹",
    short_name="小妹",
    persona_name="小妹",
)


def install_from_native_setup(
    payload_root: str | Path,
    install_root: str | Path,
) -> dict[str, object]:
    """Install a verified payload behind a native Windows setup shell."""

    root = Path(install_root).expanduser().resolve()
    stopped = 0
    if (root / INSTALL_MARKER).is_file():
        stopped = stop_installation_processes(root)
    verified = perform_install(
        InstallRequest(
            install_root=root,
            payload_root=Path(payload_root),
            brand=DEFAULT_NATIVE_BRAND,
        ),
        create_shortcuts=False,
        create_entrypoints=False,
    )
    if verified is None:
        raise RuntimeError("原生安装程序未提供有效载荷。")
    return {
        "ok": True,
        "install_root": str(root),
        "file_count": len(verified.files),
        "stopped_processes": stopped,
    }


def prepare_native_uninstall(install_root: str | Path) -> dict[str, object]:
    """Stop owned processes and remove the per-user locator before Inno removes files."""

    root = validate_uninstall_root(install_root)
    stopped = stop_installation_processes(root)
    residual_root = locator_path().parent.resolve()
    if residual_root.parent == residual_root or residual_root.name != "WeChatReBot":
        raise RuntimeError("本机残留目录无效，已停止卸载。")
    if residual_root != root and load_install_location() == root:
        shutil.rmtree(residual_root, ignore_errors=True)
    return {"ok": True, "stopped_processes": stopped}


__all__ = [
    "DEFAULT_NATIVE_BRAND",
    "install_from_native_setup",
    "prepare_native_uninstall",
]
