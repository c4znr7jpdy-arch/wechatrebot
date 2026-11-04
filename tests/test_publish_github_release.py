"""GitHub Release publisher: naming, gh wrapper, verification, promotion, rollback."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


def _module():
    script = Path(__file__).parents[1] / "scripts" / "publish_github_release.py"
    spec = importlib.util.spec_from_file_location("publish_github_release", script)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


REPO = "c4znr7jpdy-arch/wechatrebot"


def test_release_tag_and_asset_urls_follow_github_download_layout() -> None:
    module = _module()
    assert module.release_tag("0.9.9") == "v0.9.9"
    assert module.release_tag("0.10.0.post1") == "v0.10.0.post1"
    assert module.asset_prefix(REPO, "v0.9.9") == f"https://github.com/{REPO}/releases/download/v0.9.9"
    assert module.asset_url(REPO, "v0.9.9", "payload.wrb") == (
        f"https://github.com/{REPO}/releases/download/v0.9.9/payload.wrb"
    )
    assert module.stable_manifest_url(REPO) == f"https://github.com/{REPO}/releases/download/stable/stable.json"
    with pytest.raises(ValueError):
        module.release_tag("not-a-version")
    with pytest.raises(ValueError):
        module.asset_prefix("owner only", "v1")


import hashlib
import io
import urllib.error


class _Response(io.BytesIO):
    def __init__(self, body: bytes, *, final_url: str, status: int = 200, length: int | None = None) -> None:
        super().__init__(body)
        self.status = status
        self._final_url = final_url
        self.headers = {"Content-Length": str(len(body) if length is None else length)}

    def geturl(self) -> str:
        return self._final_url

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def test_public_asset_check_follows_redirect_and_compares_size_and_sha256(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    final = "https://objects.githubusercontent.com/github-production-release-asset/1?X-Amz-Signature=abc"
    seen: list[str] = []

    def urlopen(request, *, timeout):
        seen.append(request.get_method())
        return _Response(body if request.get_method() == "GET" else b"", final_url=final, length=len(body))

    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    assert module.verify_public_asset(
        url, expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(), timeout=20,
    ) == final
    assert seen == ["HEAD", "GET"]


def test_public_asset_check_rejects_http_redirect_wrong_size_and_bad_digest(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    digest = hashlib.sha256(body).hexdigest()

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url="http://insecure.example/asset.zip"))
    with pytest.raises(ValueError, match="HTTPS"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256=digest, timeout=20, attempts=1)

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url=url, length=len(body) + 1))
    with pytest.raises(ValueError, match="大小"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256=digest, timeout=20, attempts=1)

    monkeypatch.setattr(module.urllib.request, "urlopen",
                        lambda request, *, timeout: _Response(body, final_url=url))
    with pytest.raises(ValueError, match="SHA-256"):
        module.verify_public_asset(url, expected_size=len(body), expected_sha256="0" * 64, timeout=20, attempts=1)


def test_public_asset_check_retries_transient_404_then_succeeds(monkeypatch) -> None:
    module = _module()
    body = b"release asset"
    url = f"https://github.com/{REPO}/releases/download/v0.9.9/asset.zip"
    calls = {"n": 0}

    def urlopen(request, *, timeout):
        calls["n"] += 1
        if calls["n"] == 1:
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)
        return _Response(body if request.get_method() == "GET" else b"", final_url=url, length=len(body))

    slept: list[float] = []
    monkeypatch.setattr(module.urllib.request, "urlopen", urlopen)
    module.verify_public_asset(url, expected_size=len(body), expected_sha256=hashlib.sha256(body).hexdigest(),
                               timeout=20, attempts=3, delay=5.0, sleep=slept.append)
    assert slept == [5.0]


import subprocess


class _FakeGh:
    """Records gh argv and answers from a scripted table keyed by the subcommand prefix."""

    def __init__(self, answers: dict[tuple[str, ...], str | Exception] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.answers = answers or {}

    def __call__(self, argv: list[str]) -> str:
        self.calls.append(list(argv))
        for prefix, answer in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                if isinstance(answer, Exception):
                    raise answer
                return answer
        return ""


def test_gh_wrapper_runs_gh_and_returns_stdout(monkeypatch) -> None:
    module = _module()
    recorded: list[dict] = []

    def run(argv, **kwargs):
        recorded.append({"argv": argv, **kwargs})
        return subprocess.CompletedProcess(argv, 0, stdout='{"nameWithOwner":"o/r"}\n', stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module._gh(["repo", "view", "--json", "nameWithOwner"]) == '{"nameWithOwner":"o/r"}\n'
    assert recorded[0]["argv"][0] == "gh" and recorded[0]["check"] is True
    assert recorded[0]["capture_output"] is True and recorded[0]["encoding"] == "utf-8"
    assert module.repo_name_with_owner() == "o/r"


def test_release_lifecycle_uses_draft_then_publish_and_clobbers_assets(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    notes = tmp_path / "notes.md"
    notes.write_text("- 修复", encoding="utf-8")
    asset = tmp_path / "a.zip"
    asset.write_bytes(b"a")
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)

    assert module.release_exists(REPO, "v0.9.9") is False
    module.create_release(REPO, "v0.9.9", title="0.9.9", notes_file=notes, target="abc123", latest=True)
    module.upload_assets(REPO, "v0.9.9", [asset])
    module.publish_release(REPO, "v0.9.9")
    module.create_release(REPO, "stable", title="稳定清单", notes_file=notes, target="abc123", latest=False)

    assert gh.calls[1] == ["release", "create", "v0.9.9", "-R", REPO, "--draft", "--title", "0.9.9",
                           "--notes-file", str(notes), "--target", "abc123"]
    assert gh.calls[2] == ["release", "upload", "v0.9.9", str(asset), "-R", REPO, "--clobber"]
    assert gh.calls[3] == ["release", "edit", "v0.9.9", "-R", REPO, "--draft=false"]
    assert "--latest=false" in gh.calls[4]


def test_release_exists_true_when_view_succeeds(monkeypatch) -> None:
    module = _module()
    gh = _FakeGh({("release", "view", "stable"): '{"tagName":"stable"}'})
    monkeypatch.setattr(module, "_gh", gh)
    assert module.release_exists(REPO, "stable") is True


def test_download_asset_places_file_in_directory(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    gh = _FakeGh()
    monkeypatch.setattr(module, "_gh", gh)
    target = module.download_asset(REPO, "v0.9.8", "stable-0.9.8.json", tmp_path)
    assert target == tmp_path / "stable-0.9.8.json"
    assert gh.calls[0] == ["release", "download", "v0.9.8", "-R", REPO, "--pattern", "stable-0.9.8.json",
                           "--dir", str(tmp_path), "--clobber"]


import base64
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from wechatrebot_manager.updates import UpdateChannel, canonical_manifest_bytes


def _signed_manifest(version: str, key: Ed25519PrivateKey, key_id: str, package_url: str) -> dict:
    manifest = {
        "schema_version": 1, "product": "wechatrebot-desktop", "channel": "stable", "version": version,
        "minimum_supported_version": "0.7.0", "published_at": "2026-11-03T00:00:00+00:00",
        "title": f"版本 {version}", "notes": [], "mandatory": False,
        "package": {"url": package_url, "format": "zip", "size": 3, "sha256": "A" * 64,
                    "payload_file_count": 1, "uncompressed_size": 3},
    }
    signature = key.sign(canonical_manifest_bytes(manifest))
    manifest["signature"] = {"algorithm": "ed25519", "key_id": key_id,
                             "value": base64.b64encode(signature).decode("ascii")}
    return manifest


def _channel(key: Ed25519PrivateKey) -> tuple[UpdateChannel, str]:
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    return UpdateChannel(manifest_url=f"https://github.com/{REPO}/releases/download/stable/stable.json",
                         channel="stable", key_id=key_id, public_key=public), key_id


def test_fetch_stable_manifest_returns_none_on_404(monkeypatch) -> None:
    module = _module()
    channel, _ = _channel(Ed25519PrivateKey.generate())

    def missing(_channel):
        raise urllib.error.HTTPError(channel.manifest_url, 404, "Not Found", {}, None)

    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", missing)
    assert module.fetch_stable_manifest(channel) is None


def test_promote_manifest_uploads_version_copy_then_overwrites_stable(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    manifest = _signed_manifest("0.9.9", key, key_id, module.asset_url(REPO, "v0.9.9", "u.zip"))
    manifest_path = tmp_path / "update" / "stable.json"
    manifest_path.parent.mkdir()
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    gh = _FakeGh({("release", "view", "stable"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: manifest)

    module.promote_manifest(REPO, "0.9.9", manifest_path, channel=channel, target="abc123",
                            work_root=tmp_path / "work", sleep=lambda _s: None)

    uploads = [c for c in gh.calls if c[:2] == ["release", "upload"]]
    assert uploads[0][2] == "v0.9.9" and uploads[0][3].endswith("stable-0.9.9.json")
    assert any(c[:3] == ["release", "create", "stable"] and "--latest=false" in c for c in gh.calls)
    assert uploads[1][2] == "stable" and uploads[1][3].endswith("stable.json") and "--clobber" in uploads[1]
    assert json.loads((tmp_path / "work" / "stable-0.9.9.json").read_text("utf-8")) == manifest


def test_promote_manifest_fails_when_public_manifest_differs(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    manifest = _signed_manifest("0.9.9", key, key_id, module.asset_url(REPO, "v0.9.9", "u.zip"))
    other = _signed_manifest("0.9.8", key, key_id, module.asset_url(REPO, "v0.9.8", "u.zip"))
    manifest_path = tmp_path / "stable.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(module, "_gh", _FakeGh({("release", "view", "stable"): "{}"}))
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: other)
    with pytest.raises(ValueError, match="稳定清单"):
        module.promote_manifest(REPO, "0.9.9", manifest_path, channel=channel, target="abc123",
                                work_root=tmp_path / "work", attempts=2, sleep=lambda _s: None)


def test_rollback_stable_downloads_verifies_signature_and_republishes(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    previous = _signed_manifest("0.9.8", key, key_id, module.asset_url(REPO, "v0.9.8", "u.zip"))

    def download(repo, tag, name, directory):
        assert (repo, tag, name) == (REPO, "v0.9.8", "stable-0.9.8.json")
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(previous), encoding="utf-8")
        return path

    gh = _FakeGh({("release", "view", "stable"): "{}"})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "download_asset", download)
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: previous)
    module.rollback_stable(REPO, "0.9.8", channel=channel, work_root=tmp_path / "work", sleep=lambda _s: None)
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    assert upload[2] == "stable" and upload[3].endswith("stable.json")

    tampered = dict(previous)
    tampered["title"] = "改过"

    def download_tampered(repo, tag, name, directory):
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / name
        path.write_text(json.dumps(tampered), encoding="utf-8")
        return path

    monkeypatch.setattr(module, "download_asset", download_tampered)
    with pytest.raises(ValueError, match="签名"):
        module.rollback_stable(REPO, "0.9.8", channel=channel, work_root=tmp_path / "work2", sleep=lambda _s: None)


def _fake_build(root: Path, module, version: str, prefix: str):
    out = root / "artifacts" / "built"
    update = out / "update"
    update.mkdir(parents=True)
    archive = update / f"wechatrebot-{version}-update.zip"
    archive.write_bytes(b"zip")
    manifest = update / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    customer = out / "customer.zip"
    customer.write_bytes(b"customer")
    checksum = customer.with_suffix(".zip.sha256")
    checksum.write_text("x", encoding="ascii")
    payload = out / f"WeChatReBot-{version}-payload-abc.wrb"
    payload.write_bytes(b"payload")
    release = module.full.BuiltFullUpdate(
        version=version, archive_path=archive, manifest_path=manifest,
        package_url=f"{prefix}/{archive.name}", package_size=3, package_sha256=hashlib.sha256(b"zip").hexdigest(),
    )
    package = module.full.BuiltCustomerPackage(
        version=version, archive_path=customer, checksum_path=checksum,
        package_url=f"{prefix}/{customer.name}", checksum_url=f"{prefix}/{checksum.name}",
        package_size=8, package_sha256=hashlib.sha256(b"customer").hexdigest(),
        installer={"kind": "inno-online"}, payload_path=payload, payload_url=f"{prefix}/{payload.name}",
    )
    return release, package


def _common_main_patches(module, monkeypatch, tmp_path: Path, *, live_version: str | None):
    root = tmp_path / "commercial"
    (root / "artifacts").mkdir(parents=True)
    (root / "config" / "local").mkdir(parents=True)
    (root / "config" / "local" / "update-signing-private.pem").write_bytes(b"key")
    (root / "vendor").mkdir()
    key = Ed25519PrivateKey.generate()
    channel, _ = _channel(key)
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", root)
    monkeypatch.setattr(module, "_project_version", lambda: "0.9.9")
    monkeypatch.setattr(module, "load_update_channel", lambda: channel)
    monkeypatch.setattr(module, "verify_source_inputs", lambda _root, source: source)
    monkeypatch.setattr(module.publisher, "_signing_key", lambda *_a: key)
    monkeypatch.setattr(module, "repo_name_with_owner", lambda: REPO)
    monkeypatch.setattr(module, "ensure_gh_ready", lambda _repo: None)
    monkeypatch.setattr(module, "_git_head", lambda: "abc123")
    monkeypatch.setattr(module, "fetch_stable_manifest",
                        lambda _c: None if live_version is None else {"version": live_version})
    return root, channel


def test_main_dry_run_builds_without_touching_github(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    built = _fake_build(root, module, "0.9.9", prefix)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda *_a, **_k: built)
    gh = _FakeGh()
    monkeypatch.setattr(module, "_gh", gh)
    assert module.main(["--dry-run", "--output", str(root / "artifacts" / "publish"), "--title", "t"]) == 0
    assert gh.calls == []


def test_main_full_publish_creates_release_uploads_verifies_and_promotes(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    built = _fake_build(root, module, "0.9.9", prefix)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda *_a, **_k: built)
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)
    verified: list[str] = []
    monkeypatch.setattr(module, "verify_public_asset", lambda url, **k: verified.append(url) or url)
    promoted: list[tuple] = []
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: promoted.append((a, k)))
    smoked: list[str] = []
    monkeypatch.setattr(module, "smoke_update_from_github",
                        lambda channel, release, root: smoked.append(release.version))

    assert module.main(["--output", str(root / "artifacts" / "publish"), "--title", "t", "--yes"]) == 0

    create = next(c for c in gh.calls if c[:2] == ["release", "create"])
    assert create[2] == "v0.9.9" and "--draft" in create
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    uploaded = [Path(p).name for p in upload[3:] if not p.startswith("-") and p != REPO]
    assert set(uploaded) >= {"wechatrebot-0.9.9-update.zip", "customer.zip", "customer.zip.sha256",
                             "WeChatReBot-0.9.9-payload-abc.wrb", "wechatrebot-0.9.9-update.zip.sha256",
                             "WeChatReBot-0.9.9-payload-abc.wrb.sha256"}
    assert len(uploaded) == len(set(uploaded))
    assert ["release", "edit", "v0.9.9", "-R", REPO, "--draft=false"] in gh.calls
    assert set(verified) == {built[0].package_url, built[1].package_url, built[1].payload_url}
    assert promoted and promoted[0][0][:2] == (REPO, "0.9.9")
    assert smoked == ["0.9.9"]


def test_main_refuses_version_not_above_live_stable(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, _ = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.9")
    with pytest.raises(SystemExit):
        module.main(["--output", str(root / "artifacts" / "publish"), "--yes"])


def test_main_manager_only_uses_manager_delta_builder(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    commands: list[list[str]] = []
    monkeypatch.setattr(module, "_run", lambda argv, **_k: commands.append(argv))
    update = root / "artifacts" / "built" / "update"
    update.mkdir(parents=True)
    archive = update / "wechatrebot-0.9.9-manager-abc.zip"
    archive.write_bytes(b"m")
    manifest = update / "stable.json"
    manifest.write_text("{}", encoding="utf-8")
    delta = module.publisher.BuiltManagerUpdate(version="0.9.9", archive_path=archive, manifest_path=manifest,
                                                package_url=f"{prefix}/{archive.name}", package_size=1,
                                                package_sha256=hashlib.sha256(b"m").hexdigest())
    seen: dict = {}
    monkeypatch.setattr(module.publisher, "build_manager_update", lambda **k: seen.update(k) or delta)
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh")})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "verify_public_asset", lambda url, **k: url)
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: None)
    monkeypatch.setattr(module, "smoke_update_from_github", lambda *a, **k: None)

    assert module.main(["--manager-only", "--output", str(root / "artifacts" / "publish"), "--yes",
                        "--skip-tests"]) == 0
    assert Path(commands[0][1]).name == "build_manager.py"
    assert seen["asset_prefix"] == prefix
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    assert any(p.endswith("wechatrebot-0.9.9-manager-abc.zip") for p in upload)


def test_main_manager_only_refuses_legacy_live_base(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, _ = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.8.6")
    with pytest.raises(SystemExit):
        module.main(["--manager-only", "--output", str(root / "artifacts" / "publish"), "--yes", "--skip-tests"])


def test_main_rollback_calls_rollback_stable(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.9")
    calls: list[tuple] = []
    monkeypatch.setattr(module, "rollback_stable", lambda repo, version, **k: calls.append((repo, version)))
    assert module.main(["--rollback", "0.9.8", "--yes"]) == 0
    assert calls == [(REPO, "0.9.8")]


def test_powershell_entry_forwards_all_arguments() -> None:
    script = (Path(__file__).parents[1] / "scripts" / "publish-github-release.ps1").read_text(encoding="utf-8")
    assert "publish_github_release.py" in script and "@args" in script and "py -3.13" in script
