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
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh", stderr="release not found")})
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
    gh = _FakeGh({("release", "view", "stable"): subprocess.CalledProcessError(1, "gh", stderr="release not found")})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module.publisher, "_fetch_live_manifest", lambda _c: manifest)

    monkeypatch.setattr(module, "verify_public_asset", lambda *a, **k: "https://github.com/verified")
    module.promote_manifest(REPO, "0.9.9", manifest_path, channel=channel, target="abc123",
                            work_root=tmp_path / "work", sleep=lambda _s: None)

    uploads = [c for c in gh.calls if c[:2] == ["release", "upload"]]
    assert any(c[:3] == ["release", "create", "stable"] and "--latest=false" in c for c in gh.calls)
    assert uploads[0][2] == "stable" and uploads[0][3].endswith("stable.json") and "--clobber" in uploads[0]
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
    monkeypatch.setattr(module, "verify_public_asset", lambda *a, **k: "https://github.com/verified")
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
    monkeypatch.setattr(module, "verify_public_asset", lambda *a, **k: "https://github.com/verified")
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


def _move_build(built, output):
    from dataclasses import replace
    import shutil
    output.mkdir(parents=True, exist_ok=True)
    moved = []
    for record in built:
        changes = {}
        for field in ("archive_path", "manifest_path", "checksum_path", "payload_path"):
            path = getattr(record, field, None)
            if path is not None:
                destination = output / path.name
                shutil.copyfile(path, destination)
                changes[field] = destination
        moved.append(replace(record, **changes))
    return tuple(moved)


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
    monkeypatch.setattr(module, "_verify_release_target", lambda *a, **k: None)
    monkeypatch.setattr(module, "fetch_stable_manifest",
                        lambda _c: None if live_version is None else {"version": live_version})
    return root, channel


def test_main_dry_run_builds_without_touching_github(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    built = _fake_build(root, module, "0.9.9", prefix)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda options, **_k: _move_build(built, options.output))
    gh = _FakeGh()
    monkeypatch.setattr(module, "_gh", gh)
    assert module.main(["--dry-run", "--output", str(root / "artifacts" / "publish"), "--title", "t"]) == 0
    assert gh.calls == []


