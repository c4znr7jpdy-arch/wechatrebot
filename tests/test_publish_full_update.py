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


def test_full_update_entry_runs_complete_build_and_publish_pipeline(
    tmp_path: Path, monkeypatch
) -> None:
    module = _publisher()
    root = tmp_path / "commercial"
    scripts = root / "scripts"
    source = tmp_path / "project"
    (root / "artifacts").mkdir(parents=True)
    scripts.mkdir()
    source.mkdir()
    (root / "src").mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname="test"\nversion="0.8.7"\n', encoding="utf-8"
    )
    key = root / "update-key.pem"
    key.write_text("test", encoding="utf-8")
    channel = UpdateChannel(
        manifest_url="https://updates.example.test/stable.json",
        channel="stable",
        key_id="test",
        public_key=b"0" * 32,
    )
    commands: list[list[str]] = []
    published: list[object] = []

    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module, "SCRIPTS_ROOT", scripts)
    monkeypatch.setattr(module, "SOURCE_ROOT", root / "src")
    monkeypatch.setattr(module, "_project_version", lambda: "0.8.7")
    monkeypatch.setattr(module, "load_update_channel", lambda: channel)
    monkeypatch.setattr(module.publisher, "_signing_key", lambda *_args: object())
    monkeypatch.setattr(
        module.publisher,
        "_fetch_live_manifest",
        lambda _channel: {"version": "0.8.6"},
    )
    monkeypatch.setattr(
        module.publisher,
        "_fetch_download_version",
        lambda _base_url: "0.8.6",
    )
    monkeypatch.setattr(module, "_run", lambda argv, **_kwargs: commands.append(argv))
    release = module.BuiltFullUpdate(
        version="0.8.7",
        archive_path=root / "artifacts" / "release.zip",
        manifest_path=root / "artifacts" / "stable.json",
        package_url="https://updates.example.test/releases/wechatrebot-0.8.7-update.zip",
        package_size=123,
        package_sha256="A" * 64,
    )
    release.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    release.manifest_path.write_text(
        json.dumps({"published_at": "2026-09-04T02:30:00+00:00"}),
        encoding="utf-8",
    )
    customer_archive = root / "artifacts" / "customer.zip"
    customer_archive.write_bytes(b"customer")
    customer_checksum = customer_archive.with_suffix(".zip.sha256")
    customer_checksum.write_text("checksum", encoding="ascii")
    customer = module.BuiltCustomerPackage(
        version="0.8.7",
        archive_path=customer_archive,
        checksum_path=customer_checksum,
        package_url="https://updates.example.test/downloads/customer.zip",
        checksum_url="https://updates.example.test/downloads/customer.zip.sha256",
        package_size=8,
        package_sha256="B" * 64,
    )
    monkeypatch.setattr(module, "_release_from_output", lambda *_args, **_kwargs: release)
    monkeypatch.setattr(
        module,
        "_customer_package_from_output",
        lambda *_args, **_kwargs: customer,
    )
    staged = module.StagedDownloadSite("pending", "rollback", "nonce")
    monkeypatch.setattr(module, "_stage_download_site", lambda *_args, **_kwargs: staged)
    monkeypatch.setattr(
        module.publisher,
        "_publish",
        lambda value, **_kwargs: published.append(value),
    )

    assert module.main(
        [
            "--source-root",
            str(source),
            "--private-key",
            str(key),
            "--base-url",
            "https://updates.example.test",
            "--title",
            "升级修复",
            "--note",
            "修复更新回滚",
            "--yes",
        ]
    ) == 0

    invoked = [
        "pytest" if command[1:3] == ["-m", "pytest"] else Path(command[1]).name
        for command in commands
        if len(command) > 1
    ]
    assert invoked == [
        "pytest",
        "validate_release_metadata.py",
        "build_manager.py",
        "build_runtime.py",
        "build_legal_inventory.py",
        "validate_release_metadata.py",
        "build_staging.py",
        "build_update_release.py",
        "build_distribution.py",
    ]
    assert "--include" not in commands[-2]
    inventory = next(command for command in commands if "build_legal_inventory.py" in command[1])
    assert inventory[inventory.index("--notices") + 1] == "legal/THIRD-PARTY-NOTICES.md"
    assert inventory[inventory.index("--licenses") + 1] == "legal/THIRD-PARTY-LICENSES.txt"
    assert "--archive" not in commands[-1]
    assert published == [release]


