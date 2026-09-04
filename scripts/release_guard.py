from __future__ import annotations

import argparse
import json
import re
from pathlib import Path, PurePosixPath, PureWindowsPath


FORBIDDEN_PATH_PARTS = {
    "legacy_nonebot2",
    "python311-x86",
    "__pycache__",
}
FORBIDDEN_FILENAMES = {
    ".env.prod",
    "config.json",
}
GLOBAL_FORBIDDEN_FILENAMES = {"noveloader.dll", "novehelper.dll"}
FORBIDDEN_SUFFIXES = {".db", ".db-shm", ".db-wal", ".log"}
WECHAT_DISTRIBUTABLE_NAMES = (
    re.compile(r"^weixin\.exe$", re.IGNORECASE),
    re.compile(r"^wechat\.exe$", re.IGNORECASE),
    re.compile(r"^(?:weixin|wechat)[-_]?(?:setup|installer|client)(?:[-_].*)?\.(?:exe|msi|msix|appx|zip|7z|rar)$", re.IGNORECASE),
)
FORBIDDEN_TEXT = (
    (
        "developer-path",
        re.compile(
            r"(?i)(?<![A-Za-z0-9])[A-Z]:\\(?:Users|Documents and Settings|Projects?|workspace|wechatrebot)(?:\\|$)"
        ),
    ),
    ("legacy-port", re.compile(r"(?<!\d)6199(?!\d)")),
    ("legacy-loader", re.compile(r"Nove(?:Loader|Helper)", re.IGNORECASE)),
    ("secret-key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\b\s*[=:]\s*"
            r"[\"'](?!<|\$\{|your-|example|xxx)[^\"'\r\n]{8,}[\"']"
        ),
    ),
)

TRUSTED_VENDOR_PREFIX = ("runtime", "python", "lib", "site-packages")


def _is_absolute_local_path(value: str) -> bool:
    return (
        PurePosixPath(value).is_absolute()
        or PureWindowsPath(value).is_absolute()
        or bool(PureWindowsPath(value).drive)
    )


def _manifest_absolute_paths(value: object) -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, dict):
        for child in value.values():
            found.extend(_manifest_absolute_paths(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_manifest_absolute_paths(child))
    elif isinstance(value, str) and _is_absolute_local_path(value):
        found.append(value)
    return tuple(found)


def _parts_casefold(path: Path) -> tuple[str, ...]:
    return tuple(part.casefold() for part in path.parts)


def _distribution_relative_parts(relative: Path) -> tuple[str, ...]:
    parts = _parts_casefold(relative)
    if parts[:1] == ("payload",):
        return parts[1:]
    return parts


def _is_trusted_vendor(relative: Path) -> bool:
    parts = _distribution_relative_parts(relative)
    return parts[: len(TRUSTED_VENDOR_PREFIX)] == TRUSTED_VENDOR_PREFIX


def _is_patched_astrbot(relative: Path) -> bool:
    parts = _distribution_relative_parts(relative)
    prefix = (*TRUSTED_VENDOR_PREFIX, "astrbot")
    return parts[: len(prefix)] == prefix


def _is_corresponding_source(relative: Path) -> bool:
    parts = _distribution_relative_parts(relative)
    return parts[:2] == ("legal", "corresponding-source")


def scan_release(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        parts = _parts_casefold(relative)
        trusted_vendor = _is_trusted_vendor(relative)
        lowered_parts = {part.casefold() for part in relative.parts[:-1]}
        if lowered_parts & FORBIDDEN_PATH_PARTS:
            findings.append({"path": relative.as_posix(), "reason": "forbidden-directory"})
        if parts and parts[0] == "py":
            findings.append({"path": relative.as_posix(), "reason": "forbidden-directory"})
        if not trusted_vendor and "logs" in lowered_parts:
            findings.append({"path": relative.as_posix(), "reason": "forbidden-directory"})
        if not trusted_vendor and any(part.endswith(".egg-info") for part in lowered_parts):
            findings.append({"path": relative.as_posix(), "reason": "build-metadata"})
        if path.name.casefold() in GLOBAL_FORBIDDEN_FILENAMES:
            findings.append({"path": relative.as_posix(), "reason": "forbidden-filename"})
        if not trusted_vendor and path.name.casefold() in FORBIDDEN_FILENAMES:
            findings.append({"path": relative.as_posix(), "reason": "forbidden-filename"})
        if any(pattern.fullmatch(path.name) for pattern in WECHAT_DISTRIBUTABLE_NAMES):
            findings.append({"path": relative.as_posix(), "reason": "wechat-distributable"})
        if not trusted_vendor and any(
            path.name.casefold().endswith(suffix) for suffix in FORBIDDEN_SUFFIXES
        ):
            findings.append({"path": relative.as_posix(), "reason": "runtime-data"})
        if trusted_vendor and not _is_patched_astrbot(relative):
            # Third-party wheels are accepted only through the pinned wheelhouse
            # fingerprint. Their sources can legitimately contain example tokens,
            # test fixtures, port-like numbers, or package data directories.
            continue
        if path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path.name.casefold() == "release-manifest.json":
            try:
                manifest = json.loads(text)
            except json.JSONDecodeError:
                manifest = None
            if manifest is not None and _manifest_absolute_paths(manifest):
                findings.append(
                    {
                        "path": relative.as_posix(),
                        "reason": "developer-absolute-path",
                    }
                )
        patterns = FORBIDDEN_TEXT
        if trusted_vendor:
            patterns = tuple(
                item for item in FORBIDDEN_TEXT if item[0] in {"legacy-port", "legacy-loader"}
            )
        elif _is_corresponding_source(relative):
            patterns = tuple(
                item for item in FORBIDDEN_TEXT if item[0] not in {"legacy-port", "legacy-loader"}
            )
        for reason, pattern in patterns:
            if pattern.search(text):
                findings.append({"path": relative.as_posix(), "reason": reason})
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not root.is_dir():
        parser.error(f"release root does not exist: {root}")
    findings = scan_release(root)
    print(json.dumps({"ok": not findings, "findings": findings}, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
