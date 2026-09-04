import json
from pathlib import Path


def test_hook_binary_manifest_matches_hash_pinned_source_entries() -> None:
    root = Path(__file__).parents[1]
    hook = json.loads(
        (root / "release" / "hook-binaries.json").read_text(encoding="utf-8")
    )
    sources = json.loads(
        (root / "release" / "source-manifest.json").read_text(encoding="utf-8")
    )
    hook_by_path = {item["path"]: item for item in hook["files"]}
    pinned = [
        item
        for item in sources["entries"]
        if item.get("source") in hook_by_path
    ]
    assert hook["redistribution_approved"] is False
    assert len(pinned) == 2
    for item in pinned:
        assert len(item["sha256"]) == 64
        assert hook_by_path[item["source"]]["sha256"] == item["sha256"]
        assert hook_by_path[item["source"]]["architecture"] == "x64"
