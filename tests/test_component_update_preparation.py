from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager.payload import PayloadError, installed_payload_fingerprint, sha256_file


PLUGINS = "app/astrbot/data/plugins"


@pytest.fixture
def preparation():
    path = Path(__file__).parents[1] / "scripts/prepare_component_update.py"
    spec = importlib.util.spec_from_file_location("component_preparation_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(root, contents):
    for name, value in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


def _inventory(root, contents, *, staging=False, version="1.0.1"):
    _files(root, contents)
    manifest = {
        "schema_version": 1, "release_version": version,
        "files": [{"path": name, "sha256": sha256_file(root / name)} for name in contents],
    }
    name = "release-manifest.json" if staging else "config/installed-payload.json"
    _files(root, {name: json.dumps(manifest)})
    return manifest


def _fixture(tmp_path, preparation, monkeypatch):
    from build_runtime import PYTHON_SHA256, PYTHON_VERSION

    root, baseline = tmp_path / "repo", tmp_path / "published-baseline"
    plugin_manifest = json.dumps({"plugins": [{"name": "existing"}]})
    project = '[project]\nname = "fixture"\nversion = "1.0.2"\n'
    _files(root, {
        "pyproject.toml": project,
        "scripts/build_manager.py": "# fixture manager build\n",
        "scripts/manager_entry.py": "# fixture entry\n",
        "src/wechatrebot_manager/example.py": "# fixture manager source\n",
        "release/windows-version-info.txt": "fixture version info\n",
        "release/build-requirements.lock": "fixture-build==1\n",
        "release/runtime-requirements.lock": "httpx==0.28.1\n",
        "release/plugin-manifest.json": plugin_manifest,
        "legal/corresponding-source/fixture.txt": "reviewed source fixture\n",
        "vendor/existing/main.py": "answer = 2\n",
        "vendor/existing/requirements.txt": "httpx>=0.28,<1\n",
    })
    vendor_files = [p for p in (root / "vendor").rglob("*") if p.is_file()]
    _files(root, {"vendor/manifest.json": json.dumps({"schema_version": 1, "files": [
        {"path": p.relative_to(root / "vendor").as_posix(), "sha256": sha256_file(p).lower()}
        for p in vendor_files
    ]})})
    entries = [
        {"origin": "source", "source": "existing", "target": f"{PLUGINS}/existing"},
        {"origin": "commercial", "source": "release/plugin-manifest.json", "target": "legal/plugin-manifest.json"},
        {"origin": "commercial", "source": "legal/corresponding-source", "target": "legal/corresponding-source"},
    ]
    _files(root, {"release/source-manifest.json": json.dumps({"entries": entries})})
    monkeypatch.setattr(preparation.local, "BASE_INPUTS", {
        "release/runtime-requirements.lock": "legal/runtime-requirements.lock",
    })
    contents = {
        f"{PLUGINS}/existing/main.py": "answer = 1\n",
        f"{PLUGINS}/existing/obsolete.png": "obsolete image",
        "legal/plugin-manifest.json": plugin_manifest,
        "legal/runtime-requirements.lock": "httpx==0.28.1\n",
        "legal/build-requirements.lock": "fixture-build==1\n",
        "legal/manager-build-inputs.json": json.dumps(preparation.local.manager_build_inputs(root)),
        "manager/src/wechatrebot_manager/example.py": "# fixture manager source\n",
        "manager/pyproject.toml": project,
        "manager/WeChatReBot.exe": "unchanged manager fixture",
        "runtime/runtime-manifest.json": json.dumps({
            "schema_version": 1,
            "python": {"version": PYTHON_VERSION, "architecture": "x64", "sha256": PYTHON_SHA256},
        }),
        "runtime/python/python.exe": "Python is never copied",
        "runtime/ffmpeg/bin/ffmpeg.exe": "FFmpeg is never copied",
    }
    _inventory(baseline, contents)
    return root, baseline, contents


def test_snapshot_preserves_complete_inventory_without_reading_or_copying_runtime(tmp_path, preparation, monkeypatch):
    staging = tmp_path / "staging"
    contents = {
        f"{PLUGINS}/existing/main.py": "answer = 1\n",
        "runtime/python/python.exe": "large runtime",
        "runtime/ffmpeg/bin/ffmpeg.exe": "large tool",
        "runtime/runtime-manifest.json": "{}",
        "app/bridge/vendor/hook/helper.dll": "fixed hook",
        "app/astrbot/data/config/fixture_config.json": "user defaults",
    }
    original = _inventory(staging, contents, staging=True)
    real_hash = preparation.sha256_file

    def no_runtime_hash(path):
        assert path.name not in {"python.exe", "ffmpeg.exe"}
        return real_hash(path)

    monkeypatch.setattr(preparation, "sha256_file", no_runtime_hash)
    snapshot = preparation.snapshot_baseline(staging, tmp_path / "snapshot", version="1.0.1")
    manifest = json.loads((snapshot / "config/installed-payload.json").read_text("utf-8"))
    assert manifest == original
    assert installed_payload_fingerprint(snapshot) == preparation._fingerprint_manifest(original)
    assert not (snapshot / "runtime/python").exists()
    assert not (snapshot / "runtime/ffmpeg").exists()
    assert not (snapshot / "app/astrbot/data/config/fixture_config.json").exists()
    assert (snapshot / "app/bridge/vendor/hook/helper.dll").read_text() == "fixed hook"
    clone = preparation.snapshot_baseline(snapshot, tmp_path / "cloned-snapshot")
    assert installed_payload_fingerprint(clone) == installed_payload_fingerprint(snapshot)
    with pytest.raises(ValueError, match="拒绝覆盖"):
        preparation.snapshot_baseline(staging, snapshot)


@pytest.mark.parametrize("entry", [
    {"path": "../escaped.py", "sha256": "A" * 64},
    {"path": "manager/main.py", "sha256": "wrong"},
])
def test_snapshot_rejects_unsafe_inventory(tmp_path, preparation, entry):
    staging = tmp_path / "staging"
    _files(staging, {"release-manifest.json": json.dumps({"schema_version": 1, "files": [entry]})})
    with pytest.raises(PayloadError):
        preparation.snapshot_baseline(staging, tmp_path / "snapshot")


def test_snapshot_rejects_tampered_component_and_symlink(tmp_path, preparation):
    source = tmp_path / "source"
    _inventory(source, {"manager/source.py": "original"})
    (source / "manager/source.py").write_text("tampered", encoding="utf-8")
    with pytest.raises(PayloadError, match="摘要不符"):
        preparation.snapshot_baseline(source, tmp_path / "tampered")
    target = tmp_path / "outside.py"
    target.write_text("original", encoding="utf-8")
    (source / "manager/source.py").unlink()
    try:
        (source / "manager/source.py").symlink_to(target)
    except OSError:
        pytest.skip("This Windows session cannot create a symbolic link")
    with pytest.raises(PayloadError, match="链接"):
        preparation.snapshot_baseline(source, tmp_path / "linked")


def test_preparation_builds_exact_deltas_and_reusable_targets_without_changing_inputs(tmp_path, preparation, monkeypatch):
    root, baseline, contents = _fixture(tmp_path, preparation, monkeypatch)
    original_inventory = (baseline / "config/installed-payload.json").read_bytes()

    def no_build(*args, **kwargs):
        pytest.fail("unchanged manager must not be rebuilt")

    monkeypatch.setattr(preparation, "_manager_executable", no_build)
    output = root / "artifacts/prepared"
    result = preparation.prepare_component_payloads(root, root / "vendor", [baseline], output, version="1.0.2", build=3)
    assert len(result["payloads"]) == len(result["target_baselines"]) == 1
    assert result["manager_build_required"] is result["runtime_build_required"] is False
    with zipfile.ZipFile(result["payloads"][0]) as archive:
        manifest = json.loads(archive.read("release-manifest.json"))
        assert manifest["schema_version"] == 2
        assert manifest["base_fingerprint"] == installed_payload_fingerprint(baseline)
        assert manifest["release_version"] == "1.0.2" and manifest["release_build"] == 3
        assert f"{PLUGINS}/existing/obsolete.png" in {entry["path"] for entry in manifest["remove"]}
        assert not any(name.startswith("runtime/") for name in archive.namelist())
    target = result["target_baselines"][0]
    assert (target / f"{PLUGINS}/existing/main.py").read_text() == "answer = 2\n"
    assert not (target / f"{PLUGINS}/existing/obsolete.png").exists()
    assert not (target / "runtime/python/python.exe").exists()
    assert (baseline / "config/installed-payload.json").read_bytes() == original_inventory
    assert (baseline / f"{PLUGINS}/existing/obsolete.png").read_text() == contents[f"{PLUGINS}/existing/obsolete.png"]
    assert installed_payload_fingerprint(target) == result["reports"][0]["target_fingerprint"]
    assert json.loads((output / "preparation-report.json").read_text())["runtime_build_required"] is False

    # Prepare a later cumulative update from the saved target and the original
    # full baseline; no Python files are needed in either snapshot.
    (root / "vendor/existing/main.py").write_text("answer = 3\n", encoding="utf-8")
    vendor_manifest = json.loads((root / "vendor/manifest.json").read_text())
    for item in vendor_manifest["files"]:
        item["sha256"] = sha256_file(root / "vendor" / item["path"]).lower()
    _files(root, {"vendor/manifest.json": json.dumps(vendor_manifest)})
    later = preparation.prepare_component_payloads(
        root, root / "vendor", [baseline, target], root / "artifacts/later", version="1.0.2", build=4,
    )
    assert len(later["payloads"]) == len(later["target_baselines"]) == 2
    assert {report["base_fingerprint"] for report in later["reports"]} == {
        installed_payload_fingerprint(baseline), installed_payload_fingerprint(target),
    }
    assert len({installed_payload_fingerprint(path) for path in later["target_baselines"]}) == 1


def test_duplicate_and_incompatible_baselines_do_not_trigger_manager_build(tmp_path, preparation, monkeypatch):
    root, baseline, _ = _fixture(tmp_path, preparation, monkeypatch)
    monkeypatch.setattr(preparation, "_manager_executable", lambda *args: pytest.fail("must fail before build"))
    with pytest.raises(ValueError, match="重复基线"):
        preparation.prepare_component_payloads(
            root, root / "vendor", [baseline, baseline], root / "artifacts/duplicate", version="1.0.2",
        )
    assert not (root / "artifacts/duplicate").exists()
    (root / "release/runtime-requirements.lock").write_text("httpx==0.29.0\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="基线不兼容"):
        preparation.prepare_component_payloads(
            root, root / "vendor", [baseline], root / "artifacts/incompatible", version="1.0.2",
        )
    assert not (root / "artifacts/incompatible/payloads").exists()


def test_each_baseline_discovers_its_own_retired_components(tmp_path, preparation, monkeypatch):
    root, baseline, contents = _fixture(tmp_path, preparation, monkeypatch)
    older = tmp_path / "older"
    _inventory(older, {
        **contents,
        "legal/plugin-manifest.json": json.dumps({"plugins": [{"name": "existing"}, {"name": "retired"}]}),
        f"{PLUGINS}/retired/main.py": "old retired component\n",
    })
    result = preparation.prepare_component_payloads(
        root, root / "vendor", [baseline, older], root / "artifacts/retired", version="1.0.2",
    )
    retired = f"{PLUGINS}/retired/main.py"
    assert retired not in result["reports"][0]["remove"]
    assert retired in result["reports"][1]["remove"]
    assert len({installed_payload_fingerprint(path) for path in result["target_baselines"]}) == 1


def test_manager_is_validated_once_and_reused_for_multiple_bases(tmp_path, preparation, monkeypatch):
    root, baseline, contents = _fixture(tmp_path, preparation, monkeypatch)
    alternate = tmp_path / "alternate"
    _inventory(alternate, {**contents, f"{PLUGINS}/existing/main.py": "answer = 0\n"})
    monkeypatch.setattr(preparation.local, "manager_changed", lambda *_: True)
    from publish_manager_update import _validated_manager
    manager_build = tmp_path / "manager-build"
    _files(manager_build, {"dist/WeChatReBot.exe": "MZ fixture manager"})
    executable = manager_build / "dist/WeChatReBot.exe"
    _files(manager_build, {"manager-build-manifest.json": json.dumps({
        "schema_version": 1, "manager_version": "1.0.2", "filename": executable.name,
        "size": executable.stat().st_size, "sha256": sha256_file(executable),
        "self_test": {"ok": True, "frozen": True, "architecture": 64, "manager_version": "1.0.2"},
    })})
    calls = []

    def reuse_manager(root_arg, output_arg, version, build):
        calls.append(build)
        return _validated_manager(build, version)

    monkeypatch.setattr(preparation, "_manager_executable", reuse_manager)
    result = preparation.prepare_component_payloads(
        root, root / "vendor", [baseline, alternate], root / "artifacts/manager",
        version="1.0.2", manager_build=manager_build,
    )
    assert calls == [manager_build]
    assert result["manager_build_required"] is True
    for target in result["target_baselines"]:
        assert (target / "manager/WeChatReBot.exe").read_bytes() == executable.read_bytes()


def test_scan_failure_blocks_payload_and_cli_can_snapshot(tmp_path, preparation, monkeypatch):
    root, baseline, _ = _fixture(tmp_path, preparation, monkeypatch)
    monkeypatch.setattr(preparation, "scan_release", lambda _: [{"reason": "fixture finding"}])
    with pytest.raises(RuntimeError, match="候选检查未通过"):
        preparation.prepare_component_payloads(
            root, root / "vendor", [baseline], root / "artifacts/scan", version="1.0.2",
        )
    assert not (root / "artifacts/scan/payloads").exists()
    snapshot = tmp_path / "cli-snapshot"
    assert preparation.main(["--snapshot-source", str(baseline), "--output", str(snapshot)]) == 0
    assert installed_payload_fingerprint(snapshot) == installed_payload_fingerprint(baseline)
