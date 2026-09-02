from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Callable

from .brand import BrandProfile, install_brand_profile
from .install_paths import (
    INSTALL_MARKER,
    prepare_install_layout,
    validate_install_path,
)
from .json_io import write_json_atomic
from .location import save_install_location
from .payload import PayloadError, VerifiedPayload, install_payload
from .provider import OpenAIProviderProfile
from .runtime_config import write_runtime_configs
from .runtime_config import normalize_admin_id
from .shortcuts import create_windows_shortcuts
from .wechat_path import WechatExecutable


InstallProgress = Callable[[str, int, int], None]


@dataclass(frozen=True, slots=True)
class InstallRequest:
    install_root: Path
    payload_root: Path | None
    wechat: WechatExecutable
    brand: BrandProfile
    provider: OpenAIProviderProfile | None = None
    admin_id: str = ""
    allow_incomplete_payload: bool = False

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        install = validate_install_path(self.install_root)
        errors.extend(install.errors)
        if not self.wechat.supported:
            errors.extend(self.wechat.errors)
        errors.extend(self.brand.validate())
        if self.provider is not None:
            errors.extend(self.provider.validate())
        try:
            normalize_admin_id(self.admin_id)
        except ValueError as exc:
            errors.append(str(exc))
        if self.payload_root is not None and not self.payload_root.is_dir():
            errors.append("商业载荷目录不存在。")
        return tuple(errors)


def perform_install(
    request: InstallRequest,
    *,
    progress_callback: InstallProgress | None = None,
    save_locator: bool = True,
    create_shortcuts: bool = True,
) -> VerifiedPayload | None:
    errors = request.validate()
    if errors:
        raise ValueError("；".join(errors))

    root = request.install_root.expanduser().resolve()
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
                allow_incomplete=request.allow_incomplete_payload,
                progress_callback=progress_callback,
            )
        if progress_callback is not None:
            progress_callback("configure", 0, 1)
        normalized_brand = install_brand_profile(root, request.brand)
        write_runtime_configs(
            root,
            request.wechat.path,
            wechat_version=request.wechat.version,
            brand_profile=normalized_brand,
            provider_profile=request.provider,
            admin_id=request.admin_id,
        )
        write_json_atomic(
            root / INSTALL_MARKER,
            {
                "schema_version": 1,
                "state": "ready" if verified is not None else "configured",
            },
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
