from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import zipfile

import pytest

from wechatrebot_manager.payload import PayloadError, sha256_file, verify_payload


PLUGIN = "app/astrbot/data/plugins/fixture_plugin"


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_incremental_update.py"
    spec = importlib.util.spec_from_file_location("build_incremental_update_test", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _files(root: Path, contents: dict[str, str]) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for name, value in contents.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


def _baseline(root: Path, contents: dict[str, str]) -> None:
    _files(root, contents)
    _files(root, {"config/installed-payload.json": json.dumps({
        "schema_version": 1, "release_version": "0.9.9",
        "files": [{"path": name, "sha256": sha256_file(root / name)} for name in contents],
    })})


def _runtime_metadata(version: str = "3.13.9") -> dict[str, str]:
    return {"runtime/runtime-manifest.json": json.dumps({
        "schema_version": 1, "python": {"version": version, "architecture": "x64"},
    })}


def test_scoped_package_contains_only_changes_and_owned_removals(tmp_path, monkeypatch):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {
        f"{PLUGIN}/main.py": "old", f"{PLUGIN}/old.png": "obsolete",
        f"{PLUGIN}/same.py": "same", "runtime/python/python.exe": "large runtime",
        "app/astrbot/data/plugins/other_plugin/main.py": "unrelated",
    })
    _files(installed, {f"{PLUGIN}/user-cache.json": "user cache"})
    _files(candidate, {f"{PLUGIN}/main.py": "new", f"{PLUGIN}/same.py": "same", f"{PLUGIN}/help.py": "help"})
    hashed = []
    original = module.sha256_file

    def checked_hash(path):
        hashed.append(path)
        assert "runtime" not in path.parts
        return original(path)

    monkeypatch.setattr(module, "sha256_file", checked_hash)
    archive = tmp_path / "update.zip"
    report = module.build_incremental_update(candidate, installed, archive, managed_roots=[PLUGIN], version="0.9.9", build=17)
    assert report["added_count"] == 1
    assert report["changed_count"] == 1
    assert report["unchanged_count"] == 1
    assert report["removed_count"] == 1
    assert report["remove"] == [f"{PLUGIN}/old.png"]
    assert report["update_bytes"] == len("newhelp")
    assert report["removed_bytes"] == len("obsolete")
    assert report["archive_bytes"] == archive.stat().st_size
    assert hashed
    verified = verify_payload(archive)
    assert verified.manifest["schema_version"] == 2
    assert verified.manifest["release_build"] == 17
    assert verified.manifest["update_kind"] == "incremental"
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {"release-manifest.json", f"{PLUGIN}/main.py", f"{PLUGIN}/help.py"}
    assert (installed / f"{PLUGIN}/old.png").is_file()
    assert (installed / f"{PLUGIN}/user-cache.json").read_text() == "user cache"


def test_plan_never_creates_output_or_parent(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {f"{PLUGIN}/main.py": "old"})
    _files(candidate, {f"{PLUGIN}/main.py": "new"})
    output = tmp_path / "never-created" / "update.zip"
    result = module.build_incremental_update(candidate, installed, output, managed_roots=[PLUGIN], plan=True)
    assert result["changed_count"] == 1
    assert result["output"] is None
    assert not output.parent.exists()


def test_empty_component_builds_removal_only_payload(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {f"{PLUGIN}/main.py": "old"})
    (candidate / PLUGIN).mkdir(parents=True)
    output = tmp_path / "remove.zip"
    report = module.build_incremental_update(candidate, installed, output, managed_roots=[PLUGIN])
    assert report["files"] == []
    assert report["removed_count"] == 1
    assert verify_payload(output).manifest["remove"][0]["path"] == f"{PLUGIN}/main.py"


def test_missing_candidate_scope_does_not_delete_component(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {f"{PLUGIN}/main.py": "old"})
    candidate.mkdir()
    with pytest.raises(PayloadError, match="候选组件缺失"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN])


@pytest.mark.parametrize("scope", ["runtime", "data", "config", "config/manager.json", "app/astrbot/data/config", "app/astrbot/data/cmd_config.json", "app", "unrelated"])
def test_rejects_runtime_user_data_and_unbounded_scopes(tmp_path, scope):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {f"{PLUGIN}/main.py": "old"})
    candidate.mkdir()
    with pytest.raises(PayloadError):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[scope], plan=True)


def test_rejects_changed_baseline_and_unowned_file_collision(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {f"{PLUGIN}/main.py": "old"})
    _files(candidate, {f"{PLUGIN}/main.py": "new"})
    _files(installed, {f"{PLUGIN}/main.py": "locally edited"})
    with pytest.raises(PayloadError, match="安装基线文件已变化"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN])
    _files(installed, {f"{PLUGIN}/main.py": "old", f"{PLUGIN}/extra.py": "user file"})
    _files(candidate, {f"{PLUGIN}/extra.py": "new managed file"})
    with pytest.raises(PayloadError, match="未受管"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN])


