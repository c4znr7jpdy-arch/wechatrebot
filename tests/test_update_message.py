import base64
import hashlib
from http.client import IncompleteRead
import io
import json
from types import SimpleNamespace
import urllib.request

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import pytest

from wechatrebot_manager import update_message as module
from wechatrebot_manager.updates import (
    UpdateChannel, UpdateError, canonical_manifest_bytes, check_for_update, parse_update_manifest,
)

BASE = "https://github.com/test-owner/test-repo/releases/download/"
DIGEST = "a" * 64


def message(**changes):
    return {"schema_version": 1, "version": "1.0.1", "manifest_sha256": DIGEST,
            "title": "🎉 新版本", "notes": "✨ 新功能\n\n修复：\n✅ 稳定性改进", **changes}


def parse(value):
    return module.parse_update_message(value, version="1.0.1", manifest_sha256=DIGEST)


def test_message_preserves_emoji_and_blank_lines_and_normalizes_windows_newlines():
    result = parse(message(notes="✨ 第一行\r\n\r\n\t✅ 第二行\r最后一行"))
    assert result["notes"] == "✨ 第一行\n\n\t✅ 第二行\n最后一行"
    assert result["title"] == "🎉 新版本"
    assert module.create_update_message(version="1.0.1", manifest_sha256=DIGEST,
        title=result["title"], notes=result["notes"]) == result


@pytest.mark.parametrize("changes", [
    {"schema_version": True}, {"version": "1.0.2"}, {"manifest_sha256": "b" * 64},
    {"title": "x" * 101}, {"notes": "x" * 10001}, {"title": ""}, {"notes": " \n"},
    {"notes": ["不是文本"]}, {"notes": "带\x00控制符"}, {"title": "bad\x7f"},
    {"notes": "bad\x85"}, {"notes": "bad\ud800"}, {"mandatory": True}, {"package": {}},
])
def test_message_rejects_malformed_or_mismatched_content(changes):
    with pytest.raises(module.UpdateMessageError):
        parse(message(**changes))


def test_message_asset_is_fixed_to_the_configured_github_repo_and_manifest_digest():
    channel = SimpleNamespace(manifest_url=BASE + "stable/stable.json")
    assert module.message_asset_name(DIGEST) == f"release-message-{DIGEST}.json"
    assert module.message_asset_url(channel, "1.0.1", DIGEST) == BASE + f"v1.0.1/release-message-{DIGEST}.json"
    for url in (BASE.replace("https:", "http:") + "stable/stable.json",
                "https://evil.invalid/owner/repo/releases/download/stable/stable.json",
                BASE + "stable/stable.json?from=untrusted", BASE + "stable/stable.json#fragment"):
        with pytest.raises(module.UpdateMessageError):
            module.message_asset_url(SimpleNamespace(manifest_url=url), "1.0.1", DIGEST)
    for digest in ("../escape", "A" * 64, "a" * 63):
        with pytest.raises(module.UpdateMessageError):
            module.message_asset_name(digest)
    with pytest.raises(module.UpdateMessageError):
        module.message_asset_url(channel, "../escape", DIGEST)


class Response(io.BytesIO):
    def __init__(self, raw, url=BASE + "stable/stable.json"):
        super().__init__(raw)
        self.url = url

    def geturl(self):
        return self.url


def fake_opener(monkeypatch, raw, *, url=BASE + "message.json", error=None):
    opened = []

    class Opener:
        def open(self, request, *, timeout):
            opened.append((request.full_url, timeout))
            if error:
                raise error
            return Response(raw, url)

    monkeypatch.setattr(module.urllib.request, "build_opener", lambda *_: Opener())
    return opened


def test_message_download_is_bounded_and_uses_short_timeout(monkeypatch):
    raw = json.dumps(message(), ensure_ascii=False).encode("utf-8")
    opened = fake_opener(monkeypatch, raw)
    channel = SimpleNamespace(manifest_url=BASE + "stable/stable.json")
    assert module.load_update_message(channel, version="1.0.1", manifest_sha256=DIGEST) == message()
    assert opened == [(BASE + f"v1.0.1/release-message-{DIGEST}.json", 3)]


