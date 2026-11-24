"""Component publishing uses only exact-base artifacts and isolated smoke installs."""
import io
import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from test_publish_github_release import REPO, _channel, _module
from wechatrebot_manager.payload import install_payload, installed_payload_fingerprint, sha256_file


PLUGIN = "app/astrbot/data/plugins/fixture/main.py"


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _baseline(root, content, version):
    _write(root / PLUGIN, content)
    _write(root / "config/installed-payload.json", json.dumps({
        "schema_version": 1, "release_version": version,
        "files": [{"path": PLUGIN, "sha256": sha256_file(root / PLUGIN)}],
    }))
    return root


@pytest.fixture
def candidate(tmp_path, monkeypatch):
    module = _module()
    import prepare_component_update as preparation
    from build_incremental_update import build_incremental_update

    root = tmp_path / "repo"
    (root / "artifacts").mkdir(parents=True)
    (root / "vendor").mkdir()
    key = Ed25519PrivateKey.generate()
    channel, _ = _channel(key)
    key_path = root / "config/local/update-signing-private.pem"
    key_path.parent.mkdir(parents=True)
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                          serialization.NoEncryption()))
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module, "_project_version", lambda: "1.0.23")
    monkeypatch.setattr(module, "load_update_channel", lambda: channel)
    monkeypatch.setattr(module, "repo_name_with_owner", lambda: REPO)
    monkeypatch.setattr(module, "_git_head", lambda: "source-commit")
    monkeypatch.setattr(module, "verify_source_inputs", lambda _r, source: source)
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _c: None)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda *a, **k: pytest.fail("full build"))
    monkeypatch.setattr(module, "_run", lambda *a, **k: pytest.fail("runtime or manager build"))
    monkeypatch.setattr(module, "_gh", lambda *a, **k: pytest.fail("GitHub write"))

    def prepare(_root, _source, bases, output, *, version, build, manager_build):
        output.mkdir(parents=True)
        payloads, targets = [], []
        desired = output / "candidate"
        _write(desired / PLUGIN, "answer = 23\n")
        for index, base in enumerate(bases):
            payload = output / f"delta-{index}.zip"
            build_incremental_update(desired, base, payload, managed_roots=[PLUGIN], version=version, build=build)
            target = preparation.snapshot_baseline(base, output / f"target-{index}")
            install_payload(payload, target)
            payloads.append(payload)
            targets.append(target)
        return {"payloads": payloads, "target_baselines": targets}

    monkeypatch.setattr(preparation, "prepare_component_payloads", prepare)
    bases = [_baseline(tmp_path / f"base-{index}", f"answer = {index}\n", f"1.0.{21+index}")
             for index in range(2)]
    output = root / "artifacts/publish"
    args = ["--component-only", "--dry-run", "--output", str(output)]
    for base in bases:
        args += ["--baseline", str(base)]
    return module, channel, root, output, args


def test_explicit_component_mode_recovers_without_default_package(candidate):
    module, channel, root, output, args = candidate
    assert module.main(args) == 0
    record, release, assets = module._load_candidate(output, repo=REPO, version="1.0.23", channel=channel)
    manifest = json.loads(release.manifest_path.read_text("utf-8"))
    assert "package" not in manifest and manifest["component_update_protocol"] == 1
    assert len(assets) == len(manifest["incremental_packages"]) == 2
    assert record["component_context"]
    assert not list(output.rglob("*.wrb"))
    assert not (output / "runtime-build").exists()
    assert not (root / "artifacts/component-baselines").exists()


@pytest.mark.parametrize("changed", ["context", "inventory", "missing_snapshot"])
def test_resume_rejects_changed_component_baselines(candidate, changed):
    module, channel, _root, output, args = candidate
    module.main(args)
    context = json.loads((output / "component-context.json").read_text("utf-8"))
    source = output / context["baselines"][0]["source"]
    if changed == "context":
        _write(output / "component-context.json", "{}")
    elif changed == "inventory":
        _write(source / "config/installed-payload.json", '{"schema_version":1,"files":[]}')
    else:
        (source / "config/installed-payload.json").unlink()
    with pytest.raises((ValueError, RuntimeError)):
        module._load_candidate(output, repo=REPO, version="1.0.23", channel=channel)


def test_component_smoke_downloads_and_applies_every_base_before_caching(candidate, monkeypatch):
    module, channel, root, output, args = candidate
    module.main(args)
    _record, release, assets = module._load_candidate(output, repo=REPO, version="1.0.23", channel=channel)
    manifest = json.loads(release.manifest_path.read_text("utf-8"))
    packages = module.manifest_package_releases(manifest, channel)
    responses = {package.package_url: asset.read_bytes() for package, asset in zip(packages, assets)}
    downloads = []

    class Response(io.BytesIO):
        def __init__(self, url):
            super().__init__(responses[url])
            self.url = url
            self.headers = {"Content-Length": str(len(responses[url]))}

        def geturl(self):
            return self.url

    def download(request, **kwargs):
        downloads.append(request.full_url)
        return Response(request.full_url)

    import wechatrebot_manager.updates as updates
    monkeypatch.setattr(updates.urllib.request, "urlopen", download)
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _channel: manifest)
    module.smoke_update_from_github(channel, release, output / "github/smoke")
    module.smoke_update_from_github(channel, release, output / "github/smoke")
    assert set(downloads) == set(responses)
    context = json.loads((output / "component-context.json").read_text("utf-8"))
    runs = list((output / "github/smoke").iterdir())
    assert len(runs) == 2
    for run in runs:
        for index, entry in enumerate(context["baselines"]):
            assert installed_payload_fingerprint(run / str(index)) == entry["target_fingerprint"]
    module._cache_component_baselines(output, channel)
    assert len(list((root / "artifacts/component-baselines").iterdir())) == 1