def test_main_full_publish_creates_release_uploads_verifies_and_promotes(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, channel = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    prefix = module.asset_prefix(REPO, "v0.9.9")
    built = _fake_build(root, module, "0.9.9", prefix)
    monkeypatch.setattr(module.full, "build_full_update_artifacts", lambda options, **_k: _move_build(built, options.output))
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh", stderr="release not found")})
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
    assert set(verified) == {built[0].package_url, built[1].package_url, built[1].payload_url,
                             module.asset_url(REPO, "v0.9.9", "stable-0.9.9.json")}
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
    monkeypatch.setattr(module.publisher, "build_manager_update", lambda **k: seen.update(k) or _move_build((delta,), k["output"].parent)[0])
    gh = _FakeGh({("release", "view", "v0.9.9"): subprocess.CalledProcessError(1, "gh", stderr="release not found")})
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


def test_main_manager_only_rejects_component_incremental_payload(tmp_path: Path, monkeypatch) -> None:
    module = _module()
    root, _ = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    with pytest.raises(SystemExit):
        module.main([
            "--manager-only",
            "--incremental-payload",
            str(tmp_path / "component.zip"),
            "--output",
            str(root / "artifacts" / "publish"),
            "--yes",
            "--skip-tests",
        ])


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


@pytest.mark.parametrize("error", [TimeoutError("timeout"), ConnectionResetError("reset"),
                                  urllib.error.URLError(TimeoutError("wrapped timeout")),
                                  __import__("ssl").SSLEOFError("EOF"),
                                  __import__("http.client", fromlist=["IncompleteRead"]).IncompleteRead(b"part")])
def test_public_asset_restarts_complete_verification_after_transport_failure(monkeypatch, error):
    module = _module()
    body = b"asset"
    requests = []
    def request(req, **kwargs):
        requests.append(req.get_method())
        if len(requests) == 2:
            raise error
        return _Response(body, final_url=req.full_url)
    monkeypatch.setattr(module.urllib.request, "urlopen", request)
    module.verify_public_asset("https://github.com/asset", expected_size=len(body),
                               expected_sha256=hashlib.sha256(body).hexdigest(), timeout=1, sleep=lambda _: None)
    assert requests == ["HEAD", "GET", "HEAD", "GET"]


def test_integrity_and_certificate_failures_are_not_retried(monkeypatch):
    import ssl
    module = _module()
    sleeps = []
    def invalid(*a, **k):
        raise urllib.error.URLError(ssl.SSLCertVerificationError("untrusted"))
    monkeypatch.setattr(module.urllib.request, "urlopen", invalid)
    with pytest.raises(ValueError):
        module.verify_public_asset("https://github.com/a", expected_size=1, expected_sha256="0"*64,
                                   timeout=1, sleep=sleeps.append)
    assert sleeps == []


@pytest.mark.parametrize("message", ["HTTP 401 unauthorized", "HTTP 500 server error", "TLS timeout"])
def test_release_lookup_does_not_treat_network_or_auth_failure_as_missing(monkeypatch, message):
    module = _module()
    monkeypatch.setattr(module, "_gh", _FakeGh({("release", "view"): module.GitHubCommandError(message)}))
    with pytest.raises(module.GitHubCommandError):
        module.release_exists(REPO, "v1.0.1")


def test_gh_write_failure_has_redacted_diagnostic_and_is_not_retried(monkeypatch):
    module = _module()
    calls = []
    monkeypatch.setenv("GH_TOKEN", "super-secret")
    def run(argv, **kwargs):
        calls.append(argv)
        raise subprocess.CalledProcessError(1, argv, stderr="TLS EOF token=super-secret ghp_1234567890")
    monkeypatch.setattr(module.subprocess, "run", run)
    with pytest.raises(module.GitHubCommandError) as caught:
        module._gh(["release", "upload", "v1.0.1"])
    assert len(calls) == 1 and "TLS EOF" in str(caught.value)
    assert "super-secret" not in str(caught.value) and "ghp_1234567890" not in str(caught.value)


def test_existing_draft_stable_release_is_published(monkeypatch, tmp_path):
    module = _module()
    gh = _FakeGh({("release", "view"): '{"isDraft":true}'})
    monkeypatch.setattr(module, "_gh", gh)
    module.ensure_stable_release(REPO, target="commit", work_root=tmp_path)
    assert ["release", "edit", "stable", "-R", REPO, "--draft=false"] in gh.calls


def _recoverable_candidate(module, output):
    output.mkdir(parents=True)
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    archive = output / "update.zip"
    archive.write_bytes(b"zip")
    manifest = _signed_manifest("1.0.1", key, key_id, module.asset_url(REPO, "v1.0.1", archive.name))
    manifest["package"]["sha256"] = hashlib.sha256(b"zip").hexdigest().upper()
    manifest["signature"]["value"] = base64.b64encode(key.sign(canonical_manifest_bytes(manifest))).decode()
    path = output / "stable.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    release = module.full.BuiltFullUpdate("1.0.1", archive, path, manifest["package"]["url"], 3,
                                          manifest["package"]["sha256"])
    module._save_candidate(output, release, [archive], repo=REPO, target="commit", title="title", notes=(), previous=None)
    return channel, release


def test_candidate_recovery_checks_signature_files_and_path_boundary(tmp_path):
    module = _module()
    output = tmp_path / "output"
    channel, release = _recoverable_candidate(module, output)
    candidate, recovered, assets = module._load_candidate(output, repo=REPO, version="1.0.1", channel=channel)
    assert recovered == release and assets == [release.archive_path]
    release.archive_path.write_bytes(b"bad")
    with pytest.raises(ValueError, match="已改变"):
        module._load_candidate(output, repo=REPO, version="1.0.1", channel=channel)
    candidate["assets"][0]["path"] = "../outside.zip"
    (tmp_path / "outside.zip").write_bytes(b"zip")
    module._write_json(output / "publish-candidate.json", candidate)
    with pytest.raises(ValueError, match="越界"):
        module._load_candidate(output, repo=REPO, version="1.0.1", channel=channel)


