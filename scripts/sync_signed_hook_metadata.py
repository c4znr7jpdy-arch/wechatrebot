from __future__ import annotations

import argparse
import json
from pathlib import Path


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize signed hook hashes across release metadata."
    )
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    parser.add_argument(
        "--evidence", type=Path, default=Path("release/signing-evidence.json")
    )
    parser.add_argument("--manager-build-manifest", type=Path, required=True)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    evidence_path = args.evidence
    if not evidence_path.is_absolute():
        evidence_path = root / evidence_path
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    if evidence.get("status") != "valid" or evidence.get("all_signatures_valid") is not True:
        raise RuntimeError("signing evidence is not valid")
    signed = {
        item["role"]: item
        for item in evidence.get("files", [])
        if isinstance(item, dict) and isinstance(item.get("role"), str)
    }
    if set(signed) != {"manager", "injector", "hook-library"}:
        raise RuntimeError("signing evidence must contain exactly three release roles")

    manager_manifest_path = args.manager_build_manifest.resolve()
    manager_manifest = json.loads(manager_manifest_path.read_text(encoding="utf-8"))
    manager_record = signed["manager"]
    if manager_manifest.get("manager_version") != evidence.get("release_version"):
        raise RuntimeError("manager build manifest version does not match signing evidence")
    if manager_record.get("authenticode_status") != "Valid":
        raise RuntimeError("manager signature is not valid")
    manager_manifest["size"] = manager_record["size"]
    manager_manifest["sha256"] = str(manager_record["sha256"]).upper()
    manager_manifest["signed"] = True
    manager_manifest["signer_thumbprint"] = manager_record["signer_thumbprint"]
    manager_manifest["timestamp_thumbprint"] = manager_record["timestamp_thumbprint"]

    hook_path = root / "release" / "hook-binaries.json"
    source_path = root / "release" / "source-manifest.json"
    hook = json.loads(hook_path.read_text(encoding="utf-8"))
    source = json.loads(source_path.read_text(encoding="utf-8"))
    role_map = {
        "injector": "injector",
        "hook-library-c2c-video-patched": "hook-library",
    }
    hashes: dict[str, str] = {}
    for item in hook["files"]:
        evidence_role = role_map.get(item.get("role"))
        if evidence_role is None:
            raise RuntimeError(f"unexpected hook role: {item.get('role')}")
        record = signed[evidence_role]
        if record.get("authenticode_status") != "Valid":
            raise RuntimeError(f"hook signature is not valid: {evidence_role}")
        item["size"] = record["size"]
        item["sha256"] = str(record["sha256"]).upper()
        item["authenticode"] = "valid"
        item["signer_thumbprint"] = record["signer_thumbprint"]
        item["timestamp_thumbprint"] = record["timestamp_thumbprint"]
        hashes[item["path"]] = item["sha256"]
    matched: set[str] = set()
    for entry in source["entries"]:
        if entry.get("source") not in hashes:
            continue
        path = entry["source"]
        if path not in hashes:
            raise RuntimeError(f"signed hook is missing from hook metadata: {path}")
        entry["sha256"] = hashes[path]
        matched.add(path)
    if matched != set(hashes):
        raise RuntimeError("not every signed hook is pinned by the source manifest")
    _write(hook_path, hook)
    _write(source_path, source)
    _write(manager_manifest_path, manager_manifest)
    print(
        json.dumps(
            {
                "ok": True,
                "updated_manager_hash": manager_manifest["sha256"],
                "updated_hook_hashes": hashes,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
