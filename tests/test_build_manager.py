import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


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
    assert "tzdata==2026.4" in lines
    assert all("==" in line for line in lines)


def test_sha256_is_uppercase(tmp_path: Path) -> None:
    source = tmp_path / "file.bin"
    source.write_bytes(b"manager")
    digest = _builder()._sha256(source)
    assert len(digest) == 64
    assert digest == digest.upper()


def test_build_can_reuse_existing_toolchain(monkeypatch, tmp_path: Path) -> None:
    builder = _builder()
    builder.__file__ = str(tmp_path / "scripts/build_manager.py")
    monkeypatch.setattr(builder, "sys", SimpleNamespace(version_info=(3, 13), executable="python.exe"))
    monkeypatch.setattr(builder.platform, "architecture", lambda: ("64bit", ""))
    build_python = tmp_path / "cached-python.exe"
    build_python.write_bytes(b"MZ")
    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if "PyInstaller" in argv and "--distpath" in argv:
            dist = Path(argv[argv.index("--distpath") + 1])
            dist.mkdir(parents=True)
            (dist / "WeChatReBot.exe").write_bytes(b"MZ")
        elif "self-test" in argv:
            report = {
                "ok": True, "frozen": True, "architecture": 64,
                "manager_version": "test", "timezone_data": True,
            }
            Path(argv[argv.index("--output") + 1]).write_text(json.dumps(report), encoding="utf-8")

    monkeypatch.setattr(builder, "_run", fake_run)
    output = tmp_path / "artifacts" / "manager"
    assert builder.main([
        "--output", str(output), "--build-python", str(build_python),
    ]) == 0
    assert not any("pip" in command or "venv" in command for command in calls)
    assert any(command[:2] == [str(build_python), "-c"] for command in calls)


@pytest.mark.parametrize('timezone_ready', [True, False, None])
def test_build_collects_timezone_package_and_requires_frozen_self_test(
    monkeypatch, tmp_path, timezone_ready,
):
    builder = _builder()
    builder.__file__ = str(tmp_path / 'scripts/build_manager.py')
    monkeypatch.setattr(builder, 'sys', SimpleNamespace(version_info=(3, 13), executable='python.exe'))
    monkeypatch.setattr(builder.platform, 'architecture', lambda: ('64bit', ''))
    calls = []

    def fake_run(argv, **_kwargs):
        calls.append(argv)
        if 'PyInstaller' in argv:
            dist = Path(argv[argv.index('--distpath') + 1])
            dist.mkdir(parents=True)
            (dist / 'WeChatReBot.exe').write_bytes(b'test executable placeholder')
        elif 'self-test' in argv:
            report = {'ok': True, 'frozen': True, 'architecture': 64, 'manager_version': 'test'}
            if timezone_ready is not None:
                report['timezone_data'] = timezone_ready
            Path(argv[argv.index('--output') + 1]).write_text(json.dumps(report), encoding='utf-8')

    monkeypatch.setattr(builder, '_run', fake_run)
    output = tmp_path / 'artifacts/manager'
    if timezone_ready:
        assert builder.main(['--output', str(output)]) == 0
    else:
        with pytest.raises(RuntimeError, match='frozen manager self-test failed'):
            builder.main(['--output', str(output)])
        assert not (output / 'manager-build-manifest.json').exists()
    command = next(argv for argv in calls if 'PyInstaller' in argv)
    assert any(command[i:i+2] == ['--collect-all', 'tzdata'] for i in range(len(command)))
    root = Path(__file__).parents[1]
    import tomllib
    dependencies = tomllib.loads((root / 'pyproject.toml').read_text(encoding='utf-8'))['project']['dependencies']
    assert 'tzdata==2026.4' in dependencies
