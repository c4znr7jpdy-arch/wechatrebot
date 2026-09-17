from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "installer" / "WeChatReBot.iss"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _version() -> str:
    payload = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return str(payload["project"]["version"])


def _iscc(candidate: Path | None) -> Path:
    if candidate is not None:
        resolved = candidate.expanduser().resolve()
        if resolved.is_file():
            return resolved
        raise FileNotFoundError(f"Inno Setup 编译器不存在：{resolved}")
    command = shutil.which("ISCC.exe") or shutil.which("ISCC")
    candidates = (
        Path(command) if command else None,
        Path(r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 7\ISCC.exe"),
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    )
    for path in candidates:
        if path is not None and path.is_file():
            return path.resolve()
    raise FileNotFoundError(
        "未找到 Inno Setup 7 编译器 ISCC.exe；请安装 Inno Setup 7 后重试。"
    )


def _https_url(value: str) -> str:
    parsed = urlsplit(value.strip())
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError("在线载荷地址必须是有效的 HTTPS URL。")
    return value.strip()


def _definition(name: str, value: str | Path) -> str:
    text = str(value)
    if any(char in text for char in ('\n', '\r', '"')):
        raise ValueError(f"{name} 包含不允许的字符。")
    return f"/D{name}={text}"


def command_for(args: argparse.Namespace, *, require_compiler: bool) -> tuple[list[str], dict[str, object]]:
    manager = args.manager_exe.expanduser().resolve()
    if not manager.is_file() or manager.suffix.casefold() != ".exe":
        raise FileNotFoundError(f"管理器 EXE 不存在：{manager}")
    with manager.open("rb") as handle:
        signature = handle.read(2)
    if signature != b"MZ":
        raise ValueError(f"管理器不是有效的 Windows EXE：{manager}")

    output = args.output.expanduser().resolve()
    artifacts = (ROOT / "artifacts").resolve()
    if output == artifacts or artifacts not in output.parents:
        raise ValueError("安装器输出目录必须位于当前工作树的 artifacts 目录内。")

    values: dict[str, object] = {
        "mode": "online" if args.payload_url else "offline",
        "version": _version(),
        "manager_exe": str(manager),
        "output": str(output),
    }
    definitions = [
        _definition("AppVersion", str(values["version"])),
        _definition("ManagerExe", manager),
    ]
    if args.payload_url:
        digest = (args.payload_sha256 or "").strip().upper()
        if len(digest) != 64 or any(char not in "0123456789ABCDEF" for char in digest):
            raise ValueError("在线载荷必须提供64位十六进制 SHA-256。")
        url = _https_url(args.payload_url)
        definitions.extend(
            (_definition("PayloadUrl", url), _definition("PayloadSHA256", digest))
        )
        values.update(payload_url=url, payload_sha256=digest)
    else:
        if args.payload is None:
            raise ValueError("离线安装器必须提供 --payload。")
        payload = args.payload.expanduser().resolve()
        if not payload.is_file():
            raise FileNotFoundError(f"离线载荷不存在：{payload}")
        definitions.append(_definition("PayloadArchive", payload))
        values.update(
            payload=str(payload),
            payload_size=payload.stat().st_size,
            payload_sha256=_sha256(payload),
        )

    compiler = _iscc(args.iscc) if require_compiler else Path("ISCC.exe")
    command = [
        str(compiler),
        *definitions,
        f"/O{output}",
        str(SCRIPT),
    ]
    return command, values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build the standard Windows WeChat ReBot setup executable."
    )
    parser.add_argument("--manager-exe", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--payload", type=Path)
    mode.add_argument("--payload-url")
    parser.add_argument("--payload-sha256")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--iscc", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    command, result = command_for(args, require_compiler=not args.dry_run)
    if args.dry_run:
        result["command"] = command
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    output = args.output.expanduser().resolve()
    output.mkdir(parents=True, exist_ok=True)
    subprocess.run(command, cwd=ROOT, check=True)
    setup = output / f"WeChatReBot-Setup-{result['version']}.exe"
    if not setup.is_file():
        raise RuntimeError(f"Inno Setup 未生成预期文件：{setup}")
    result.update(
        setup=str(setup),
        setup_size=setup.stat().st_size,
        setup_sha256=_sha256(setup),
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
