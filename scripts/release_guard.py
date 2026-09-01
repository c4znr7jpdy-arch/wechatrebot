from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


FORBIDDEN_PATH_PARTS = {
    "py",
    "legacy_nonebot2",
    "python311-x86",
    "logs",
    "__pycache__",
}
FORBIDDEN_FILENAMES = {
    "noveloader.dll",
    "novehelper.dll",
    ".env.prod",
    "config.json",
}
FORBIDDEN_SUFFIXES = {".db", ".db-shm", ".db-wal", ".log"}
WECHAT_DISTRIBUTABLE_NAMES = (
    re.compile(r"^weixin\.exe$", re.IGNORECASE),
    re.compile(r"^wechat\.exe$", re.IGNORECASE),
    re.compile(r"^(?:weixin|wechat)[-_]?(?:setup|installer|client)(?:[-_].*)?\.(?:exe|msi|msix|appx|zip|7z|rar)$", re.IGNORECASE),
)
FORBIDDEN_TEXT = (
    ("legacy-port", re.compile(r"(?<!\d)6199(?!\d)")),
    ("legacy-loader", re.compile(r"Nove(?:Loader|Helper)", re.IGNORECASE)),
    ("secret-key", re.compile(r"\bsk-[A-Za-z0-9_-]{8,}\b")),
    (
        "assigned-secret",
        re.compile(
            r"(?i)\b(?:api[_-]?key|access[_-]?token|secret)\b\s*[=:]\s*[\"']?(?!<|\$\{|your-|example|xxx)[^\s\"']{8,}"
        ),
    ),
)


def scan_release(root: Path) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(root)
        lowered_parts = {part.casefold() for part in relative.parts[:-1]}
        if lowered_parts & FORBIDDEN_PATH_PARTS:
            findings.append({"path": relative.as_posix(), "reason": "forbidden-directory"})
        if any(part.endswith(".egg-info") for part in lowered_parts):
            findings.append({"path": relative.as_posix(), "reason": "build-metadata"})
        if path.name.casefold() in FORBIDDEN_FILENAMES:
            findings.append({"path": relative.as_posix(), "reason": "forbidden-filename"})
        if any(pattern.fullmatch(path.name) for pattern in WECHAT_DISTRIBUTABLE_NAMES):
            findings.append({"path": relative.as_posix(), "reason": "wechat-distributable"})
        if any(path.name.casefold().endswith(suffix) for suffix in FORBIDDEN_SUFFIXES):
            findings.append({"path": relative.as_posix(), "reason": "runtime-data"})
        if path.stat().st_size > 4 * 1024 * 1024:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for reason, pattern in FORBIDDEN_TEXT:
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
