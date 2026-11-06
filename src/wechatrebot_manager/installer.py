from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable

from .brand import BrandProfile, install_brand_profile
from .customer_config import load_robot_settings
from .install_paths import (
    INSTALL_MARKER,
    prepare_install_layout,
    validate_install_path,
)
from .json_io import write_json_atomic
from .installed_entries import ensure_launcher_executable
from .location import save_install_location
from .payload import (
    PayloadError,
    VerifiedPayload,
    install_payload,
    payload_relative_paths,
)
from .provider import OpenAIProviderProfile
from .runtime_config import _load_object, write_runtime_configs
from .runtime_config import normalize_admin_id
from .shortcuts import create_windows_shortcuts
from .uninstaller import provision_uninstaller
from .wechat_path import WechatExecutable


InstallProgress = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class InstallRequest:
    install_root: Path
    payload_root: Path | None
    brand: BrandProfile
    wechat: WechatExecutable | None = None
    provider: OpenAIProviderProfile | None = None
    admin_id: str = ""

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        relative_paths: tuple[Path, ...] = ()
        if self.payload_root is not None and (
            self.payload_root.is_dir() or self.payload_root.is_file()
        ):
            try:
                relative_paths = payload_relative_paths(self.payload_root)
            except PayloadError as exc:
                errors.append(str(exc))
        install = validate_install_path(
            self.install_root,
            payload_relative_paths=relative_paths,
        )
        errors.extend(install.errors)
        if self.wechat is not None and not self.wechat.supported:
            errors.extend(self.wechat.errors)
        errors.extend(self.brand.validate())
        if self.provider is not None:
            errors.extend(self.provider.validate())
        try:
            normalize_admin_id(self.admin_id)
        except ValueError as exc:
            errors.append(str(exc))
        if self.payload_root is not None and not (
            self.payload_root.is_dir() or self.payload_root.is_file()
        ):
            errors.append("商业安装载荷不存在。")
        return tuple(errors)


def perform_install(
    request: InstallRequest,
    *,
    progress_callback: InstallProgress | None = None,
    save_locator: bool = True,
    create_shortcuts: bool = True,
    create_entrypoints: bool = True,
    preserve_existing_settings: bool = False,
) -> VerifiedPayload | None:
    errors = request.validate()
    if errors:
        raise ValueError("；".join(errors))

    root = request.install_root.expanduser().resolve()
    existing_settings = load_robot_settings(root) if preserve_existing_settings else None
    existing_manager = _load_object(root / "config/manager.json") if preserve_existing_settings else {}
    existing_engine = (
        _load_object(root / "app/astrbot/data/cmd_config.json") if preserve_existing_settings else {}
    )
    existing_brand = preserve_existing_settings and (root / "config/brand.json").is_file()
    wechat_path = request.wechat.path if request.wechat is not None else None
    wechat_version = request.wechat.version if request.wechat is not None else None
    if existing_manager:
        wechat_path = str(existing_manager.get("wechat_exe_path") or "") or None
        wechat_version = str(existing_manager.get("wechat_version") or "") or None
    prepare_install_layout(root)
    write_json_atomic(
        root / INSTALL_MARKER,
        {"schema_version": 1, "state": "installing"},
    )
    try:
        verified: VerifiedPayload | None = None
        if request.payload_root is not None:
            verified = install_payload(
                request.payload_root,
                root,
                progress_callback=progress_callback,
                preserve_existing_user_config=preserve_existing_settings,
            )
        if progress_callback is not None:
            progress_callback("configure", 0, 1)
        normalized_brand = (
            existing_settings.profile if existing_brand and existing_settings is not None
            else install_brand_profile(root, request.brand)
        )
        # Omitting identity updates preserves custom personas and the complete
        # administrator list; the editor's RobotSettings exposes only its first ID.
        write_runtime_configs(
            root,
            wechat_path,
            wechat_version=wechat_version,
            brand_profile=None if existing_engine else normalized_brand,
            provider_profile=request.provider,
            admin_id=None if existing_engine else request.admin_id,
        )
        write_json_atomic(
            root / INSTALL_MARKER,
            {
                "schema_version": 1,
                "state": "ready" if verified is not None else "configured",
            },
        )
        if create_entrypoints:
            ensure_launcher_executable(root)
            provision_uninstaller(
                root,
                display_name=normalized_brand.display_name,
            )
        if save_locator:
            try:
                save_install_location(root)
            except OSError:
                # The installation itself remains usable through its local executable.
                pass
        if create_shortcuts:
            try:
                create_windows_shortcuts(
                    root,
                    description=f"启动{normalized_brand.display_name}",
                )
            except (OSError, subprocess.SubprocessError):
                # A shortcut failure must not invalidate a verified installation.
                pass
        if progress_callback is not None:
            progress_callback("configure", 1, 1)
        return verified
    except Exception as exc:
        try:
            write_json_atomic(
                root / INSTALL_MARKER,
                {
                    "schema_version": 1,
                    "state": "failed",
                    "error_type": type(exc).__name__,
                },
            )
        except OSError:
            pass
        raise


__all__ = ["InstallRequest", "PayloadError", "perform_install"]
