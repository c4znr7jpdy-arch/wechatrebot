import importlib.util
import json
from pathlib import Path
import sys
import zipfile

import pytest


def builder():
    path = Path(__file__).parents[1] / "scripts" / "build_local_package.py"
    spec = importlib.util.spec_from_file_location("build_local_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_failed_stage_stops_and_keeps_diagnostic_log(tmp_path):
    module = builder()
    with pytest.raises(RuntimeError, match="exit 7"):
        module.run_step("broken", [sys.executable, "-c", "print('diagnostic'); exit(7)"], tmp_path)
    assert "diagnostic" in (tmp_path / "broken.log").read_text()


def test_local_plan_has_no_publish_or_service_commands(tmp_path):
    steps = builder().build_steps(tmp_path / "source with spaces", tmp_path / "output")
    assert [Path(command[1]).name for _, command in steps] == [
        "build_manager.py", "build_runtime.py", "build_staging.py",
        "build_distribution.py", "build_native_installer.py",
    ]
    installer = steps[-1][1]
    assert "--payload" in installer
    assert "--payload-url" not in installer
    assert str(tmp_path / "source with spaces") in steps[2][1]


def test_customer_zip_contains_native_installer_and_checksum(tmp_path):
    module = builder()
    installer = tmp_path / "installer" / "WeChatReBot-Setup-0.9.9.exe"
    installer.parent.mkdir()
    installer.write_bytes(b"test installer")
    archive = module.finish_package(tmp_path, "0.9.9")
    with zipfile.ZipFile(archive) as package:
        assert set(package.namelist()) == {installer.name, "安装说明.txt"}
        assert package.read(installer.name) == installer.read_bytes()
    result = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert result["uploaded"] is False
    assert result["services_started"] is False
    assert result["sha256"] in archive.with_suffix(".zip.sha256").read_text()
