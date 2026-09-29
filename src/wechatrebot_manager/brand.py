from __future__ import annotations

from dataclasses import asdict, dataclass
import os
from pathlib import Path
import shutil
import tempfile

from .json_io import write_json_atomic


SUPPORTED_AVATAR_SUFFIXES = {".jpg", ".jpeg", ".png", ".webp"}
MAX_AVATAR_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True)
class BrandProfile:
    display_name: str
    short_name: str
    persona_name: str
    avatar_path: str = ""
    welcome_text: str = ""
    help_title: str = ""
    schema_version: int = 1
    name_source: str = "custom"

    def validate(self) -> tuple[str, ...]:
        errors: list[str] = []
        required = {
            "机器人名称": self.display_name,
            "机器人简称": self.short_name,
            "人格称呼": self.persona_name,
        }
        for label, value in required.items():
            stripped = value.strip()
            if not stripped:
                errors.append(f"{label}不能为空。")
            if len(stripped) > 32:
                errors.append(f"{label}不能超过 32 个字符。")
            if any(ord(char) < 32 for char in stripped):
                errors.append(f"{label}不能包含控制字符。")
        if self.avatar_path and not Path(self.avatar_path).is_file():
            errors.append("头像文件不存在。")
        elif self.avatar_path:
            avatar = Path(self.avatar_path)
            if avatar.suffix.casefold() not in SUPPORTED_AVATAR_SUFFIXES:
                errors.append("头像只支持 JPG、PNG 或 WebP。")
            try:
                if avatar.stat().st_size > MAX_AVATAR_BYTES:
                    errors.append("头像文件不能超过 10 MiB。")
            except OSError:
                pass
        return tuple(errors)

    def normalized(self) -> "BrandProfile":
        display_name = self.display_name.strip()
        short_name = self.short_name.strip()
        persona_name = self.persona_name.strip()
        return BrandProfile(
            display_name=display_name,
            short_name=short_name,
            persona_name=persona_name,
            avatar_path=self.avatar_path.strip(),
            welcome_text=self.welcome_text.strip() or f"你好，我是{display_name}。",
            help_title=self.help_title.strip() or f"{display_name}帮助",
            schema_version=self.schema_version,
            name_source=self.name_source,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self.normalized())


def save_brand_profile(path: Path, profile: BrandProfile) -> None:
    errors = profile.validate()
    if errors:
        raise ValueError("；".join(errors))
    write_json_atomic(path, profile.to_dict())


def install_brand_profile(
    install_root: str | Path,
    profile: BrandProfile,
) -> BrandProfile:
    root = Path(install_root).expanduser().resolve()
    normalized = profile.normalized()
    errors = normalized.validate()
    if errors:
        raise ValueError("；".join(errors))
    if normalized.avatar_path:
        source = Path(normalized.avatar_path).expanduser().resolve()
        assets = root / "config" / "assets"
        assets.mkdir(parents=True, exist_ok=True)
        target = assets / f"avatar{source.suffix.casefold()}"
        if source != target.resolve():
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=".avatar.", suffix=source.suffix.casefold(), dir=assets
            )
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                shutil.copy2(source, temporary)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
        normalized = BrandProfile(
            display_name=normalized.display_name,
            short_name=normalized.short_name,
            persona_name=normalized.persona_name,
            avatar_path=str(target.resolve()),
            welcome_text=normalized.welcome_text,
            help_title=normalized.help_title,
            schema_version=normalized.schema_version,
            name_source=normalized.name_source,
        )
    save_brand_profile(root / "config" / "brand.json", normalized)
    return normalized