def _add_candidate_delta(module, output, *, bad_url=False, missing=False):
    channel, release = _recoverable_candidate(module, output)
    key = Ed25519PrivateKey.generate()
    channel, key_id = _channel(key)
    manifest = json.loads(release.manifest_path.read_text("utf-8"))
    delta = output / "component.zip"
    delta.write_bytes(b"delta")
    manifest["incremental_packages"] = [{"base_fingerprint": "A" * 64, "package": {
        "url": "https://other.example/component.zip" if bad_url else module.asset_url(REPO, "v1.0.1", delta.name),
        "size": delta.stat().st_size, "sha256": hashlib.sha256(delta.read_bytes()).hexdigest().upper(),
    }}]
    manifest["signature"].update(key_id=key_id, value=base64.b64encode(key.sign(canonical_manifest_bytes(manifest))).decode())
    release.manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    module._save_candidate(output, release, [release.archive_path] if missing else [release.archive_path, delta],
                           repo=REPO, target="commit", title="title", notes=(), previous=None)
    return channel, release, delta


@pytest.mark.parametrize("case", ["valid", "missing", "foreign_url"])
def test_candidate_recovery_requires_every_signed_delta_asset(tmp_path, case):
    module = _module()
    output = tmp_path / "candidate"
    channel, _, delta = _add_candidate_delta(module, output, bad_url=case == "foreign_url", missing=case == "missing")
    if case == "valid":
        _, _, assets = module._load_candidate(output, repo=REPO, version="1.0.1", channel=channel)
        assert delta in assets
    else:
        with pytest.raises(ValueError, match="不一致|不属于"):
            module._load_candidate(output, repo=REPO, version="1.0.1", channel=channel)


def test_rollback_verifies_all_signed_delta_assets_before_stable_write(tmp_path, monkeypatch):
    module = _module()
    channel, release, _ = _add_candidate_delta(module, tmp_path / "candidate")
    verified, uploaded = [], []
    monkeypatch.setattr(module, "download_asset", lambda *a: release.manifest_path)
    def verify(url, **kwargs):
        verified.append(url)
        if url.endswith("component.zip"):
            raise ValueError("delta digest mismatch")
    monkeypatch.setattr(module, "verify_public_asset", verify)
    monkeypatch.setattr(module, "upload_assets", lambda *a: uploaded.append(a))
    with pytest.raises(ValueError, match="delta digest"):
        module.rollback_stable(REPO, "1.0.1", channel=channel, work_root=tmp_path / "work")
    assert len(verified) == 2 and uploaded == []


@pytest.mark.parametrize("changed", [None, "target", "manifest_sha256", "packages"])
def test_resume_from_web_upload_requires_matching_public_candidate(tmp_path, monkeypatch, changed):
    module = _module()
    channel, release, delta = _add_candidate_delta(module, tmp_path / "candidate")
    versioned = release.manifest_path.with_name("stable-1.0.1.json")
    versioned.write_bytes(release.manifest_path.read_bytes())
    packages = module.manifest_package_releases(json.loads(versioned.read_text("utf-8")), channel)
    marker = tmp_path / "publish-candidate.json"
    value = {"schema_version": 1, "version": "1.0.1", "repo": REPO, "target": "commit",
             "manifest_sha256": module.sha256_file(versioned).lower(), "packages": sorted([
                 {"name": p.package_url.rsplit("/", 1)[-1], "size": p.package_size,
                  "sha256": p.package_sha256.lower()} for p in packages], key=lambda p: p["name"])}
    if changed:
        value[changed] = [] if changed == "packages" else "changed"
    marker.write_text(json.dumps(value), encoding="utf-8")
    gh = _FakeGh({("release", "view"): json.dumps({"isDraft": False, "assets": [
        {"name": marker.name, "size": marker.stat().st_size}]})})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "download_asset", lambda *a: marker)
    monkeypatch.setattr(module, "_verify_release_target", lambda *a, **k: None)
    monkeypatch.setattr(module, "verify_public_asset", lambda *a, **k: None)
    kwargs = dict(title="t", notes=(), version="1.0.1", assets=[release.archive_path, delta, versioned],
                  work_root=tmp_path / "work", timeout=1, resume=True, target="commit", channel=channel)
    if changed:
        with pytest.raises(ValueError, match="混合恢复"):
            module._publish_assets(REPO, "v1.0.1", **kwargs)
        assert all(call[:2] == ["release", "view"] for call in gh.calls)
    else:
        module._publish_assets(REPO, "v1.0.1", **kwargs)
        upload = next(call for call in gh.calls if call[:2] == ["release", "upload"])
        assert str(delta) in upload and str(marker) not in upload and "--clobber" not in upload


