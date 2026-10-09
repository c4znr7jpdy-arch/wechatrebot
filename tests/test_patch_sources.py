import hashlib
import json
from pathlib import Path


def test_patch_source_snapshot_matches_its_manifest() -> None:
    root = Path(__file__).parents[1] / "vendor"
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["files"], "Patch tests require a pinned source snapshot"
    for entry in manifest["files"]:
        content = (root / entry["path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == entry["sha256"], entry["path"]