def test_customer_package_is_content_addressed_and_metadata_matches(
    tmp_path: Path,
) -> None:
    module = _publisher()
    package_root = tmp_path / "WeChatReBot-0.8.7-Windows-x64"
    original_archive = package_root.parent / f"{package_root.name}.zip"
    with zipfile.ZipFile(original_archive, "w") as archive:
        for name in (
            "WeChatReBot.exe",
            "payload.wrb",
            "DISTRIBUTION-MANIFEST.json",
            "START-HERE.txt",
        ):
            archive.writestr(f"{package_root.name}/{name}", name)
    digest = sha256_file(original_archive)
    original_archive.with_name(original_archive.name + ".sha256").write_text(
        f"{digest}  {original_archive.name}\n",
        encoding="ascii",
    )

    package = module._customer_package_from_output(
        package_root,
        version="0.8.7",
        base_url="https://updates.example.test/product",
    )
    metadata_path = module._write_download_metadata(
        tmp_path / "download.json",
        package=package,
        published_at="2026-09-04T02:30:00+00:00",
        title="手动下载安装",
        notes=("新增完整 ZIP 下载页",),
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))

    assert digest[:12].lower() in package.archive_path.name
    assert package.archive_path.name.endswith(".zip")
    assert package.checksum_path.read_text(encoding="ascii").strip() == (
        f"{digest}  {package.archive_path.name}"
    )
    assert metadata["version"] == "0.8.7"
    assert metadata["download"] == {
        "url": package.package_url,
        "filename": package.archive_path.name,
        "size": package.package_size,
        "sha256": package.package_sha256,
        "checksum_url": package.checksum_url,
    }

    resumed = module._customer_package_from_output(
        package_root,
        version="0.8.7",
        base_url="https://updates.example.test/product",
    )
    assert resumed == package


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
        base_url="https://download.example.test/wechatrebot-updates",
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
    assert Path(commands[0][1]).name == "build_native_installer.py"
    assert commands[0][commands[0].index("--payload-sha256") + 1] == package.installer["payload_sha256"]
    with zipfile.ZipFile(package.archive_path) as archive:
        assert set(archive.namelist()) == {
            "WeChatReBot-Setup-0.9.8.exe",
            "安装说明.txt",
        }

    metadata_path = module._write_download_metadata(
        tmp_path / "download.json",
        package=package,
        published_at="2026-09-20T06:00:00+00:00",
        title="标准安装器",
        notes=(),
    )
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    assert metadata["installer"] == package.installer


def test_full_update_powershell_entry_forwards_all_arguments() -> None:
    wrapper = (
        Path(__file__).parents[1] / "scripts" / "publish-full-update.ps1"
    ).read_text(encoding="utf-8")

    assert "& py -3.13 $publisher @args" in wrapper
    assert "publish_full_update.py" in wrapper


def test_oss_download_site_stage_does_not_copy_large_package_to_server(
    tmp_path: Path, monkeypatch
) -> None:
    module = _publisher()
    root = tmp_path / "commercial"
    page = root / "deploy" / "download-site" / "index.html"
    nginx = root / "deploy" / "nginx" / "wechatrebot-updates.conf"
    page.parent.mkdir(parents=True)
    nginx.parent.mkdir(parents=True)
    page.write_text('<a id="downloadLink"></a><script>fetch("./download.json")</script>', encoding="utf-8")
    nginx.write_text("/srv/wechatrebot-updates", encoding="utf-8")
    archive = tmp_path / "customer.zip"
    checksum = tmp_path / "customer.zip.sha256"
    metadata = tmp_path / "download.json"
    archive.write_bytes(b"package")
    checksum.write_text("checksum", encoding="ascii")
    metadata.write_text("{}", encoding="utf-8")
    package = module.BuiltCustomerPackage(
        version="0.8.8",
        archive_path=archive,
        checksum_path=checksum,
        package_url="https://download.example.test/downloads/customer.zip",
        checksum_url="https://download.example.test/downloads/customer.zip.sha256",
        package_size=archive.stat().st_size,
        package_sha256=sha256_file(archive),
    )
    copied: list[Path] = []
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module.publisher, "_ssh", lambda *_args: None)
    monkeypatch.setattr(
        module.publisher, "_scp", lambda local, *_args: copied.append(local)
    )
    monkeypatch.setattr(module, "_public_page", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(module, "_public_head", lambda *_args, **_kwargs: None)

    work = tmp_path / "work"
    work.mkdir()
    module._stage_download_site(
        package,
        metadata,
        host="root@example.test",
        remote_root="/srv/updates",
        base_url="https://updates.example.test",
        work_root=work,
        timeout=20,
        upload_package_to_server=False,
    )

    assert archive not in copied
    assert checksum not in copied
    assert page in copied
    assert metadata in copied
