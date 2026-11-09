"""Bounded, plain-text release messages; never an authority for update policy or packages."""
from __future__ import annotations

from http.client import HTTPException
import json
import re
import urllib.error
import urllib.parse
import urllib.request

from packaging.version import InvalidVersion, Version

MAX_MESSAGE_BYTES = 64 * 1024
MAX_MESSAGE_TITLE = 100
MAX_MESSAGE_NOTES = 10000
MESSAGE_TIMEOUT_SECONDS = 3
_FIELDS = {"schema_version", "version", "manifest_sha256", "title", "notes"}
_PUBLIC_HOSTS = {"github.com", "release-assets.githubusercontent.com", "objects.githubusercontent.com"}


class UpdateMessageError(ValueError):
    """A safe maintainer-facing validation error."""


def _identity(version: str, manifest_sha256: str) -> None:
    try:
        if not isinstance(version, str) or str(Version(version)) != version:
            raise ValueError()
    except (InvalidVersion, ValueError, TypeError):
        raise UpdateMessageError("更新说明版本号无效。") from None
    message_asset_name(manifest_sha256)


def _text(value: object, maximum: int, label: str) -> str:
    if not isinstance(value, str):
        raise UpdateMessageError(f"{label}必须是文本。")
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    if not value.strip() or len(value) > maximum:
        raise UpdateMessageError(f"{label}不能为空，且最多 {maximum} 个字符。")
    if any((ord(char) < 32 and char not in "\n\t") or 127 <= ord(char) <= 159 for char in value):
        raise UpdateMessageError(f"{label}含有不支持的控制字符。")
    try:
        value.encode("utf-8")
    except UnicodeError:
        raise UpdateMessageError(f"{label}含有无效字符。") from None
    return value


def parse_update_message(value: object, *, version: str, manifest_sha256: str) -> dict:
    _identity(version, manifest_sha256)
    if (not isinstance(value, dict) or set(value) != _FIELDS
            or type(value.get("schema_version")) is not int or value["schema_version"] != 1):
        raise UpdateMessageError("更新说明格式无效。")
    if value["version"] != version or value["manifest_sha256"] != manifest_sha256:
        raise UpdateMessageError("更新说明与当前版本或签名清单不一致。")
    result = {**value, "title": _text(value["title"], MAX_MESSAGE_TITLE, "更新标题"),
              "notes": _text(value["notes"], MAX_MESSAGE_NOTES, "更新说明")}
    if len(json.dumps(result, ensure_ascii=False).encode("utf-8")) > MAX_MESSAGE_BYTES:
        raise UpdateMessageError("更新说明数据过大。")
    return result


def create_update_message(*, version: str, manifest_sha256: str, title: str, notes: str) -> dict:
    return parse_update_message({"schema_version": 1, "version": version,
        "manifest_sha256": manifest_sha256, "title": title, "notes": notes},
        version=version, manifest_sha256=manifest_sha256)


def message_asset_name(manifest_sha256: str) -> str:
    if not isinstance(manifest_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", manifest_sha256):
        raise UpdateMessageError("更新说明的签名清单摘要无效。")
    return f"release-message-{manifest_sha256}.json"


def message_asset_url(channel, version: str, manifest_sha256: str) -> str:
    _identity(version, manifest_sha256)
    parsed = urllib.parse.urlsplit(channel.manifest_url)
    match = re.fullmatch(r"/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/releases/download/stable/stable\.json", parsed.path)
    if parsed.scheme != "https" or parsed.netloc != "github.com" or not match or parsed.query or parsed.fragment:
        raise UpdateMessageError("当前更新通道不支持独立更新说明。")
    return f"https://github.com/{match.group(1)}/releases/download/v{version}/{message_asset_name(manifest_sha256)}"


def _public_url(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname not in _PUBLIC_HOSTS
            or parsed.port not in {None, 443} or parsed.username or parsed.password or parsed.fragment):
        raise UpdateMessageError("更新说明下载地址无效。")


class _MessageRedirectHandler(urllib.request.HTTPRedirectHandler):
    max_redirections = 3

    def redirect_request(self, request, fp, code, message, headers, newurl):
        _public_url(newurl)
        return super().redirect_request(request, fp, code, message, headers, newurl)


def load_update_message(channel, *, version: str, manifest_sha256: str) -> dict | None:
    """Optional presentation only: missing, slow or invalid data keeps signed fallback text."""
    try:
        url = message_asset_url(channel, version, manifest_sha256)
        request = urllib.request.Request(url, headers={"Accept": "application/json",
            "User-Agent": "WeChatReBot-UpdateMessage/1", "Cache-Control": "no-cache"})
        opener = urllib.request.build_opener(_MessageRedirectHandler())
        with opener.open(request, timeout=MESSAGE_TIMEOUT_SECONDS) as response:
            _public_url(response.geturl())
            chunks, size = [], 0
            while chunk := response.read(min(16 * 1024, MAX_MESSAGE_BYTES + 1 - size)):
                size += len(chunk)
                if size > MAX_MESSAGE_BYTES:
                    raise UpdateMessageError("更新说明数据过大。")
                chunks.append(chunk)
        value = json.loads(b"".join(chunks).decode("utf-8"))
        return parse_update_message(value, version=version, manifest_sha256=manifest_sha256)
    except (OSError, ValueError, HTTPException, RecursionError, urllib.error.URLError):
        return None
