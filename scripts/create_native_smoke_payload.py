"""Create a manager-only payload for installer integration tests, not customers."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import tomllib
import zipfile


ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manager-exe", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    manager = args.manager_exe.expanduser().resolve()
    output = args.output.expanduser().resolve()
    artifacts = (ROOT / "artifacts").resolve()
    if not manager.is_file():
        parser.error(f"manager executable not found: {manager}")
    if output == artifacts or artifacts not in output.parents or output.suffix != ".wrb":
        parser.error("output must be a .wrb file inside artifacts")
    if output.exists():
        parser.error(f"output already exists: {output}")
    digest = hashlib.sha256()
    with manager.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    manifest = {
        "schema_version": 1,
        "release_version": version,
        "test_only_native_installer_smoke": True,
        "files": [{"path": "manager/WeChatReBot.exe", "sha256": digest.hexdigest().upper()}],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.write(manager, "manager/WeChatReBot.exe")
        archive.writestr("release-manifest.json", json.dumps(manifest))
    print(json.dumps({"output": str(output), "size": output.stat().st_size, "manager_sha256": digest.hexdigest().upper()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
