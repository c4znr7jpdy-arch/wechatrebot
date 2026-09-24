import importlib.util
import json
from pathlib import Path

import pytest


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_legal_inventory.py"
    spec = importlib.util.spec_from_file_location("build_legal_inventory", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_license_expression_uses_cyclonedx_expression() -> None:
    entry = _builder()._license_entry("Apache-2.0 OR BSD-3-Clause")

    assert entry == {"expression": "Apache-2.0 OR BSD-3-Clause"}


def test_unknown_license_is_not_reported_as_spdx_identifier() -> None:
    entry = _builder()._license_entry("NOASSERTION")

    assert entry == {"license": {"name": "NOASSERTION"}}


def test_bundled_node_inventory_preserves_redistribution_license(tmp_path: Path, monkeypatch) -> None:
    builder = _builder()
    driver = tmp_path / "playwright" / "driver"
    driver.mkdir(parents=True)
    (driver / "node.exe").write_bytes(b"bundled node")
    (driver / "LICENSE").write_text("Node license and bundled third-party notices", encoding="utf-8")
    monkeypatch.setattr(builder.subprocess, "check_output", lambda *args, **kwargs: "v24.15.0\n")

    components, documents = builder._bundled_node_inventory(tmp_path)

    assert components[0]["version"] == "24.15.0"
    assert components[0]["hashes"][0]["content"] == builder._sha256(driver / "node.exe")
    assert documents == [("Node.js 24.15.0 and bundled dependencies", driver / "LICENSE")]


def test_shared_sources_do_not_invent_unavailable_licenses(tmp_path: Path) -> None:
    (tmp_path / "rocom").mkdir()
    components, documents = _builder()._shared_components(tmp_path, {"shared_runtime": [{
        "source": "rocom", "name": "rocom", "version": "local", "license": "NOASSERTION", "purpose": "game tools"
    }]})
    assert components[0]["licenses"] == [{"license": {"name": "NOASSERTION"}}]
    assert documents == []


def _ffmpeg_fixture(tmp_path: Path) -> tuple[dict[str, object], dict[str, object]]:
    definition = json.loads(
        (Path(__file__).parents[1] / "release" / "ffmpeg-runtime.json").read_text(
            encoding="utf-8"
        )
    )
    license_file = tmp_path / "ffmpeg" / "LICENSE.txt"
    license_file.parent.mkdir(parents=True)
    license_file.write_text("GNU LESSER GENERAL PUBLIC LICENSE\nVersion 3\n", encoding="utf-8")
    metadata = {
        **definition,
        "files": [{
            "path": "LICENSE.txt",
            "size": license_file.stat().st_size,
            "sha256": _builder()._sha256(license_file),
        }],
    }
    return definition, metadata


def test_ffmpeg_inventory_records_pinned_archive_license_and_sources(tmp_path: Path) -> None:
    definition, metadata = _ffmpeg_fixture(tmp_path)
    builder = _builder()

    validated = builder._validated_ffmpeg_metadata(tmp_path, {"ffmpeg": metadata}, definition)
    component = builder._ffmpeg_component(validated)

    assert component["licenses"] == [{"license": {"id": "LGPL-3.0-or-later"}}]
    assert component["hashes"] == [{"alg": "SHA-256", "content": definition["archive_sha256"]}]
    assert {item["url"] for item in component["externalReferences"]} == {
        definition["archive_url"], definition["source_url"], definition["build_source_url"]
    }
    properties = {item["name"]: item["value"] for item in component["properties"]}
    assert properties["wechatrebot:distribution"] == "lgpl-shared"
    assert properties["wechatrebot:source_commit"] == definition["source_commit"]


@pytest.mark.parametrize("field", ["version", "license", "archive_sha256", "source_commit"])
def test_ffmpeg_inventory_rejects_unmatched_runtime(tmp_path: Path, field: str) -> None:
    definition, metadata = _ffmpeg_fixture(tmp_path)
    metadata[field] = "changed"

    with pytest.raises(RuntimeError, match=f"does not match release definition: {field}"):
        _builder()._validated_ffmpeg_metadata(tmp_path, {"ffmpeg": metadata}, definition)


def test_ffmpeg_inventory_rejects_changed_license_text(tmp_path: Path) -> None:
    definition, metadata = _ffmpeg_fixture(tmp_path)
    (tmp_path / "ffmpeg" / "LICENSE.txt").write_text("changed", encoding="utf-8")

    with pytest.raises(RuntimeError, match="license text does not match"):
        _builder()._validated_ffmpeg_metadata(tmp_path, {"ffmpeg": metadata}, definition)


def test_legal_inventory_includes_ffmpeg_in_all_artifacts(tmp_path: Path, monkeypatch) -> None:
    builder = _builder()
    _, ffmpeg = _ffmpeg_fixture(tmp_path)
    python_license = tmp_path / "python" / "LICENSE.txt"
    python_license.parent.mkdir()
    python_license.write_text("Python license text", encoding="utf-8")
    (tmp_path / "runtime-manifest.json").write_text(
        json.dumps({
            "python": {"version": "3.13.9", "source_url": "https://www.python.org/"},
            "wheelhouse": {"wheel_count": 0, "aggregate_sha256": "A" * 64},
            "ffmpeg": ffmpeg,
        }),
        encoding="utf-8",
    )
    monkeypatch.setattr(builder, "_python_components", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(builder, "_plugin_components", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(builder, "_bundled_node_inventory", lambda *args, **kwargs: ([], []))
    monkeypatch.setattr(builder, "_shared_components", lambda *args, **kwargs: ([], []))
    outputs = {name: tmp_path / name for name in ("sbom", "notices", "licenses", "review")}
    corresponding_source = tmp_path / "corresponding-source.md"
    corresponding_source.write_text("Source information", encoding="utf-8")

    result = builder.main([
        "--runtime-root", str(tmp_path),
        "--manager-site-packages", str(tmp_path),
        "--project-root", str(tmp_path),
        "--corresponding-source", str(corresponding_source),
        *(value for name, path in outputs.items() for value in (f"--{name}", str(path))),
    ])

    assert result == 0
    for output in outputs.values():
        assert b"\r\n" not in output.read_bytes(), output
    sbom = json.loads(outputs["sbom"].read_text(encoding="utf-8"))
    assert [item["name"] for item in sbom["components"]] == ["CPython", "FFmpeg"]
    assert {item["name"]: item["value"] for item in sbom["metadata"]["properties"]}[
        "wechatrebot:ffmpeg_included"
    ] == "true"
    assert "LGPL-3.0-or-later" in outputs["notices"].read_text(encoding="utf-8")
    assert f"COMPONENT: FFmpeg {ffmpeg['version']}" in outputs["licenses"].read_text(encoding="utf-8")
    review = json.loads(outputs["review"].read_text(encoding="utf-8"))
    assert review["runtime_component_count"] == 2
    assert review["artifacts"]["license_texts"]["sha256"] == builder._sha256(outputs["licenses"])
