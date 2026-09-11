import importlib.util
import hashlib
import json
from pathlib import Path
import struct
from types import SimpleNamespace

import pytest


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_staging.py"
    spec = importlib.util.spec_from_file_location("build_staging", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runtime_manifest(commercial: Path, runtime: Path) -> dict[str, object]:
    wheelhouse = json.loads(
        (commercial / "release" / "runtime-wheelhouse.json").read_text(
            encoding="utf-8"
        )
    )
    ffmpeg_release = json.loads(
        (commercial / "release" / "ffmpeg-runtime.json").read_text(encoding="utf-8")
    )
    ffmpeg_files = []
    for relative, content in {
        "bin/ffmpeg.exe": b"ffmpeg",
        "bin/ffprobe.exe": b"ffprobe",
        "bin/avutil-60.dll": b"avutil",
        "LICENSE.txt": b"LGPL-3.0-or-later",
    }.items():
        target = runtime / "ffmpeg" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        ffmpeg_files.append(
            {
                "path": relative,
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest().upper(),
            }
        )
    return {
        "schema_version": 1,
        "python": {
            "version": wheelhouse["python_version"],
            "architecture": "x64",
        },
        "wheelhouse": {
            key: wheelhouse[key]
            for key in ("wheel_count", "total_bytes", "aggregate_sha256")
        },
        "ffmpeg": {
            key: ffmpeg_release[key]
            for key in (
                "version",
                "architecture",
                "archive_url",
                "archive_sha256",
                "license",
                "source_url",
                "build_source_url",
            )
        }
        | {"files": ffmpeg_files},
    }


def _write_runtime_fixture(runtime: Path, commercial: Path) -> dict[str, object]:
    (runtime / "python").mkdir(parents=True)
    (runtime / "python" / "python.exe").write_bytes(b"placeholder")
    manifest = _runtime_manifest(commercial, runtime)
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    return manifest


def test_runtime_root_validation_accepts_matching_fingerprint(tmp_path: Path) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    manifest = _write_runtime_fixture(runtime, commercial)
    validated = _builder()._validate_runtime_root(runtime, commercial)
    assert validated["wheelhouse"] == manifest["wheelhouse"]
    assert validated["ffmpeg"] == manifest["ffmpeg"]


def test_runtime_root_validation_rejects_wrong_fingerprint(tmp_path: Path) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    manifest = _write_runtime_fixture(runtime, commercial)
    manifest["wheelhouse"]["wheel_count"] = 1
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )
    with pytest.raises(RuntimeError, match="fingerprint mismatch"):
        _builder()._validate_runtime_root(runtime, commercial)


def test_runtime_root_validation_rejects_tampered_ffmpeg_file(
    tmp_path: Path,
) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    _write_runtime_fixture(runtime, commercial)
    (runtime / "ffmpeg" / "bin" / "ffprobe.exe").write_bytes(b"changed")

    with pytest.raises(RuntimeError, match="FFmpeg"):
        _builder()._validate_runtime_root(runtime, commercial)


@pytest.mark.parametrize(
    "field",
    [
        "version",
        "architecture",
        "archive_sha256",
        "archive_url",
        "license",
        "source_url",
        "build_source_url",
    ],
)
def test_runtime_root_validation_rejects_wrong_ffmpeg_identity(
    tmp_path: Path, field: str,
) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    manifest = _write_runtime_fixture(runtime, commercial)
    manifest["ffmpeg"][field] = "not-the-release-identity"
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="FFmpeg identity mismatch"):
        _builder()._validate_runtime_root(runtime, commercial)


def test_runtime_root_validation_rejects_python_only_runtime(tmp_path: Path) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    manifest = _write_runtime_fixture(runtime, commercial)
    del manifest["ffmpeg"]
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="FFmpeg fingerprint is missing"):
        _builder()._validate_runtime_root(runtime, commercial)


@pytest.mark.parametrize(
    "relative", ["bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/avutil-60.dll", "LICENSE.txt"]
)
def test_runtime_root_validation_rejects_missing_ffmpeg_file(
    tmp_path: Path, relative: str,
) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    _write_runtime_fixture(runtime, commercial)
    (runtime / "ffmpeg" / relative).unlink()

    with pytest.raises(RuntimeError, match="FFmpeg required file is missing"):
        _builder()._validate_runtime_root(runtime, commercial)


@pytest.mark.parametrize(
    "relative", ["bin/ffmpeg.exe", "bin/ffprobe.exe", "bin/avutil-60.dll", "LICENSE.txt"]
)
def test_runtime_root_validation_rejects_omitted_required_ffmpeg_record(
    tmp_path: Path, relative: str,
) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    manifest = _write_runtime_fixture(runtime, commercial)
    manifest["ffmpeg"]["files"] = [
        item for item in manifest["ffmpeg"]["files"] if item["path"] != relative
    ]
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="FFmpeg manifest omits required"):
        _builder()._validate_runtime_root(runtime, commercial)