def test_resume_reuses_existing_matching_assets_without_clobber(tmp_path, monkeypatch):
    module = _module()
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"zip")
    gh = _FakeGh({("release", "view"): json.dumps({"isDraft": True, "assets": [{"name": "a.zip", "size": 3}]})})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "download_asset", lambda *a: archive)
    verified = []
    monkeypatch.setattr(module, "verify_public_asset", lambda url, **kwargs: verified.append(url))
    monkeypatch.setattr(module, "_verify_release_target", lambda *a, **k: None)
    module._publish_assets(REPO, "v1.0.1", title="t", notes=(), version="1.0.1", assets=[archive],
                            work_root=tmp_path, timeout=1, resume=True, target="commit")
    upload = next(c for c in gh.calls if c[:2] == ["release", "upload"])
    assert str(archive) not in upload and "--clobber" not in upload
    assert str(archive) + ".sha256" in upload
    assert verified == [module.asset_url(REPO, "v1.0.1", "a.zip")]


def test_resume_rejects_different_remote_bytes_before_any_write(tmp_path, monkeypatch):
    module = _module()
    archive = tmp_path / "a.zip"
    archive.write_bytes(b"zip")
    other = tmp_path / "other.zip"
    other.write_bytes(b"bad")
    gh = _FakeGh({("release", "view"): json.dumps({"assets": [{"name": "a.zip", "size": 3}]})})
    monkeypatch.setattr(module, "_gh", gh)
    monkeypatch.setattr(module, "download_asset", lambda *a: other)
    monkeypatch.setattr(module, "_verify_release_target", lambda *a, **k: None)
    with pytest.raises(ValueError, match="SHA-256"):
        module._publish_assets(REPO, "v1.0.1", title="t", notes=(), version="1.0.1", assets=[archive],
                                work_root=tmp_path, timeout=1, resume=True, target="commit")
    assert all(c[:2] == ["release", "view"] for c in gh.calls)


def test_resume_does_not_build_or_require_private_key(tmp_path, monkeypatch):
    module = _module()
    output = tmp_path / "artifacts" / "candidate"
    channel, release = _recoverable_candidate(module, output)
    monkeypatch.setattr(module, "COMMERCIAL_ROOT", tmp_path)
    monkeypatch.setattr(module, "_project_version", lambda: "1.0.1")
    monkeypatch.setattr(module, "load_update_channel", lambda: channel)
    monkeypatch.setattr(module, "repo_name_with_owner", lambda: REPO)
    monkeypatch.setattr(module, "ensure_gh_ready", lambda _: None)
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _: None)
    monkeypatch.setattr(module, "_build_candidate", lambda *a, **k: pytest.fail("rebuilt candidate"))
    monkeypatch.setattr(module, "_publish_assets", lambda *a, **k: None)
    monkeypatch.setattr(module, "_finish_publication", lambda *a, **k: None)
    assert module.main(["--resume", "--output", str(output), "--yes"]) == 0


def test_failed_smoke_restores_previous_signed_manifest_and_receipt(tmp_path, monkeypatch):
    module = _module()
    channel, release = _recoverable_candidate(module, tmp_path / "output")
    key = Ed25519PrivateKey.generate()
    previous_channel, key_id = _channel(key)
    previous = _signed_manifest("1.0.0", key, key_id, module.asset_url(REPO, "v1.0.0", "update.zip"))
    monkeypatch.setattr(module, "_verify_signed_manifest", lambda *a: None)
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _: previous)
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: None)
    monkeypatch.setattr(module, "smoke_update_from_github", lambda *a: (_ for _ in ()).throw(ValueError("smoke failed")))
    uploads = []
    monkeypatch.setattr(module, "upload_assets", lambda repo, tag, paths: uploads.append(json.loads(paths[0].read_text(encoding="utf-8"))))
    monkeypatch.setattr(module, "verify_stable_manifest", lambda channel, manifest: None)
    receipt = {}
    with pytest.raises(ValueError, match="回滚状态：verified"):
        module._finish_publication(REPO, release, channel=channel, work_root=tmp_path / "output" / "github",
                                   target="commit", previous=previous, skip_smoke=False, receipt=receipt)
    assert uploads == [previous] and receipt["rollback"] == "verified"
    assert json.loads((tmp_path / "output" / "publication-receipt.json").read_text(encoding="utf-8"))["status"] == "failed"


