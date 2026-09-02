import importlib.util
from pathlib import Path


def _builder():
    script = Path(__file__).parents[1] / "scripts" / "build_manager.py"
    spec = importlib.util.spec_from_file_location("build_manager", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_tool_lock_is_exact() -> None:
    root = Path(__file__).parents[1]
    lines = [
        line.strip()
        for line in (root / "release" / "build-requirements.lock")
        .read_text(encoding="utf-8")
        .splitlines()
        if line.strip() and not line.startswith("#")
    ]
    assert "pyinstaller==6.22.2" in lines
    assert "pywebview==6.2.1" in lines
    assert "pythonnet==3.1.0" in lines
    assert all("==" in line for line in lines)


def test_sha256_is_uppercase(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"manager")
    digest = _builder()._sha256(source)
    assert len(digest) == 64
    assert digest == digest.upper()
