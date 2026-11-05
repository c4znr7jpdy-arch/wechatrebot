"""Transactional host support files for the activation deployment (stdlib only)."""
from __future__ import annotations

import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import urllib.error
import urllib.request


SERVICE = "wechatrebot-activation.service"
SYSTEMD_DIR = Path("/etc/systemd/system")
ADMIN_COMMAND = Path("/usr/local/bin/wechatrebot-activation-admin")
SUPPORT_FILES = (
    "deploy/activation-service/wechatrebot-activation.service",
    "deploy/activation-service/backup.py",
    "deploy/activation-service/admin.sh",
    "deploy/nginx/wechatrebot-admin.conf",
    "deploy/nginx/wechatrebot-license-limits.conf",
    "scripts/activation_deploy_support.py",
)


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, check=False)
    if result.returncode:
        raise RuntimeError(f"{argv[0]} failed ({result.returncode}): {result.stderr[-600:]}")
    return result.stdout


def nginx_files(dump: str) -> tuple[Path, Path]:
    """Locate existing dedicated snippets; never guess or rewrite a server block."""
    parts = re.split(r"(?m)^# configuration file ([^\r\n]+):\s*\n", dump)
    license_paths, limit_paths, wildcard_includes = [], [], []
    for index in range(1, len(parts), 2):
        path, text = Path(parts[index]), parts[index + 1]
        if (path.name == "wechatrebot-license.conf"
                and re.search(r"location\s+\^~\s+/wechatrebot-license/\s*\{", text)
                and not re.search(r"(?m)^\s*(server|http)\s*\{", text)):
            license_paths.append(path)
        if path.name == "wechatrebot-license-limits.conf" and "zone=wechatrebot_license:" in text:
            limit_paths.append(path)
        wildcard_includes.extend(pattern.strip(" \"'") for pattern in
                                 re.findall(r"(?m)^\s*include\s+([^;]*\*[^;]*);", text))
    if len(set(license_paths)) != 1 or len(set(limit_paths)) != 1:
        raise RuntimeError("Cannot uniquely locate the enabled license nginx snippets; prepare their includes before upgrading")
    admin_path = license_paths[0].with_name("wechatrebot-admin.conf").as_posix()
    if any(fnmatch.fnmatchcase(admin_path, pattern) for pattern in wildcard_includes):
        raise RuntimeError("The admin location snippet would also match a wildcard include; use an explicitly included snippet directory")
    return license_paths[0], limit_paths[0]


def _snapshot_path(root: Path, snapshot: Path) -> Path:
    root, snapshot = root.resolve(), snapshot.absolute()
    if snapshot.is_symlink() or snapshot.parent.resolve() != root / "support-backups":
        raise ValueError("Support snapshot must be directly inside the deployment support-backups directory")
    if not re.fullmatch(r"[0-9a-f]{8,64}", snapshot.name):
        raise ValueError("Invalid support snapshot identity")
    return snapshot


def _read_regular(path: Path) -> bytes | None:
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError(f"Expected a regular support file: {path}")
    return path.read_bytes() if path.exists() else None