@pytest.mark.parametrize("unsafe_path", ["../outside.dll", r"bin\ffmpeg.exe", "C:/ffmpeg.exe"])
def test_runtime_root_validation_rejects_unsafe_ffmpeg_record(
    tmp_path: Path, unsafe_path: str,
) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    manifest = _write_runtime_fixture(runtime, commercial)
    manifest["ffmpeg"]["files"][0]["path"] = unsafe_path
    (runtime / "runtime-manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    with pytest.raises(RuntimeError, match="FFmpeg file path"):
        _builder()._validate_runtime_root(runtime, commercial)


def test_runtime_root_validation_rejects_unrecorded_ffmpeg_file(tmp_path: Path) -> None:
    commercial = Path(__file__).parents[1]
    runtime = tmp_path / "runtime"
    _write_runtime_fixture(runtime, commercial)
    (runtime / "ffmpeg" / "bin" / "unexpected.dll").write_bytes(b"not fingerprinted")

    with pytest.raises(RuntimeError, match="FFmpeg contains files outside its fingerprint"):
        _builder()._validate_runtime_root(runtime, commercial)


def test_staging_copies_bundled_ffmpeg_and_records_it(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    source = tmp_path / "source"
    output = commercial / "artifacts" / "staging"
    runtime = tmp_path / "runtime"
    source.mkdir()
    (commercial / "release").mkdir(parents=True)
    (commercial / "release" / "source-manifest.json").write_text(
        json.dumps({"entries": []}), encoding="utf-8"
    )
    (commercial / "pyproject.toml").write_text(
        '[project]\nversion = "0.8.8"\n', encoding="utf-8"
    )
    (runtime / "python").mkdir(parents=True)
    (runtime / "python" / "python.exe").write_bytes(b"python")
    (runtime / "ffmpeg" / "bin").mkdir(parents=True)
    (runtime / "ffmpeg" / "bin" / "ffmpeg.exe").write_bytes(b"ffmpeg")
    (runtime / "ffmpeg" / "bin" / "ffprobe.exe").write_bytes(b"ffprobe")
    (runtime / "runtime-manifest.json").write_text("{}", encoding="utf-8")
    runtime_manifest = {
        "python": {"version": "3.13.9", "architecture": "x64"},
        "wheelhouse": {"wheel_count": 1},
        "ffmpeg": {
            "version": "n8.1.2-test",
            "architecture": "win64",
            "license": "LGPL-3.0-or-later",
        },
    }
    staged_checks: list[Path] = []
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", commercial)
    monkeypatch.setattr(
        module, "_validate_runtime_root", lambda *_args: runtime_manifest
    )
    monkeypatch.setattr(
        module, "_validate_staged_runtime", lambda value: staged_checks.append(value)
    )
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    assert (
        module.main(
            [
                "--source-root",
                str(source),
                "--output",
                str(output),
                "--runtime-root",
                str(runtime),
            ]
        )
        == 0
    )

    assert (output / "runtime" / "ffmpeg" / "bin" / "ffmpeg.exe").is_file()
    assert (output / "runtime" / "ffmpeg" / "bin" / "ffprobe.exe").is_file()
    assert staged_checks == [output]
    release_manifest = json.loads(
        (output / "release-manifest.json").read_text(encoding="utf-8")
    )
    assert release_manifest["runtime"]["ffmpeg"] == runtime_manifest["ffmpeg"]


def test_staging_manager_pe_check_rejects_plain_file(tmp_path: Path) -> None:
    source = tmp_path / "manager.exe"
    source.write_bytes(b"not-a-pe")
    with pytest.raises(RuntimeError, match="Windows PE"):
        _builder()._validate_manager_exe(source)


def test_staging_manager_pe_check_requires_x64(tmp_path: Path) -> None:
    source = tmp_path / "manager.exe"
    payload = bytearray(256)
    payload[:2] = b"MZ"
    struct.pack_into("<I", payload, 0x3C, 0x80)
    payload[0x80:0x84] = b"PE\x00\x00"
    struct.pack_into("<H", payload, 0x84, 0x014C)
    source.write_bytes(payload)
    with pytest.raises(RuntimeError, match="x64"):
        _builder()._validate_manager_exe(source)


def test_staging_provenance_does_not_record_source_machine_path(
    tmp_path: Path,
) -> None:
    output = tmp_path / "staging"
    target = output / "manager" / "WeChatReBot.exe"

    assert _builder()._copied_record(target, output) == {
        "target": "manager/WeChatReBot.exe"
    }


def test_staging_checks_and_applies_declared_plugin_patches(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    output = commercial / "artifacts" / "staging"
    patch = commercial / "patches" / "astrbot-plugin-demo_plugin.patch"
    plugin = output / "app" / "astrbot" / "data" / "plugins" / "demo_plugin"
    patch.parent.mkdir(parents=True)
    plugin.mkdir(parents=True)
    patch.write_text("patch", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    module._apply_plugin_patches(commercial, output)

    assert len(calls) == 2
    assert "--check" in calls[0][0]
    assert "--check" not in calls[1][0]
    assert calls[0][1] == {"cwd": commercial, "check": True}
    assert calls[1][1] == {"cwd": commercial, "check": True}
    assert any(
        argument.startswith("--directory=artifacts/staging/")
        for argument in calls[0][0]
    )


def test_staging_checks_applies_and_archives_bridge_patch(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    output = commercial / "artifacts" / "staging"
    patch = commercial / "patches" / "hook-bridge-group-directory.patch"
    bridge = output / "app" / "bridge"
    patch.parent.mkdir(parents=True)
    bridge.mkdir(parents=True)
    patch.write_text("bridge patch", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    module._apply_bridge_patch(commercial, output)

    assert len(calls) == 2
    assert "--check" in calls[0][0]
    assert "--check" not in calls[1][0]
    assert all(
        "--directory=artifacts/staging/app/bridge" in call[0]
        for call in calls
    )
    archived = output / "legal" / "corresponding-source" / patch.name
    assert archived.read_text(encoding="utf-8") == "bridge patch"


def test_staging_applies_base_then_named_extension_patch_to_same_plugin(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    commercial = tmp_path / "commercial"
    output = commercial / "artifacts" / "staging"
    patches = commercial / "patches"
    plugin = output / "app" / "astrbot" / "data" / "plugins" / "demo_plugin"
    patches.mkdir(parents=True)
    plugin.mkdir(parents=True)
    base = patches / "astrbot-plugin-demo_plugin.patch"
    extension = patches / "astrbot-plugin-demo_plugin--permissions.patch"
    base.write_text("base", encoding="utf-8")
    extension.write_text("extension", encoding="utf-8")
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda command, **kwargs: calls.append((command, kwargs)),
    )

    module._apply_plugin_patches(commercial, output)

    applied = [Path(call[0][-1]).name for call in calls if "--check" not in call[0]]
    assert applied == [base.name, extension.name]
    assert all(
        "--directory=artifacts/staging/app/astrbot/data/plugins/demo_plugin"
        in call[0]
        for call in calls
    )


def test_staging_keeps_legacy_update_bootstrap_marker(tmp_path: Path) -> None:
    module = _builder()
    source_manifest = tmp_path / "source-manifest.json"
    source_manifest.write_text("{}", encoding="utf-8")

    manifest = module._release_manifest(
        version="0.8.6",
        source_manifest=source_manifest,
        copied=[],
        runtime=None,
        files=[],
    )

    assert manifest["source_manifest_status"] == "ready"
    assert manifest["release_version"] == "0.8.6"


def test_staging_removes_embedded_checkin_account_seeds(tmp_path: Path) -> None:
    module = _builder()
    source = (
        tmp_path
        / "app"
        / "astrbot"
        / "ai_plugin"
        / "ai_plugin"
        / "checkin.py"
    )
    source.parent.mkdir(parents=True)
    source.write_text(
        "ACCOUNTS = [\n"
        "    {\"cookie\": \"private-cookie-value-that-must-not-ship\"},\n"
        "]\n"
        "class Manager:\n"
        "    def _seed_config(self):\n"
        "        return {\"schedule\": {\"enabled\": True, \"time\": \"08:30\", \"session\": \"private-chat-session-123456\"}}\n"
        "def account_count():\n"
        "    return len(ACCOUNTS)\n",
        encoding="utf-8",
    )

    module._remove_embedded_checkin_accounts(tmp_path)

    sanitized = source.read_text(encoding="utf-8")
    assert "private-cookie-value-that-must-not-ship" not in sanitized
    assert "private-chat-session-123456" not in sanitized
    assert "ACCOUNTS = []" in sanitized
    namespace: dict[str, object] = {}
    exec(compile(sanitized, str(source), "exec"), namespace)
    assert namespace["account_count"]() == 0
    assert namespace["Manager"]()._seed_config()["schedule"] == {
        "enabled": False,
        "time": "08:30",
        "session": "",
    }


def test_staging_sanitizes_active_and_documented_project_credentials(
    tmp_path: Path,
) -> None:
    module = _builder()
    plugin = tmp_path / "app" / "astrbot" / "data" / "plugins" / "demo"
    plugin.mkdir(parents=True)
    source = plugin / "main.py"
    source.write_text(
        "CONFIG = {\"content_api_key\": \"private-active-value-123456\"}\n"
        "ENV = {\"content_api_key\": \"JIANG_CONTENT_API_KEY\"}\n"
        "# cookie = \"private-comment-value-123456\"\n"
        "def configured():\n"
        "    return bool(CONFIG[\"content_api_key\"])\n",
        encoding="utf-8",
    )
    guide = plugin / "README.md"
    guide.write_text(
        'authorization: "private-doc-value-123456"\n'
        '"session_id": "private-session-example-123456"\n',
        encoding="utf-8",
    )

    module._sanitize_project_source_credentials(tmp_path)

    sanitized = source.read_text(encoding="utf-8")
    assert "private-active-value-123456" not in sanitized
    assert "private-comment-value-123456" not in sanitized
    assert "JIANG_CONTENT_API_KEY" in sanitized
    assert "your-value-here" in sanitized
    namespace: dict[str, object] = {}
    exec(compile(sanitized, str(source), "exec"), namespace)
    assert namespace["configured"]() is False
    assert guide.read_text(encoding="utf-8") == (
        'authorization: "your-value-here"\n'
        '"session_id": "your-value-here"\n'
    )
