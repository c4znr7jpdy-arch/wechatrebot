import importlib.util
import hashlib
from pathlib import Path
import stat
import zipfile

import pytest


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_runtime.py"
    spec = importlib.util.spec_from_file_location("build_runtime", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runtime_builder_pins_python_archive_and_hash() -> None:
    module = _builder()
    assert module.PYTHON_VERSION == "3.13.9"
    assert module.PYTHON_URL.startswith("https://www.python.org/")
    assert len(module.PYTHON_SHA256) == 64


def test_runtime_zip_extraction_rejects_traversal(tmp_path: Path) -> None:
    module = _builder()
    archive = tmp_path / "malicious.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("../outside.txt", "bad")
    with pytest.raises(RuntimeError, match="不安全路径"):
        module._safe_extract_zip(archive, tmp_path / "output")


def _ffmpeg_archive(
    archive: Path,
    definition: dict[str, object],
    *,
    omit: str | None = None,
    extra: str | zipfile.ZipInfo | None = None,
) -> dict[str, bytes]:
    files = {
        "bin/ffmpeg.exe": b"ffmpeg executable",
        "bin/ffprobe.exe": b"ffprobe executable",
        "bin/avcodec-62.dll": b"avcodec library",
        "bin/avutil-60.dll": b"avutil library",
        "LICENSE.txt": b"GNU LESSER GENERAL PUBLIC LICENSE",
    }
    if omit == "dlls":
        files = {name: data for name, data in files.items() if not name.endswith(".dll")}
    elif omit:
        files.pop(omit)
    with zipfile.ZipFile(archive, "w") as bundle:
        for name, data in files.items():
            bundle.writestr(f"{definition['archive_root']}/{name}", data)
        bundle.writestr(f"{definition['archive_root']}/bin/ffplay.exe", b"excluded")
        bundle.writestr(f"{definition['archive_root']}/include/libavcodec/avcodec.h", b"excluded")
        bundle.writestr("another-distribution/bin/extra.dll", b"excluded")
        if extra is not None:
            bundle.writestr(extra, b"unsafe")
    return files


def test_ffmpeg_install_selects_runtime_files_and_records_verified_contents(
    tmp_path: Path,
) -> None:
    module = _builder()
    definition = module._load_ffmpeg_definition(Path(__file__).parents[1])
    archive = tmp_path / "ffmpeg.zip"
    expected = _ffmpeg_archive(archive, definition)
    destination = tmp_path / "installed"

    installed = module._install_ffmpeg(archive, destination, definition)

    actual = {
        path.relative_to(destination).as_posix(): path.read_bytes()
        for path in destination.rglob("*")
        if path.is_file()
    }
    assert actual == expected
    assert installed["version"] == definition["version"]
    assert installed["archive_sha256"] == definition["archive_sha256"]
    assert installed["license"] == definition["license"]
    records = {record["path"]: record for record in installed["files"]}
    assert set(records) == set(expected)
    for name, content in expected.items():
        assert records[name]["size"] == len(content)
        assert records[name]["sha256"] == hashlib.sha256(content).hexdigest().upper()


@pytest.mark.parametrize(
    "unsafe_path",
    [
        "../outside.txt",
        "unrelated/../../outside.txt",
        r"unrelated\..\outside.txt",
        "C:/outside.txt",
        "unrelated/file.txt:stream",
    ],
)
def test_ffmpeg_install_rejects_unsafe_members_before_writing(
    tmp_path: Path, unsafe_path: str,
) -> None:
    module = _builder()
    definition = module._load_ffmpeg_definition(Path(__file__).parents[1])
    archive = tmp_path / "ffmpeg.zip"
    _ffmpeg_archive(archive, definition, extra=unsafe_path)
    destination = tmp_path / "installed"

    with pytest.raises(RuntimeError):
        module._install_ffmpeg(archive, destination, definition)

    assert not destination.exists()


def test_ffmpeg_install_rejects_symlink_member_before_writing(tmp_path: Path) -> None:
    module = _builder()
    definition = module._load_ffmpeg_definition(Path(__file__).parents[1])
    archive = tmp_path / "ffmpeg.zip"
    symlink = zipfile.ZipInfo("unrelated/link")
    symlink.create_system = 3
    symlink.external_attr = (stat.S_IFLNK | 0o777) << 16
    _ffmpeg_archive(archive, definition, extra=symlink)
    destination = tmp_path / "installed"

    with pytest.raises(RuntimeError, match="符号链接"):
        module._install_ffmpeg(archive, destination, definition)

    assert not destination.exists()


@pytest.mark.parametrize(
    "missing", ["bin/ffmpeg.exe", "bin/ffprobe.exe", "LICENSE.txt", "dlls"]
)
def test_ffmpeg_install_rejects_incomplete_shared_runtime(
    tmp_path: Path, missing: str,
) -> None:
    module = _builder()
    definition = module._load_ffmpeg_definition(Path(__file__).parents[1])
    archive = tmp_path / "ffmpeg.zip"
    _ffmpeg_archive(archive, definition, omit=missing)
    destination = tmp_path / "installed"

    with pytest.raises(RuntimeError, match="必要文件"):
        module._install_ffmpeg(archive, destination, definition)

    assert not destination.exists()


@pytest.mark.parametrize("cached", [False, True])
def test_offline_archive_rejects_missing_or_tampered_cache_without_network(
    tmp_path: Path, monkeypatch, cached: bool,
) -> None:
    module = _builder()
    archive = tmp_path / "ffmpeg.zip"
    if cached:
        archive.write_bytes(b"corrupted cache")

    def unexpected_download(*_args, **_kwargs):
        pytest.fail("offline archive validation must never access the network")

    monkeypatch.setattr(module.urllib.request, "urlretrieve", unexpected_download)
    with pytest.raises(RuntimeError, match="离线"):
        module._download_verified_archive(
            archive,
            url="https://example.invalid/ffmpeg.zip",
            expected_sha256=hashlib.sha256(b"verified archive").hexdigest(),
            display_name="FFmpeg",
            offline=True,
        )

    assert archive.exists() is cached
    if cached:
        assert archive.read_bytes() == b"corrupted cache"


@pytest.mark.parametrize("offline", [False, True])
def test_verified_archive_cache_is_reused_without_network(
    tmp_path: Path, monkeypatch, offline: bool,
) -> None:
    module = _builder()
    archive = tmp_path / "ffmpeg.zip"
    content = b"verified archive"
    archive.write_bytes(content)

    def unexpected_download(*_args, **_kwargs):
        pytest.fail("a verified cache must not be downloaded again")

    monkeypatch.setattr(module.urllib.request, "urlretrieve", unexpected_download)
    module._download_verified_archive(
        archive,
        url="https://example.invalid/ffmpeg.zip",
        expected_sha256=hashlib.sha256(content).hexdigest(),
        display_name="FFmpeg",
        offline=offline,
    )

    assert archive.read_bytes() == content


def test_failed_archive_download_preserves_cache_and_removes_partial_file(
    tmp_path: Path, monkeypatch,
) -> None:
    module = _builder()
    archive = tmp_path / "ffmpeg.zip"
    archive.write_bytes(b"previous cache")
    monkeypatch.setattr(
        module.urllib.request,
        "urlretrieve",
        lambda _url, target: Path(target).write_bytes(b"bad download"),
    )

    with pytest.raises(RuntimeError, match="哈希错误"):
        module._download_verified_archive(
            archive,
            url="https://example.invalid/ffmpeg.zip",
            expected_sha256=hashlib.sha256(b"verified archive").hexdigest(),
            display_name="FFmpeg",
            offline=False,
        )

    assert archive.read_bytes() == b"previous cache"
    assert list(tmp_path.iterdir()) == [archive]


def test_runtime_lock_is_exact_and_has_no_editable_sources() -> None:
    root = Path(__file__).parents[1]
    lines = [
        line.strip()
        for line in (root / "release" / "runtime-requirements.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert len(lines) >= 200
    assert all("==" in line for line in lines)
    assert all("git+" not in line and " -e " not in f" {line} " for line in lines)


def test_blessed_wheelhouse_manifest_matches_runtime_lock_size() -> None:
    import json

    root = Path(__file__).parents[1]
    payload = json.loads(
        (root / "release" / "runtime-wheelhouse.json").read_text(encoding="utf-8")
    )
    assert payload["wheel_count"] >= 200
    assert payload["total_bytes"] > 0
    assert len(payload["aggregate_sha256"]) == 64


def test_download_wheels_reuses_a_verified_wheelhouse(
    tmp_path: Path, monkeypatch
) -> None:
    module = _builder()
    wheelhouse = tmp_path / "wheels"
    wheelhouse.mkdir()
    (wheelhouse / "package-1.0-py3-none-any.whl").write_bytes(b"wheel")
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_verify_wheelhouse", lambda *_args: {"ok": True})
    monkeypatch.setattr(
        module,
        "_run",
        lambda argv, **_kwargs: commands.append(argv),
    )

    module._download_wheels(
        Path("python.exe"),
        tmp_path / "requirements.lock",
        wheelhouse,
        offline=False,
        commercial_root=tmp_path,
    )

    assert commands == []
