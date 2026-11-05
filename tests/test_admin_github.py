import base64
import copy
import hashlib
import json
from urllib.parse import parse_qs, urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from services.activation_service.admin.auth import private_json
from services.activation_service.admin.github import GitHubError, GitHubReleases
from wechatrebot_manager.updates import UpdateChannel, canonical_manifest_bytes

REPO = "test-owner/test-repo"
BASE = f"https://github.com/{REPO}/releases/download/"


def signed(key, version, package=b"verified update bytes", *, package_url=""):
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    key_id = hashlib.sha256(public).hexdigest().upper()[:16]
    value = {"schema_version": 1, "product": "wechatrebot-desktop", "channel": "stable", "version": version,
             "notes": [], "package": {"url": package_url or BASE + f"v{version}/update.zip", "size": len(package),
                                        "sha256": hashlib.sha256(package).hexdigest(), "format": "zip"}}
    value["signature"] = {"algorithm": "ed25519", "key_id": key_id,
                          "value": base64.b64encode(key.sign(canonical_manifest_bytes(value))).decode("ascii")}
    return json.dumps(value, ensure_ascii=False).encode("utf-8")


class FakeGitHub(GitHubReleases):
    def __init__(self, directory):
        self.signer = Ed25519PrivateKey.generate()
        public = self.signer.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        super().__init__(directory, channel=UpdateChannel(BASE + "stable/stable.json", "stable",
            hashlib.sha256(public).hexdigest().upper()[:16], public))
        self.previous, self.target = signed(self.signer, "1.0.0"), signed(self.signer, "1.0.1")
        self.contents = {11: self.previous}
        self.assets = {11: self.asset(11, "stable.json", self.previous)}
        self.calls, self.downloads = [], []
        self.fail_readback, self.fail_delete, self.bad_package = False, False, False
        self.race_content = False

    @staticmethod
    def asset(identifier, name, raw):
        return {"id": identifier, "name": name, "size": len(raw), "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}

    async def _api(self, method, path, *, payload=None, content=None):
        self.calls.append((method, path, payload))
        if method == "GET" and path.startswith("/releases/tags/v"):
            version = path.split("/v")[-1]
            return {"id": 1, "tag_name": "v" + version, "draft": False, "prerelease": False,
                    "assets": [{"name": "stable-" + version + ".json"}]}
        if method == "GET" and path == "/releases/tags/stable":
            values = copy.deepcopy(list(self.assets.values()))
            if self.race_content:
                values[0]["digest"] = "sha256:" + "f" * 64
            return {"id": 9, "tag_name": "stable", "draft": False, "prerelease": False, "assets": values}
        if method == "POST":
            name = parse_qs(urlsplit(path).query)["name"][0]
            self.contents[22] = content
            self.assets[22] = self.asset(22, name, content)
            return dict(self.assets[22])
        identifier = int(path.rsplit("/", 1)[1])
        if method == "PATCH":
            assert identifier in self.assets
            assert not any(a["name"] == payload["name"] and a["id"] != identifier for a in self.assets.values())
            self.assets[identifier]["name"] = payload["name"]
            return dict(self.assets[identifier])
        if method == "DELETE":
            if identifier not in self.assets:
                raise GitHubError("GitHub 请求失败（HTTP 404）")
            del self.assets[identifier]
            if self.fail_delete:
                self.fail_delete = False
                raise GitHubError("download response lost")
            return None
        raise AssertionError((method, path))

    async def _download(self, url, *, maximum, expected_size=0, expected_sha256=""):
        self.downloads.append(url)
        if expected_size:
            if self.bad_package:
                raise GitHubError("更新包 SHA-256 不符")
            assert expected_sha256.lower() == hashlib.sha256(b"verified update bytes").hexdigest()
            return b""
        if url.endswith("/stable/stable.json"):
            asset = next(a for a in self.assets.values() if a["name"] == "stable.json")
            return self.contents[asset["id"]]
        return self.target

    async def _readback(self, expected, *, attempts=10):
        if expected == self.target and self.fail_readback:
            raise GitHubError("readback mismatch")
        assert await self._download(BASE + "stable/stable.json", maximum=1024 * 1024) == expected


@pytest.mark.asyncio
async def test_signed_manifest_verification_rejects_tampering_cross_repo_and_non_object(tmp_path):
    github = FakeGitHub(tmp_path)
    github._manifest(github.target, "1.0.1")
    for raw in (github.target.replace(b"1.0.1", b"1.0.2"), b"[]",
                signed(github.signer, "1.0.1", package_url="https://evil.invalid/payload.zip")):
        with pytest.raises(GitHubError, match="验签"):
            github._manifest(raw)
    for url in ("http://github.com/a", "https://127.0.0.1/a", "https://github.com@evil.invalid/a", "https://github.com:444/a"):
        with pytest.raises(GitHubError):
            github._public_url(url)


@pytest.mark.asyncio
async def test_switch_stream_verifies_package_before_mutation_and_keeps_old_asset(tmp_path):
    github = FakeGitHub(tmp_path)
    result = await github.switch("1.0.1", hashlib.sha256(github.previous).hexdigest())
    assert result == {"version": "1.0.1", "previous_version": "1.0.0", "changed": True}
    assert github.assets[22]["name"] == "stable.json" and github.assets[11]["name"] == "stable-previous-11.json"
    assert any(url.endswith("/update.zip") for url in github.downloads)
    assert not (tmp_path / "admin-release-recovery.json").exists()


@pytest.mark.asyncio
async def test_switch_verifies_every_signed_incremental_asset_before_mutation(tmp_path):
    github = FakeGitHub(tmp_path)
    manifest = json.loads(github.target)
    delta = b"verified delta bytes"
    manifest["incremental_packages"] = [{
        "base_fingerprint": "A" * 64,
        "package": {
            "url": BASE + "v1.0.1/component.zip",
            "size": len(delta),
            "sha256": hashlib.sha256(delta).hexdigest(),
            "format": "zip",
        },
    }]
    manifest["signature"]["value"] = base64.b64encode(
        github.signer.sign(canonical_manifest_bytes(manifest))
    ).decode("ascii")
    github.target = json.dumps(manifest, ensure_ascii=False).encode("utf-8")
    original_download = github._download

    async def download(url, *, maximum, expected_size=0, expected_sha256=""):
        if url.endswith("/component.zip"):
            github.downloads.append(url)
            assert expected_size == len(delta)
            assert expected_sha256 == hashlib.sha256(delta).hexdigest().upper()
            return b""
        return await original_download(
            url,
            maximum=maximum,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    github._download = download
    result = await github.switch("1.0.1", hashlib.sha256(github.previous).hexdigest())
    assert result["changed"]
    assert any(url.endswith("/update.zip") for url in github.downloads)
    assert any(url.endswith("/component.zip") for url in github.downloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["package", "page_stale", "remote_race"])
async def test_failed_preflight_never_changes_stable(tmp_path, failure):
    github = FakeGitHub(tmp_path)
    github.bad_package = failure == "package"
    github.race_content = failure == "remote_race"
    digest = "0" * 64 if failure == "page_stale" else hashlib.sha256(github.previous).hexdigest()
    with pytest.raises(GitHubError):
        await github.switch("1.0.1", digest)
    assert github.assets[11]["name"] == "stable.json"
    assert not any(method != "GET" for method, _, _ in github.calls)


@pytest.mark.asyncio
async def test_failed_public_readback_restores_old_manifest(tmp_path):
    github = FakeGitHub(tmp_path)
    github.fail_readback = True
    with pytest.raises(GitHubError, match="已恢复"):
        await github.switch("1.0.1", hashlib.sha256(github.previous).hexdigest())
    assert github.assets[11]["name"] == "stable.json" and 22 not in github.assets
    assert not (tmp_path / "admin-release-recovery.json").exists()


@pytest.mark.asyncio
async def test_response_loss_during_recovery_delete_is_recoverable(tmp_path):
    github = FakeGitHub(tmp_path)
    github.fail_readback, github.fail_delete = True, True
    with pytest.raises(GitHubError, match="自动恢复未完成"):
        await github.switch("1.0.1", hashlib.sha256(github.previous).hexdigest())
    assert (tmp_path / "admin-release-recovery.json").exists()
    assert github.assets[11]["name"] == "stable.json" and 22 not in github.assets
    github.fail_readback = False
    assert (await github.switch("1.0.1", hashlib.sha256(github.previous).hexdigest()))["changed"]
    assert not (tmp_path / "admin-release-recovery.json").exists()


@pytest.mark.asyncio
async def test_restore_does_not_clobber_another_publishers_asset(tmp_path):
    github = FakeGitHub(tmp_path)
    recovery = {"old_id": 11, "new_id": 22, "previous": github.previous.decode(), "staged_name": "pending.json"}
    github.assets[11]["name"] = "previous.json"
    github.assets[33] = github.asset(33, "stable.json", github.target)
    with pytest.raises(GitHubError, match="其他发布者"):
        await github._restore(recovery)
    assert github.assets[33]["name"] == "stable.json"
    assert not any(method != "GET" for method, _, _ in github.calls)


@pytest.mark.asyncio
async def test_api_reads_all_chunks_and_never_sends_token_on_redirect(tmp_path, monkeypatch):
    from services.activation_service.admin import github as module

    seen = []

    class Response:
        status = 200
        content = None
        def __init__(self):
            self.content = self
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def iter_chunked(self, size):
            for chunk in (b'{"assets":', b'[{"id":1}', b']}'):
                yield chunk

    class Session:
        def __init__(self, **kwargs):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        def request(self, method, url, **kwargs):
            seen.append((url, kwargs))
            return Response()

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    github = FakeGitHub(tmp_path)
    private_json(tmp_path / "admin-github.json", {"token": "github_pat_" + "a" * 40})
    result = await GitHubReleases._api(github, "GET", "/releases")
    assert result == {"assets": [{"id": 1}]}
    assert seen[0][0].startswith("https://api.github.com/") and seen[0][1]["allow_redirects"] is False
    Response.status = 302
    with pytest.raises(GitHubError, match="HTTP 302") as caught:
        await GitHubReleases._api(github, "GET", "/releases")
    assert "github_pat_" not in str(caught.value)


@pytest.mark.asyncio
async def test_public_stable_readback_bypasses_cached_redirect(tmp_path, monkeypatch):
    from services.activation_service.admin import github as module

    seen = []

    class Response:
        status = 200

        def __init__(self, fresh):
            self.content = self
            self.raw = b"new stable" if fresh else b"cached old stable"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def iter_chunked(self, size):
            yield self.raw

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def request(self, method, url, **kwargs):
            seen.append(url)
            return Response(bool(parse_qs(urlsplit(url).query).get("_wrb_check")))

    monkeypatch.setattr(module.aiohttp, "ClientSession", Session)
    github = FakeGitHub(tmp_path)
    for _ in range(2):
        assert await GitHubReleases._download(github, github.channel.manifest_url, maximum=100) == b"new stable"
    assert seen[0] != seen[1]


@pytest.mark.parametrize("url", [
    BASE + "v1.0.1/stable-1.0.1.json",
    BASE + "v1.0.1/update.zip",
    "https://release-assets.githubusercontent.com/asset?sig=opaque&token=unchanged",
    "https://updates.example.test/stable.json",
    "https://github.com.evil.test/o/r/releases/download/stable/stable.json",
])
def test_fresh_manifest_url_preserves_immutable_assets_and_other_hosts(url):
    from wechatrebot_manager.updates import fresh_stable_manifest_url

    assert fresh_stable_manifest_url(url) == url


def test_fresh_manifest_url_preserves_existing_query_bytes():
    from wechatrebot_manager.updates import fresh_stable_manifest_url

    url = BASE + "stable/stable.json?download=1&name=a%2Fb"
    assert fresh_stable_manifest_url(url).startswith(url + "&_wrb_check=")