@pytest.mark.parametrize("raw,url,error", [
    (b"x" * (module.MAX_MESSAGE_BYTES + 1), BASE + "message.json", None),
    (b"not JSON", BASE + "message.json", None),
    (json.dumps(message(mandatory=True)).encode(), BASE + "message.json", None),
    (json.dumps(message()).encode(), "https://evil.invalid/message.json", None),
    (b"", BASE + "message.json", TimeoutError("slow optional presentation")),
    (b"", BASE + "message.json", IncompleteRead(b"interrupted")),
    (b"[" * 1500 + b"]" * 1500, BASE + "message.json", None),
], ids=["oversized", "invalid-json", "extra-field", "unsafe-final-url", "timeout", "interrupted", "deep-json"])
def test_invalid_or_unavailable_message_is_an_optional_fallback(monkeypatch, raw, url, error):
    fake_opener(monkeypatch, raw, url=url, error=error)
    channel = SimpleNamespace(manifest_url=BASE + "stable/stable.json")
    assert module.load_update_message(channel, version="1.0.1", manifest_sha256=DIGEST) is None


@pytest.mark.parametrize("url", ["http://github.com/message.json", "https://evil.invalid/message.json",
    "https://github.com@evil.invalid/message.json", "https://github.com:444/message.json",
    "https://github.com/message.json#fragment"])
def test_message_redirect_is_rejected_before_opening_untrusted_target(url):
    request = urllib.request.Request(BASE + "message.json")
    with pytest.raises(module.UpdateMessageError):
        module._MessageRedirectHandler().redirect_request(request, None, 302, "redirect", {}, url)


def signed_manifest():
    key = Ed25519PrivateKey.generate()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    channel = UpdateChannel(BASE + "stable/stable.json", "stable", "test-key", public)
    value = {"schema_version": 1, "product": "wechatrebot-desktop", "channel": "stable",
        "version": "1.0.1", "title": "签名默认标题", "notes": ["签名默认说明"], "mandatory": True,
        "package": {"url": BASE + "v1.0.1/update.zip", "size": 12, "sha256": "b" * 64, "format": "zip"}}
    value["signature"] = {"algorithm": "ed25519", "key_id": "test-key",
        "value": base64.b64encode(key.sign(canonical_manifest_bytes(value))).decode("ascii")}
    return channel, value


def test_update_check_applies_only_presentation_after_signature_validation(monkeypatch):
    from wechatrebot_manager import updates

    channel, value = signed_manifest()
    raw = json.dumps(value).encode()
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: Response(raw))
    calls = []

    def load(actual_channel, *, version, manifest_sha256):
        calls.append((actual_channel, version, manifest_sha256))
        return message(manifest_sha256=manifest_sha256)

    monkeypatch.setattr(module, "load_update_message", load)
    expected = parse_update_manifest(value, channel, current_version="1.0.0")
    actual = check_for_update(channel, current_version="1.0.0")
    assert actual.title == "🎉 新版本" and actual.notes == (message()["notes"],)
    assert actual.mandatory is True
    for name in ("version", "package_url", "package_size", "package_sha256", "build", "minimum_supported_version"):
        assert getattr(actual, name) == getattr(expected, name)
    assert calls == [(channel, "1.0.1", hashlib.sha256(raw).hexdigest())]


def test_update_check_rejects_tampered_manifest_before_fetching_optional_text(monkeypatch):
    from wechatrebot_manager import updates

    channel, value = signed_manifest()
    value["mandatory"] = False
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: Response(json.dumps(value).encode()))
    monkeypatch.setattr(module, "load_update_message", lambda *_a, **_k: pytest.fail("must verify signature first"))
    with pytest.raises(UpdateError, match="签名"):
        check_for_update(channel, current_version="1.0.0")


def test_update_check_keeps_signed_text_when_message_is_missing(monkeypatch):
    from wechatrebot_manager import updates

    channel, value = signed_manifest()
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *_a, **_k: Response(json.dumps(value).encode()))
    monkeypatch.setattr(module, "load_update_message", lambda *_a, **_k: None)
    assert check_for_update(channel, current_version="1.0.0").notes == ("签名默认说明",)
