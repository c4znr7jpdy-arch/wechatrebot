from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(
                "__pycache__",
                "*.pyc",
                ".pytest_cache",
                "*.egg-info",
            ),
        )
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--allow-gate", action="append", default=[])
    args = parser.parse_args(argv)

    commercial_root = Path(__file__).resolve().parents[1]
    source_root = args.source_root.resolve()
    output = (args.output or commercial_root / "artifacts" / "staging").resolve()
    artifacts_root = (commercial_root / "artifacts").resolve()
    if output != artifacts_root and artifacts_root not in output.parents:
        parser.error("output must stay inside the commercial artifacts directory")
    if output.exists() and any(output.iterdir()):
        parser.error(f"output must not already contain files: {output}")

    manifest = json.loads(
        (commercial_root / "release" / "source-manifest.json").read_text(encoding="utf-8")
    )
    copied: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for entry in manifest["entries"]:
        gate = entry.get("gate")
        if gate and gate not in args.allow_gate:
            skipped.append({"source": entry["source"], "gate": gate})
            continue
        origin = commercial_root if entry["origin"] == "commercial" else source_root
        source = (origin / entry["source"]).resolve()
        if not source.exists():
            raise FileNotFoundError(source)
        target = output / entry["target"]
        _copy(source, target)
        copied.append({"source": str(source), "target": target.relative_to(output).as_posix()})

    hashes = [
        {"path": path.relative_to(output).as_posix(), "sha256": _sha256(path)}
        for path in sorted(output.rglob("*"))
        if path.is_file()
    ]
    (output / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_manifest_status": manifest["status"],
                "copied": copied,
                "skipped_gated_entries": skipped,
                "files": hashes,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    guard = subprocess.run(
        [sys.executable, str(commercial_root / "scripts" / "release_guard.py"), str(output)],
        check=False,
    )
    return guard.returncode


if __name__ == "__main__":
    raise SystemExit(main())
