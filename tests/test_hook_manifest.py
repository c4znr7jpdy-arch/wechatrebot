import json
from pathlib import Path


def test_hook_binary_manifest_matches_gated_source_entries() -> None:
    root = Path(__file__).parents[1]
    hook = json.loads(
        (root / "release" / "hook-binaries.json").read_text(encoding="utf-8")
    )
    sources = json.loads(
        (root / "release" / "source-manifest.json").read_text(encoding="utf-8")
    )
    hook_by_path = {item["path"]: item for item in hook["files"]}
    gated = [
        item
        for item in sources["entries"]
        if item.get("gate") == "hook-binary-redistribution"
    ]
    assert hook["redistribution_approved"] is False
    assert len(gated) == 2
    for item in gated:
        assert len(item["sha256"]) == 64
        assert hook_by_path[item["source"]]["sha256"] == item["sha256"]
        assert hook_by_path[item["source"]]["architecture"] == "x64"
