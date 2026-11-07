import importlib.util
import hashlib
import json
from pathlib import Path
import shutil

import pytest


def _validator():
    script = Path(__file__).parents[1] / "scripts" / "validate_release_metadata.py"
    spec = importlib.util.spec_from_file_location("validate_release_metadata", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _metadata_copy(tmp_path: Path) -> Path:
    source = Path(__file__).parents[1]
    target = tmp_path / "repo"
    shutil.copytree(source / "release", target / "release")
    shutil.copy2(source / "pyproject.toml", target / "pyproject.toml")
    package = target / "src" / "wechatrebot_manager"
    package.mkdir(parents=True)
    for name in (
        "__init__.py", "task_store.py", "hook_recall_patch.py",
        "group_analysis_schedule.py", "group_feature_runtime.py",
    ):
        shutil.copy2(source / "src" / "wechatrebot_manager" / name, package)
    shutil.copytree(source / "src/wechatrebot_manager/licensing", package / "licensing",
                    ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(source / "runtime", target / "runtime")
    shutil.copytree(source / "legal", target / "legal")
    shutil.copytree(source / "config", target / "config")
    shutil.copytree(source / "assets", target / "assets")
    shutil.copytree(source / "patches", target / "patches")
    (target / "scripts").mkdir()
    shutil.copy2(source / "scripts" / "build_runtime.py", target / "scripts")
    shutil.copy2(source / "scripts" / "build_office_renderer.py", target / "scripts")
    return target


def test_current_release_metadata_is_internally_consistent() -> None:
    root = Path(__file__).parents[1]
    assert _validator().validate_release_metadata(root) == []


def test_legacy_ready_status_does_not_enable_approval_gates(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["status"] = "ready"
    source_path.write_text(json.dumps(source), encoding="utf-8")

    assert _validator().validate_release_metadata(root) == []


def test_hook_hash_must_match_source_manifest(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    hook = json.loads((root / "release" / "hook-binaries.json").read_text(encoding="utf-8"))
    hook_paths = {item["path"] for item in hook["files"]}
    hook_entry = next(
        item
        for item in source["entries"]
        if item.get("source") in hook_paths
    )
    hook_entry["sha256"] = "0" * 64
    source_path.write_text(json.dumps(source), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert "source manifest hook hashes do not match hook-binaries.json" in errors


def test_legal_artifact_hash_change_is_rejected(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    notices = root / "legal" / "THIRD-PARTY-NOTICES.md"
    notices.write_text(notices.read_text(encoding="utf-8") + "changed\n", encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert any("legal review artifact hash mismatch" in item for item in errors)


def _sbom_copy(tmp_path: Path):
    source = Path(__file__).parents[1]
    root = tmp_path / "sbom-repo"
    (root / "release").mkdir(parents=True)
    for name in ("sbom.cdx.json", "runtime-requirements.lock", "build-requirements.lock"):
        shutil.copy2(source / "release" / name, root / "release" / name)
    sbom_path = root / "release" / "sbom.cdx.json"
    return root, sbom_path, json.loads(sbom_path.read_text("utf-8"))


def _sbom_errors(root: Path, sbom_path: Path, sbom):
    validator = _validator()
    sbom_path.write_text(json.dumps(sbom), "utf-8")
    errors: list[str] = []
    pins = {
        "runtime": validator._exact_lock(root / "release/runtime-requirements.lock", errors),
        "manager-build": validator._exact_lock(root / "release/build-requirements.lock", errors),
    }
    # Recompute the artifact hash: a stale hash must not be the reason this fails.
    review = {"schema_version": 1, "status": "review-required", "artifacts": {
        name: {"path": "release/sbom.cdx.json", "sha256": hashlib.sha256(sbom_path.read_bytes()).hexdigest()}
        for name in ("sbom", "notices", "license_texts", "corresponding_source")
    }}
    validator._validate_legal_review(
        root, review, project_version=_application_version(), python_pins=pins, errors=errors,
    )
    assert not any("artifact hash" in item for item in errors)
    return errors


def _application_version():
    import tomllib
    project = Path(__file__).parents[1] / "pyproject.toml"
    return tomllib.loads(project.read_text("utf-8"))["project"]["version"]


@pytest.mark.parametrize("group,name", [("runtime", "httpcore"), ("manager-build", "cryptography")])
def test_sbom_dependency_version_cannot_change_even_with_rehashed_legal_record(tmp_path: Path, group, name):
    root, path, sbom = _sbom_copy(tmp_path)
    component = next(item for item in sbom["components"] if item.get("group") == group and item["name"] == name)
    component["version"] = "999.0.0"
    component["purl"] = f"pkg:pypi/{name}@999.0.0"

    errors = _sbom_errors(root, path, sbom)

    assert any(f"version does not match lock: {group}/{name}" in error for error in errors)


def test_sbom_application_version_must_match_project_after_rehash(tmp_path: Path):
    root, path, sbom = _sbom_copy(tmp_path)
    sbom["metadata"]["component"]["version"] = "999.0.0"

    assert "SBOM application version does not match release version" in _sbom_errors(root, path, sbom)


@pytest.mark.parametrize("group,name", [("runtime", "httpcore"), ("manager-build", "cryptography")])
@pytest.mark.parametrize("mutation", ["missing", "duplicate", "purl-version", "purl-name", "purl-missing"])
def test_sbom_locked_components_must_be_unique_complete_and_have_matching_purls(tmp_path: Path, group, name, mutation):
    root, path, sbom = _sbom_copy(tmp_path)
    component = next(item for item in sbom["components"] if item.get("group") == group and item["name"] == name)
    if mutation == "missing":
        sbom["components"].remove(component)
    elif mutation == "duplicate":
        sbom["components"].append({**component, "name": name.upper()})
    elif mutation == "purl-missing":
        component.pop("purl")
    else:
        component["purl"] = "pkg:pypi/another-package@999" if mutation == "purl-name" else f"pkg:pypi/{name}@999"
    message = "missing locked Python component" if mutation == "missing" else "duplicate SBOM" if mutation == "duplicate" else "purl does not match"

    assert any(message in error for error in _sbom_errors(root, path, sbom))


def test_sbom_normalizes_python_names_and_allows_unlocked_manager_extra(tmp_path: Path):
    root, path, sbom = _sbom_copy(tmp_path)
    component = next(item for item in sbom["components"] if item.get("group") == "manager-build" and item["name"] == "typing_extensions")
    component["name"] = "Typing.Extensions"
    assert any(item.get("group") == "manager-build" and item["name"] == "pip" for item in sbom["components"])
    assert "pip==" not in (root / "release/build-requirements.lock").read_text("utf-8")

    assert _sbom_errors(root, path, sbom) == []


@pytest.mark.parametrize("mutation", ["root", "metadata", "components", "component", "name", "version"])
def test_malformed_sbom_reports_errors_without_crashing(tmp_path: Path, mutation):
    root, path, sbom = _sbom_copy(tmp_path)
    if mutation == "root":
        sbom = []
    elif mutation == "metadata":
        sbom["metadata"] = []
    elif mutation == "components":
        sbom["components"] = {}
    elif mutation == "component":
        sbom["components"].append(None)
    else:
        component = next(item for item in sbom["components"] if item.get("purl") == "pkg:pypi/httpcore@1.0.9")
        component[mutation] = None

    assert _sbom_errors(root, path, sbom)


@pytest.mark.parametrize("line", [
    "httpcore==1.*", "httpcore===1.0.9", "httpcore>=1.0.9", "httpcore==1.0.9,!=1.0.10",
    "httpcore==1.0.9; python_version >= '3.13'", "httpcore[extra]==1.0.9",
    "httpcore @ https://example.invalid/httpcore.whl", "httpcore==bad-version",
])
def test_exact_lock_rejects_nonexact_or_conditional_requirements(tmp_path: Path, line):
    path = tmp_path / "requirements.lock"
    path.write_text(line, "utf-8")
    errors: list[str] = []

    assert _validator()._exact_lock(path, errors) == {}
    assert any("unpinned requirement" in error for error in errors)


def test_exact_lock_rejects_normalized_duplicate_and_returns_original_pin(tmp_path: Path):
    path = tmp_path / "requirements.lock"
    path.write_text("# exact pins\nTyping_Extensions==4.16.0\ntyping.extensions==4.15.0\n", "utf-8")
    errors: list[str] = []

    assert _validator()._exact_lock(path, errors) == {"typing-extensions": "4.16.0"}
    assert errors == ["requirements.lock contains a duplicate requirement: typing-extensions"]


def test_full_release_validation_detects_httpcore_version_change_after_legal_rehash(tmp_path: Path):
    root = _metadata_copy(tmp_path)
    sbom_path = root / "release/sbom.cdx.json"
    sbom = json.loads(sbom_path.read_text("utf-8"))
    component = next(item for item in sbom["components"] if item.get("purl") == "pkg:pypi/httpcore@1.0.9")
    component.update(version="1.0.10", purl="pkg:pypi/httpcore@1.0.10")
    sbom_path.write_text(json.dumps(sbom), "utf-8")
    review_path = root / "release/legal-review.json"
    review = json.loads(review_path.read_text("utf-8"))
    review["artifacts"]["sbom"]["sha256"] = hashlib.sha256(sbom_path.read_bytes()).hexdigest().upper()
    review_path.write_text(json.dumps(review), "utf-8")

    errors = _validator().validate_release_metadata(root)

    assert errors == ["SBOM Python version does not match lock: runtime/httpcore 1.0.10 != 1.0.9"]


def test_invalid_utf8_sbom_uses_existing_validation_error_channel(tmp_path: Path):
    path = tmp_path / "sbom.cdx.json"
    path.write_bytes(b"\xff\xfe")
    errors: list[str] = []

    _validator()._validate_sbom_inventory(path, _application_version(), {}, errors)

    assert any("cannot read sbom.cdx.json" in error for error in errors)


def test_sbom_nonpython_components_do_not_gain_new_inventory_or_license_requirements(tmp_path: Path):
    root, path, sbom = _sbom_copy(tmp_path)
    sbom["components"].append({"group": "synthetic-plugin", "type": "library", "name": "unknown"})

    assert _sbom_errors(root, path, sbom) == []


def test_approved_evidence_requires_accountable_artifacts(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    evidence_path = root / "release" / "acceptance-evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["requirements"][0]["status"] = "approved"
    evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert any("no reviewer or time" in item for item in errors)
    assert any("has no artifacts" in item for item in errors)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("archive_sha256", "Z" * 64, "archive SHA-256 is invalid"),
        ("archive_root", "../ffmpeg", "archive root does not match"),
        ("archive_url", "https://example.com/latest.zip", "archive URL does not match"),
        ("license", "GPL-3.0-only", "win64 LGPL shared distribution"),
        ("source_commit", "0123456789", "source commit does not match its version"),
        ("build_source_url", "https://github.com/BtbN/FFmpeg-Builds", "build source URL does not match"),
    ],
)
def test_ffmpeg_pin_rejects_inconsistent_metadata(field: str, value: str, message: str) -> None:
    root = Path(__file__).parents[1]
    definition = json.loads((root / "release" / "ffmpeg-runtime.json").read_text(encoding="utf-8"))
    definition[field] = value
    errors: list[str] = []

    _validator()._validate_ffmpeg_definition(definition, errors)

    assert any(message in error for error in errors)


def test_ffmpeg_pin_must_be_shipped_with_payload(tmp_path: Path) -> None:
    root = _metadata_copy(tmp_path)
    source_path = root / "release" / "source-manifest.json"
    source = json.loads(source_path.read_text(encoding="utf-8"))
    source["entries"] = [
        item for item in source["entries"] if item["target"] != "legal/ffmpeg-runtime.json"
    ]
    source_path.write_text(json.dumps(source), encoding="utf-8")

    errors = _validator().validate_release_metadata(root)

    assert "source manifest must ship the pinned FFmpeg runtime definition" in errors
