from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path

from .json_io import write_json_atomic


@dataclass(frozen=True)
class BrandProfile:
    display_name: str
    short_name: str
    persona_name: str
    avatar_path: str = ""
    welcome_text: str = ""
    help_title: str = ""
    schema_version: int = 1

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
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self.normalized())


def save_brand_profile(path: Path, profile: BrandProfile) -> None:
    errors = profile.validate()
    if errors:
        raise ValueError("；".join(errors))
    write_json_atomic(path, profile.to_dict())