def _write_atomic(path: Path, data: bytes, mode: int, uid: int | None = None, gid: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        if uid is not None and hasattr(os, "chown"):
            os.chown(temporary, uid, gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _candidates(root: Path, release: Path, nginx_dump: str, env_file: str, container: str) -> list[tuple[Path, bytes, int]]:
    license_path, limits_path = nginx_files(nginx_dump)
    admin_path = license_path.with_name("wechatrebot-admin.conf")
    license_data = _read_regular(license_path).decode("utf-8")
    include = f'include "{admin_path.as_posix()}";'
    matching = re.findall(r"(?m)^\s*include\s+([^;]*wechatrebot-admin\.conf[\"']?)\s*;", license_data)
    if matching and (len(matching) != 1 or matching[0].strip("\"'") != admin_path.as_posix()):
        raise ValueError("An unexpected admin nginx include already exists")
    if not matching:
        license_data = license_data.rstrip() + "\n\n" + include + "\n"
    limits = _read_regular(limits_path).decode("utf-8")
    source_limits = (release / SUPPORT_FILES[4]).read_text("utf-8")
    for zone in ("wechatrebot_admin", "wechatrebot_admin_login"):
        rule = re.compile(rf"(?m)^\s*limit_req_zone\b[^;\n]*\bzone={zone}:[^;\n]*;[^\n]*$")
        desired = rule.findall(source_limits)
        if len(desired) != 1 or len(rule.findall(limits)) > 1:
            raise ValueError(f"Invalid nginx limit zone: {zone}")
        limits = rule.sub(lambda _match: desired[0].strip(), limits) if rule.search(limits) else limits.rstrip() + "\n" + desired[0].strip() + "\n"
    unit = (release / SUPPORT_FILES[0]).read_text("utf-8").replace(
        "EnvironmentFile=/etc/wechatrebot-activation.env", f"EnvironmentFile={env_file}")
    unit = unit.replace("--name wechatrebot-activation ", f"--name {container} ").replace(
        "--time 30 wechatrebot-activation", f"--time 30 {container}")
    admin = (release / SUPPORT_FILES[2]).read_text("utf-8").replace(" wechatrebot-activation python ", f" {container} python ")
    return [
        (SYSTEMD_DIR / SERVICE, unit.encode(), 0o644),
        (root / "deploy/activation-service/backup.py", (release / SUPPORT_FILES[1]).read_bytes(), 0o644),
        (ADMIN_COMMAND, admin.encode(), 0o755),
        (admin_path, (release / SUPPORT_FILES[3]).read_bytes(), 0o644),
        (license_path, license_data.encode(), 0o644),
        (limits_path, limits.encode(), 0o644),
    ]


def prepare(root: Path, release: Path, snapshot: Path, command=run, *,
            env_file: str = "/etc/wechatrebot-activation.env", container: str = "wechatrebot-activation") -> None:
    root, release = root.resolve(), release.resolve()
    if release.parent != root / "releases":
        raise ValueError("Release directory is outside the deployment releases directory")
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", env_file) or ".." in env_file.split("/"):
        raise ValueError("Invalid environment file path")
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", container):
        raise ValueError("Invalid container name")
    candidates = _candidates(root, release, command(["nginx", "-T"]), env_file, container)
    _save_snapshot(root, snapshot, candidates)


def _save_snapshot(root: Path, snapshot: Path, candidates: list[tuple[Path, bytes, int]]) -> None:
    snapshot = _snapshot_path(root, snapshot)
    if snapshot.exists():
        raise ValueError("Support snapshot already exists")
    # Read every old file before writing any target, including initially absent files.
    previous = [(path, _read_regular(path), path.stat() if path.exists() else None, data, mode)
                for path, data, mode in candidates]
    snapshot.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    snapshot.mkdir(mode=0o700)
    entries = []
    for index, (path, old, stat, data, mode) in enumerate(previous):
        if old is not None:
            (snapshot / f"{index}.old").write_bytes(old)
        (snapshot / f"{index}.new").write_bytes(data)
        entries.append({"path": str(path), "existed": old is not None, "mode": mode,
                        "old_mode": stat.st_mode & 0o7777 if stat else None,
                        "uid": stat.st_uid if stat else None, "gid": stat.st_gid if stat else None})
    shutil.copyfile(__file__, snapshot / "tool.py")
    (snapshot / "manifest.json").write_text(json.dumps({"root": str(root), "entries": entries}), encoding="utf-8")


def _load(root: Path, snapshot: Path) -> list[dict]:
    snapshot = _snapshot_path(root, snapshot)
    manifest = json.loads((snapshot / "manifest.json").read_text("utf-8"))
    if manifest.get("root") != str(root.resolve()) or not isinstance(manifest.get("entries"), list):
        raise ValueError("Invalid support snapshot manifest")
    return manifest["entries"]


def capture(root: Path, snapshot: Path, previous: Path) -> None:
    """Save today's files before an explicit rollback so that it can be reversed."""
    candidates = []
    for entry in _load(root, previous):
        path = Path(entry["path"])
        content = _read_regular(path)
        candidates.append((path, content or b"", entry["mode"]))
    _save_snapshot(root.resolve(), snapshot, candidates)


def apply(root: Path, snapshot: Path, *, restore: bool = False, command=run) -> None:
    entries = _load(root, snapshot)
    for index, entry in enumerate(entries):
        path = Path(entry["path"])
        _read_regular(path)
        if restore and not entry["existed"]:
            path.unlink(missing_ok=True)
            continue
        data = (snapshot / f"{index}.{'old' if restore else 'new'}").read_bytes()
        _write_atomic(path, data, entry["old_mode"] if restore else entry["mode"], entry["uid"], entry["gid"])
    command(["nginx", "-t"])
    command(["systemctl", "daemon-reload"])


def probe_admin(url: str = "http://127.0.0.1:8092/login") -> None:
    if url != "http://127.0.0.1:8092/login" and not url.startswith("https://"):
        raise ValueError("Admin probe requires the loopback listener or public HTTPS")
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        status, headers, text = response.status, response.headers, response.read(1024 * 1024).decode("utf-8")
    initialized = status == 200 or (status == 429 and "登录已锁定" in text)
    valid_page = (status == 503 and "未初始化" in text) or (initialized and 'name="password"' in text)
    if (not valid_page or "no-store" not in headers.get("Cache-Control", "")
            or headers.get("X-Frame-Options") != "DENY" or not headers.get("Content-Security-Policy")):
        raise RuntimeError(f"Admin loopback login probe failed: HTTP {status}")
    print(json.dumps({"ok": True, "status": status, "initialized": initialized}))


def probe_restored(root: Path, snapshot: Path, url: str) -> None:
    for index, entry in enumerate(_load(root, snapshot)):
        if Path(entry["path"]).name == SERVICE and entry["existed"]:
            previous_unit = (snapshot / f"{index}.old").read_text("utf-8")
            if "127.0.0.1:8092:8092" in previous_unit:
                probe_admin(url)
                return
    print(json.dumps({"ok": True, "skipped": "restored version has no admin listener"}))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("operation", choices=("prepare", "apply", "restore", "capture", "probe", "probe-restored"))
    parser.add_argument("--root", type=Path)
    parser.add_argument("--release", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--url", default="http://127.0.0.1:8092/login")
    parser.add_argument("--env-file", default="/etc/wechatrebot-activation.env")
    parser.add_argument("--container", default="wechatrebot-activation")
    args = parser.parse_args()
    if args.operation == "probe":
        probe_admin(args.url)
        return
    if args.root is None or args.snapshot is None:
        parser.error("--root and --snapshot are required")
    if args.operation == "probe-restored":
        probe_restored(args.root, args.snapshot, args.url)
    elif args.operation == "prepare":
        if args.release is None:
            parser.error("--release is required")
        prepare(args.root, args.release, args.snapshot, env_file=args.env_file, container=args.container)
    elif args.operation == "capture":
        if args.previous is None:
            parser.error("--previous is required")
        capture(args.root, args.snapshot, args.previous)
    else:
        apply(args.root, args.snapshot, restore=args.operation == "restore")


if __name__ == "__main__":
    main()
