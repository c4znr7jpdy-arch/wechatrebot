import importlib.util
import json
from pathlib import Path


def _syncer():
    script = Path(__file__).parents[1] / "scripts" / "sync_signed_hook_metadata.py"
    spec = importlib.util.spec_from_file_location("sync_signed_hook_metadata", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_signed_hashes_update_build_and_hook_metadata(tmp_path: Path) -> None:
    release = tmp_path / "release"
    release.mkdir()
    manager_manifest = tmp_path / "manager-build-manifest.json"
    manager_manifest.write_text(
        json.dumps({"manager_version": "0.8.3", "signed": False}),
        encoding="utf-8",
    )
    hook_paths = {
        "injector": "bridge/vendor/hook/x64 inject.exe",
        "hook-library-c2c-video-patched": "bridge/vendor/hook/hook.dll",
    }
    (release / "hook-binaries.json").write_text(
        json.dumps(
            {
                "files": [
                    {"role": role, "path": path, "sha256": "0" * 64}
                    for role, path in hook_paths.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    (release / "source-manifest.json").write_text(
        json.dumps(
            {
                "entries": [
                    {
                        "source": path,
                        "sha256": "0" * 64,
                    }
                    for path in hook_paths.values()
                ]
            }
        ),
        encoding="utf-8",
    )
    hashes = {"manager": "A" * 64, "injector": "B" * 64, "hook-library": "C" * 64}
    evidence = {
        "release_version": "0.8.3",
        "status": "valid",
        "all_signatures_valid": True,
        "files": [
            {
                "role": role,
                "size": index,
                "sha256": digest,
                "authenticode_status": "Valid",
                "signer_thumbprint": "D" * 40,
                "timestamp_thumbprint": "E" * 40,
            }
            for index, (role, digest) in enumerate(hashes.items(), start=1)
        ],
    }
    (release / "signing-evidence.json").write_text(
        json.dumps(evidence), encoding="utf-8"
    )

    result = _syncer().main(
        ["--root", str(tmp_path), "--manager-build-manifest", str(manager_manifest)]
    )

    assert result == 0
    updated_manager = json.loads(manager_manifest.read_text(encoding="utf-8"))
    updated_hook = json.loads(
        (release / "hook-binaries.json").read_text(encoding="utf-8")
    )
    assert updated_manager["signed"] is True
    assert updated_manager["sha256"] == hashes["manager"]
    assert {item["sha256"] for item in updated_hook["files"]} == {
        hashes["injector"],
        hashes["hook-library"],
    }
