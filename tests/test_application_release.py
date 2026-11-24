"""Exercise release assembly without building executables or publishing assets."""
import json
from pathlib import Path
from types import SimpleNamespace
import zipfile

import pytest
from cryptography.hazmat.primitives import serialization

from test_runtime_distribution import environment_factory  # noqa: F401
from test_payload import _payload_files


def test_application_builder_reuses_environment_without_scanning_or_rebuilding_it(environment_factory, tmp_path, monkeypatch):
    import build_application_release as builder
    import build_update_release
    import publish_github_release as publisher
    from build_runtime_packages import environment_inputs
    from wechatrebot_manager.json_io import write_json_atomic
    from wechatrebot_manager.payload import PAYLOAD_MANIFEST, verify_payload, sha256_file
    from wechatrebot_manager.runtime_manifest import parse_environment

    root = tmp_path / "project"
    (root / "release").mkdir(parents=True)
    for name in ("runtime-requirements.lock", "runtime-wheelhouse.json", "ffmpeg-runtime.json"):
        (root / "release" / name).write_text("fixed inputs")
    env, _, _ = environment_factory()
    manifest = {**env.manifest, "inputs": environment_inputs(root)}
    build_update_release._sign_manifest(manifest, environment_factory.private)
    env = parse_environment(manifest, environment_factory.channel)
    previous = root / "environment.json"
    write_json_atomic(previous, env.manifest)
    private_path = root / "test-private.pem"
    private_path.write_bytes(environment_factory.private.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    (root / "pyproject.toml").write_text('[project]\nversion="1.4.0"\n')
    output = root / "artifacts/release"
    manager = root / "manager.exe"
    manager.write_bytes(b"synthetic executable")
    monkeypatch.setattr(builder, "_manager_executable", lambda *_: manager)
    monkeypatch.setattr(builder, "build_environment", lambda *_a, **_k: pytest.fail("must not scan runtime"))

    def staging(arguments):
        path = Path(arguments[arguments.index("--output") + 1])
        _payload_files(path, {"manager/WeChatReBot.exe": manager.read_bytes(), "legal/notice.txt": b"notice",
                              "app/plugin.py": b"application"})
        value = json.loads((path / PAYLOAD_MANIFEST).read_text())
        value["release_version"] = "1.4.0"
        write_json_atomic(path / PAYLOAD_MANIFEST, value)

    def engine(_root, path, _environment, **kwargs):
        (path / "astrbot").mkdir(parents=True)
        (path / "astrbot/__init__.py").write_text("# current application engine")

    def customer(path, **kwargs):
        verify_payload(path / "payload.wrb")
        archive = output / "customer.zip"
        archive.write_bytes(b"synthetic customer wrapper")
        checksum = output / "customer.zip.sha256"
        checksum.write_text(sha256_file(archive))
        return SimpleNamespace(archive_path=archive, checksum_path=checksum, payload_path=path / "payload.wrb")

    monkeypatch.setattr(builder.build_staging, "main", staging)
    monkeypatch.setattr(builder, "build_engine", engine)
    monkeypatch.setattr(builder.full, "_native_customer_package_from_output", customer)
    monkeypatch.setattr(build_update_release, "COMMERCIAL_ROOT", root)
    args = SimpleNamespace(private_key=private_path, environment_manifest=previous, runtime_root=None,
        refresh_environment=False, manager_build=None, source_root=root / "vendor", wheelhouse=None,
        offline=True, build=None, mandatory=False, note=["test"], title="application", minimum_supported_version="0.7.0")
    release, assets = builder.build_application(args, root=root, output=output, version="1.4.0",
        channel=environment_factory.channel, prefix="https://github.com/example/test/releases/download/v1.4.0", live_version="1.1.0")
    assert not any(p.name.startswith("runtime-") and p.suffix == ".zip" for p in assets)
    stable = json.loads(release.manifest_path.read_text())
    assert stable["environment_size"] == sum(p.size for p in env.packages)
    with zipfile.ZipFile(release.archive_path) as bundle:
        assert all(not name.startswith("payload/runtime/") for name in bundle.namelist())
        assert "payload/app/engine/python/astrbot/__init__.py" in bundle.namelist()
    publisher._verify_runtime_assets(stable, assets, channel=environment_factory.channel, remote=False)
    verified_urls = []
    monkeypatch.setattr(publisher, "verify_public_asset", lambda url, **kw: verified_urls.append(url))
    publisher._verify_runtime_assets(stable, assets, channel=environment_factory.channel, remote=True)
    assert set(p.url for p in env.packages).issubset(verified_urls)
    publisher._save_candidate(output, release, assets, repo="example/test", target="a" * 40,
                              title="application", notes=(), previous=None)
    restored, restored_release, restored_assets = publisher._load_candidate(
        output, repo="example/test", version="1.4.0", channel=environment_factory.channel)
    assert restored_release.package_sha256 == release.package_sha256
    assert restored_assets == assets
    assert restored["target"] == "a" * 40
    (root / "release/runtime-requirements.lock").write_text("new dependency")
    with pytest.raises(ValueError, match="依赖锁不一致"):
        builder.build_application(args, root=root, output=root / "artifacts/next", version="1.4.0",
            channel=environment_factory.channel, prefix="https://github.com/example/test/releases/download/v1.4.0", live_version="1.1.0")


def test_engine_rebuild_uses_fixed_wheel_current_patch_and_auth_bundle(tmp_path, monkeypatch):
    import build_application_release as builder
    from wechatrebot_manager.payload import sha256_file
    root = tmp_path / "root"
    wheelhouse = root / "wheels"
    wheelhouse.mkdir(parents=True)
    wheel = wheelhouse / "astrbot-4.26.6-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("astrbot/core/platform/__init__.py", "# original\n")
    helper = root / "src/wechatrebot_manager/group_feature_runtime.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("# current helper\n")
    seen = []
    monkeypatch.setattr(builder.build_runtime, "_apply_astrbot_patch", lambda r, p: seen.append((r, p)))
    monkeypatch.setattr(builder, "install_runtime_bundle", lambda r, p: seen.append((r, p)))
    env = SimpleNamespace(manifest={"engine_wheel": {"filename": wheel.name,
                                                   "size": wheel.stat().st_size, "sha256": sha256_file(wheel)}})
    output = root / "app/engine/python"
    builder.build_engine(root, output, env, wheelhouse=wheelhouse, offline=True)
    assert seen == [(root, output), (root, output)]
    assert (output / "astrbot/core/platform/wechat_group_policy.py").read_bytes() == helper.read_bytes()
    wheel.write_bytes(b"changed")
    with pytest.raises(ValueError, match="校验失败"):
        builder.build_engine(root, root / "other", env, wheelhouse=wheelhouse, offline=True)


@pytest.mark.parametrize("missing_environment", [False, True])
def test_application_publisher_promotes_latest_only_after_environment_and_smoke(tmp_path, monkeypatch, missing_environment):
    from test_publish_github_release import _module, _common_main_patches, _fake_build, _move_build, REPO
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    built = _fake_build(root, module, "0.9.9", module.asset_prefix(REPO, "v0.9.9"))
    events = []

    def build(args, **kwargs):
        assert args.application and not args.component_only
        release, customer = _move_build(built, kwargs["output"])
        release.manifest_path.write_text(json.dumps({"package": {}, "runtime_environment": {"id": "test"}}))
        environment = kwargs["output"] / "runtime-environment.json"
        environment.write_text('{"id":"test"}')
        return release, [release.archive_path, customer.archive_path, customer.payload_path, environment]

    def publish(*args, **kwargs):
        assert kwargs["latest"] is False
        events.append("upload")

    def verify(*args, **kwargs):
        if kwargs["remote"]:
            events.append("environment")
            if missing_environment:
                raise ValueError("historical asset unavailable")

    monkeypatch.setattr(module, "_build_candidate", build)
    monkeypatch.setattr(module, "_publish_assets", publish)
    monkeypatch.setattr(module, "_verify_runtime_assets", verify)
    monkeypatch.setattr(module, "_finish_publication", lambda *a, **k: events.append("stable_and_smoke"))
    monkeypatch.setattr(module, "publish_release", lambda *a, **k: events.append("latest") if k["latest"] else None)
    monkeypatch.setattr(module, "_cache_component_baselines", lambda *a: pytest.fail("application has no component baseline"))
    command = ["--output", str(root / "artifacts/publish"), "--yes"]
    if missing_environment:
        with pytest.raises(ValueError, match="historical asset"):
            module.main(command)
        assert events == ["upload", "environment"]
    else:
        assert module.main(command) == 0
        assert events == ["upload", "environment", "stable_and_smoke", "latest"]
        assert json.loads((root / "artifacts/runtime-environment/latest.json").read_text()) == {"id": "test"}