def test_new_plugin_reuses_verified_existing_dependencies(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {**_runtime_metadata(), "legal/runtime-requirements.lock": "httpx==0.28.1\n"})
    _files(candidate, {f"{PLUGIN}/main.py": "import httpx", f"{PLUGIN}/requirements.txt": "httpx>=0.28,<1\n"})
    report = module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)
    assert report["added_count"] == 2


@pytest.mark.parametrize("requirement", ["brand-new-package>=1", "httpx>=1", "httpx[socks]>=0.28", "-r another.txt"])
def test_new_dependencies_are_refused_without_running_pip(tmp_path, requirement):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {**_runtime_metadata(), "legal/runtime-requirements.lock": "httpx==0.28.1\n"})
    _files(candidate, {f"{PLUGIN}/requirements.txt": requirement})
    with pytest.raises(PayloadError, match="依赖"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)


@pytest.mark.parametrize("version", ["3.13.9", "3.14.2"])
def test_dependency_markers_use_installed_python_full_version(tmp_path, version):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {**_runtime_metadata(version), "legal/runtime-requirements.lock": "httpx==0.28.1\n"})
    _files(candidate, {f"{PLUGIN}/requirements.txt":
                       'missing-required-package>=1; python_full_version >= "3.13.1"\n'})
    with pytest.raises(PayloadError, match="未满足的新依赖"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)


def test_dependency_marker_can_exclude_requirement_from_installed_python(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, _runtime_metadata("3.13.9"))
    _files(candidate, {f"{PLUGIN}/requirements.txt":
                       'missing-required-package>=1; python_full_version >= "3.13.10"\n'})
    report = module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)
    assert report["added_count"] == 1


@pytest.mark.parametrize("constraint", [">=3.14", "<3.13", ">=3.13.10"])
def test_pyproject_python_requirement_rejects_incompatible_runtime(tmp_path, constraint):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, _runtime_metadata("3.13.9"))
    _files(candidate, {f"{PLUGIN}/pyproject.toml": f'[project]\nrequires-python = "{constraint}"\n'})
    with pytest.raises(PayloadError, match="Python 版本要求不兼容"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)


def test_pyproject_python_requirement_accepts_compatible_runtime_without_dependencies(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, _runtime_metadata("3.14.2"))
    _files(candidate, {f"{PLUGIN}/pyproject.toml": '[project]\nrequires-python = ">=3.14,<3.15"\n'})
    report = module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)
    assert report["added_count"] == 1


@pytest.mark.parametrize("state", ["missing", "modified", "invalid"])
def test_dependency_validation_requires_trusted_runtime_metadata(tmp_path, state):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    contents = {"legal/runtime-requirements.lock": "httpx==0.28.1\n"}
    if state != "missing":
        contents.update(_runtime_metadata())
    if state == "invalid":
        contents["runtime/runtime-manifest.json"] = "{}"
    _baseline(installed, contents)
    if state == "modified":
        _files(installed, _runtime_metadata("3.14.2"))
    _files(candidate, {f"{PLUGIN}/requirements.txt": "httpx>=0.28\n"})
    with pytest.raises(PayloadError, match="运行时元信息"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)


def test_frozen_manager_source_metadata_does_not_require_bot_dependencies(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {"manager/pyproject.toml": '[project]\nname = "old-manager"\n'})
    _files(candidate, {"manager/pyproject.toml":
                       '[project]\nrequires-python = ">=3.14"\ndependencies = ["manager-only-package>=1"]\n'})
    report = module.build_incremental_update(candidate, installed, tmp_path / "update.zip",
                                             managed_roots=["manager/pyproject.toml"], plan=True)
    assert report["changed_count"] == 1


def test_dependency_lock_changes_are_refused(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {"legal/runtime-requirements.lock": "httpx==0.28.1\n"})
    _files(candidate, {"legal/runtime-requirements.lock": "httpx==0.30.0\n"})
    with pytest.raises(PayloadError, match="底层运行环境声明"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=["legal"], plan=True)


def test_candidate_cannot_be_live_installation_or_its_descendant(tmp_path):
    module = _builder()
    installed = tmp_path / "installed"
    _baseline(installed, {f"{PLUGIN}/main.py": "old"})
    with pytest.raises(PayloadError, match="完全分离"):
        module.build_incremental_update(installed, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)


def test_unchanged_native_file_is_ignored_but_changes_are_refused(tmp_path):
    module = _builder()
    installed, candidate = tmp_path / "installed", tmp_path / "candidate"
    _baseline(installed, {f"{PLUGIN}/native.pyd": "existing native"})
    _files(candidate, {f"{PLUGIN}/native.pyd": "existing native"})
    report = module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN])
    assert report["output"] is None
    _files(candidate, {f"{PLUGIN}/native.pyd": "new native"})
    with pytest.raises(PayloadError, match="底层二进制"):
        module.build_incremental_update(candidate, installed, tmp_path / "update.zip", managed_roots=[PLUGIN], plan=True)
