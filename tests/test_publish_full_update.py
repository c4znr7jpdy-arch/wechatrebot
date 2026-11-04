from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path
import sys
import zipfile

from wechatrebot_manager.payload import sha256_file

from wechatrebot_manager.updates import UpdateChannel


def _publisher():
    script = Path(__file__).parents[1] / "scripts" / "publish_full_update.py"
    spec = importlib.util.spec_from_file_location("publish_full_update", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_build_full_update_artifacts_runs_the_build_pipeline_in_order(
    tmp_path: Path, monkeypatch, vendor_inputs
) -> None:
    module = _publisher()
    root = tmp_path / "commercial"
    scripts = root / "scripts"
    source = vendor_inputs(root)
    (root / "artifacts").mkdir(parents=True)
    scripts.mkdir()
    (root / "src").mkdir()
    channel = UpdateChannel(manifest_url="https://github.com/o/r/releases/download/stable/stable.json",
                            channel="stable", key_id="test", public_key=b"0" * 32)
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module, "SCRIPTS_ROOT", scripts)
    monkeypatch.setattr(module, "SOURCE_ROOT", root / "src")
    monkeypatch.setattr(module, "_run", lambda argv, **_kwargs: commands.append(argv))
    release = module.BuiltFullUpdate(
        version="0.8.7", archive_path=root / "artifacts" / "release.zip",
        manifest_path=root / "artifacts" / "stable.json",
        package_url="https://github.com/o/r/releases/download/v0.8.7/wechatrebot-0.8.7-update.zip",
        package_size=123, package_sha256="A" * 64,
    )
    customer = module.BuiltCustomerPackage(
        version="0.8.7", archive_path=root / "artifacts" / "customer.zip",
        checksum_path=root / "artifacts" / "customer.zip.sha256",
        package_url="https://github.com/o/r/releases/download/v0.8.7/customer.zip",
        checksum_url="https://github.com/o/r/releases/download/v0.8.7/customer.zip.sha256",
        package_size=8, package_sha256="B" * 64,
    )
    monkeypatch.setattr(module, "_release_from_output", lambda *_a, **_k: release)
    monkeypatch.setattr(module, "_customer_package_from_output", lambda *_a, **_k: customer)
    options = module.FullBuildOptions(
        source_root=source, private_key=root / "key.pem", output=root / "artifacts" / "publish",
        title="升级修复", notes=("修复更新回滚",), minimum_supported_version="0.7.0",
        build=None, mandatory=False, wheelhouse=None, offline=False,
        skip_tests=False, resume=False, refresh_manager=False,
    )

    built_release, built_customer = module.build_full_update_artifacts(
        options, version="0.8.7", channel=channel, previous_version="0.8.6",
        package_url=release.package_url, asset_prefix="https://github.com/o/r/releases/download/v0.8.7",
    )

    assert (built_release, built_customer) == (release, customer)
    invoked = ["pytest" if c[1:3] == ["-m", "pytest"] else Path(c[1]).name for c in commands if len(c) > 1]
    assert invoked == ["pytest", "validate_release_metadata.py", "build_manager.py", "build_runtime.py",
                       "build_legal_inventory.py", "validate_release_metadata.py", "build_staging.py",
                       "build_update_release.py", "build_distribution.py"]
    update_release = next(c for c in commands if "build_update_release.py" in c[1])
    assert update_release[update_release.index("--package-url") + 1] == release.package_url
    assert "--include" not in update_release


def test_customer_package_is_content_addressed_under_the_asset_prefix(tmp_path: Path) -> None:
    module = _publisher()
    package_root = tmp_path / "WeChatReBot-0.8.7-Windows-x64"
    original_archive = package_root.parent / f"{package_root.name}.zip"
    with zipfile.ZipFile(original_archive, "w") as archive:
        for name in ("WeChatReBot.exe", "payload.wrb", "DISTRIBUTION-MANIFEST.json", "START-HERE.txt"):
            archive.writestr(f"{package_root.name}/{name}", name)
    digest = sha256_file(original_archive)
    original_archive.with_name(original_archive.name + ".sha256").write_text(
        f"{digest}  {original_archive.name}\n", encoding="ascii")
    prefix = "https://github.com/o/r/releases/download/v0.8.7"

    package = module._customer_package_from_output(package_root, version="0.8.7", asset_prefix=prefix)

    assert digest[:12].lower() in package.archive_path.name
    assert package.package_url == f"{prefix}/{package.archive_path.name}"
    assert package.checksum_url == f"{prefix}/{package.checksum_path.name}"
    assert package.checksum_path.read_text(encoding="ascii").strip() == f"{digest}  {package.archive_path.name}"
    assert module._customer_package_from_output(package_root, version="0.8.7", asset_prefix=prefix) == package


def test_native_customer_package_uses_standard_setup_and_pinned_payload(
    tmp_path: Path, monkeypatch
) -> None:
    module = _publisher()
    package_root = tmp_path / "distribution" / "WeChatReBot-0.9.8-Windows-x64"
    package_root.mkdir(parents=True)
    manager = package_root / "WeChatReBot.exe"
    manager.write_bytes(b"MZmanager")
    payload = package_root / "payload.wrb"
    with zipfile.ZipFile(payload, "w") as archive:
        archive.writestr("manager/WeChatReBot.exe", manager.read_bytes())
        archive.writestr(
            "release-manifest.json",
            json.dumps(
                {
                    "release_version": "0.9.8",
                    "files": [
                        {
                            "path": "manager/WeChatReBot.exe",
                            "sha256": hashlib.sha256(manager.read_bytes()).hexdigest(),
                        }
                    ]
                }
            ),
        )
    output = tmp_path / "artifacts" / "publish"

    commands: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        commands.append(argv)
        setup_root = Path(argv[argv.index("--output") + 1])
        setup_root.mkdir(parents=True, exist_ok=True)
        (setup_root / "WeChatReBot-Setup-0.9.8.exe").write_bytes(b"MZsetup")

    monkeypatch.setattr(module, "_run", fake_run)
    package = module._customer_package_from_output(
        package_root,
        version="0.9.8",
        asset_prefix="https://github.com/o/r/releases/download/v0.9.8",
        native=True,
        manager_exe=manager,
        output_root=output,
    )

    assert package.installer is not None
    assert package.installer["kind"] == "inno-online"
    assert package.installer["entrypoint"] == "WeChatReBot-Setup-0.9.8.exe"
    assert package.installer["payload_url"] == package.payload_url
    assert package.payload_path is not None
    assert package.payload_path.name.startswith("WeChatReBot-0.9.8-payload-")
    assert package.payload_url in package.installer["payload_url"]
    prefix = "https://github.com/o/r/releases/download/v0.9.8"
    assert package.package_url == f"{prefix}/{package.archive_path.name}"
    assert package.checksum_url == f"{prefix}/{package.checksum_path.name}"
    assert package.payload_url == f"{prefix}/{package.payload_path.name}"
    assert Path(commands[0][1]).name == "build_native_installer.py"
    assert commands[0][commands[0].index("--payload-sha256") + 1] == package.installer["payload_sha256"]
    with zipfile.ZipFile(package.archive_path) as archive:
        assert set(archive.namelist()) == {
            "WeChatReBot-Setup-0.9.8.exe",
            "安装说明.txt",
        }