def test_resume_missing_candidate_never_rebuilds(tmp_path, monkeypatch):
    module = _module()
    root, _ = _common_main_patches(module, monkeypatch, tmp_path, live_version="0.9.8")
    monkeypatch.setattr(module, "_build_candidate", lambda *a, **k: pytest.fail("must not build"))
    with pytest.raises(SystemExit):
        module.main(["--resume", "--output", str(root / "artifacts" / "missing"), "--yes"])


def test_already_promoted_resume_failure_never_rolls_back_old_baseline(tmp_path, monkeypatch):
    module = _module()
    channel, release = _recoverable_candidate(module, tmp_path / "output")
    expected = json.loads(release.manifest_path.read_text(encoding="utf-8"))
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _: expected)
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: pytest.fail("must not write"))
    monkeypatch.setattr(module, "upload_assets", lambda *a, **k: pytest.fail("must not roll back"))
    monkeypatch.setattr(module, "verify_stable_manifest", lambda *a: (_ for _ in ()).throw(TimeoutError("timeout")))
    receipt = {}
    with pytest.raises(ValueError, match="本次未修改"):
        module._finish_publication(REPO, release, channel=channel, work_root=tmp_path / "output" / "github",
                                   target="commit", previous={"version": "1.0.0"}, skip_smoke=False, receipt=receipt)
    assert receipt["rollback"] == "not_needed_existing_stable_unchanged"


def test_pre_promotion_conflict_is_recorded_without_rollback(tmp_path, monkeypatch):
    module = _module()
    channel, release = _recoverable_candidate(module, tmp_path / "output")
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _: {"version": "9.0.0"})
    monkeypatch.setattr(module, "upload_assets", lambda *a, **k: pytest.fail("must not write"))
    receipt = {}
    with pytest.raises(ValueError, match="其他发布"):
        module._finish_publication(REPO, release, channel=channel, work_root=tmp_path / "output" / "github",
                                   target="commit", previous=None, skip_smoke=False, receipt=receipt)
    assert receipt["status"] == "failed_before_promotion"


@pytest.mark.parametrize("draft", [True, False])
def test_remote_release_target_must_match_candidate(monkeypatch, draft):
    module = _module()
    gh = _FakeGh({("api",): "different-commit"})
    monkeypatch.setattr(module, "_gh", gh)
    with pytest.raises(ValueError, match="目标提交"):
        module._verify_release_target(REPO, "v1.0.1", "candidate-commit",
                                      {"isDraft": draft, "targetCommitish": "master"})
    ref = "master" if draft else "v1.0.1"
    assert gh.calls == [["api", f"repos/{REPO}/commits/{ref}", "--jq", ".sha"]]


def test_failed_promotion_will_not_restore_over_another_publish(tmp_path, monkeypatch):
    module = _module()
    channel, release = _recoverable_candidate(module, tmp_path / "output")
    previous = {"version": "1.0.0"}
    reads = iter([previous, {"version": "2.0.0"}])
    monkeypatch.setattr(module, "fetch_stable_manifest", lambda _: next(reads))
    monkeypatch.setattr(module, "promote_manifest", lambda *a, **k: (_ for _ in ()).throw(ValueError("failed")))
    monkeypatch.setattr(module, "upload_assets", lambda *a, **k: pytest.fail("must not overwrite another publish"))
    receipt = {}
    with pytest.raises(ValueError, match="回滚状态：failed"):
        module._finish_publication(REPO, release, channel=channel, work_root=tmp_path / "output" / "github",
                                   target="commit", previous=previous, skip_smoke=False, receipt=receipt)
    assert "其他发布" in receipt["rollback_error"]