def test_missing_baseline_never_falls_back_to_full(candidate):
    module, _channel, _root, _output, args = candidate
    with pytest.raises(ValueError, match="缺少发布基线"):
        module.main(args[:4])


def test_standalone_manager_update_keeps_next_component_base_available(candidate, tmp_path):
    from test_publish_manager_update import _manager_build

    module, channel, root, output, args = candidate
    base = _baseline(tmp_path / "manager-base", "answer = 1\n", "1.0.21")
    _write(base / "manager/WeChatReBot.exe", "old manager")
    inventory_path = base / "config/installed-payload.json"
    inventory = json.loads(inventory_path.read_text("utf-8"))
    inventory["files"].append({"path": "manager/WeChatReBot.exe",
                               "sha256": sha256_file(base / "manager/WeChatReBot.exe")})
    _write(inventory_path, json.dumps(inventory))
    cache = root / "artifacts/component-baselines"
    old = module._store_baseline(base, cache)
    manager_output = root / "artifacts/manager-release"
    module.publisher.build_manager_update(
        manager_build=_manager_build(tmp_path / "manager-build", "1.0.22"),
        output=manager_output / "update", version="1.0.22",
        private_key_path=root / "config/local/update-signing-private.pem", channel=channel,
        asset_prefix=module.asset_prefix(REPO, "v1.0.22"), title="manager", notes=(),
        minimum_supported_version="0.0.0",
    )
    module._cache_component_baselines(manager_output, channel)
    bases = list(cache.iterdir())
    assert len(bases) == 2 and old.exists()
    new = next(path for path in bases if path != old)
    assert (new / "manager/WeChatReBot.exe").read_bytes() == b"manager-update"
    assert module.main(args[:4]) == 0
    manifest = json.loads((output / "update/stable.json").read_text("utf-8"))
    selected = module.parse_update_manifest(manifest, channel, current_version="1.0.22", install_root=new)
    assert selected.base_fingerprint == installed_payload_fingerprint(new)


def test_plan_normalizes_full_staging_without_building_or_signing(candidate, tmp_path, monkeypatch, capsys):
    module, _channel, _root, _output, _args = candidate
    import update_local_incremental as local

    staging = _baseline(tmp_path / "full-staging", "answer = 1\n", "1.0.22")
    (staging / "config/installed-payload.json").rename(staging / "release-manifest.json")
    seen = []
    monkeypatch.setattr(local, "verify_base_inputs", lambda root, base: seen.append(base))
    monkeypatch.setattr(local, "source_scopes", lambda root, base: ([], [PLUGIN])
                        if (base / "config/installed-payload.json").is_file() else pytest.fail("unnormalized base"))
    monkeypatch.setattr(local, "manager_changed", lambda root, base: False)
    monkeypatch.setattr(module.publisher, "_signing_key", lambda *a: pytest.fail("signing key read"))
    assert module.main(["--plan", "--baseline", str(staging)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert seen and report["runtime_build_required"] is False
    assert report["baselines"][0]["baseline"] == str(staging.resolve())


def test_component_release_preserves_latest_installer_and_uses_component_notes(candidate, monkeypatch):
    module, channel, _root, output, args = candidate
    module.main(args)
    _record, release, assets = module._load_candidate(output, repo=REPO, version="1.0.23", channel=channel)
    versioned = output / "github/stable-1.0.23.json"
    versioned.parent.mkdir()
    versioned.write_bytes(release.manifest_path.read_bytes())
    monkeypatch.setattr(module, "release_exists", lambda *a: False)
    monkeypatch.setattr(module, "_verify_release_target", lambda *a, **k: None)
    monkeypatch.setattr(module, "verify_public_asset", lambda url, **k: url)
    calls = []
    monkeypatch.setattr(module, "_gh", lambda args: calls.append(args) or "")
    module._publish_assets(REPO, "v1.0.23", title="components", notes=(), version="1.0.23",
        assets=[*assets, versioned], work_root=versioned.parent, timeout=30, latest=False)
    assert all("--latest=false" in call for call in calls if call[:2] in (["release", "create"], ["release", "edit"]))
    notes = (versioned.parent / "release-notes.md").read_text("utf-8")
    assert "此 Release 只提供组件更新" in notes
    assert "下载 `WeChatReBot-*-online-*.zip`" not in notes
