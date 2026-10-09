#!/usr/bin/env python3
"""Tiny authenticated state store for short-drama selection pages.

The service stores only short-lived JSON metadata. It never proxies HLS
manifests, segments, or any other media bytes.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import tempfile
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit


LOGGER = logging.getLogger("short_drama_state")
TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{16,96}$")
MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_ITEMS = 24
MAX_EPISODES = 2000


class StateStore:
    def __init__(self, root: Path, ttl_seconds: int, max_entries: int) -> None:
        self.root = root
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, payload: dict[str, object]) -> tuple[str, float]:
        now = time.time()
        self.purge(now)
        token = secrets.token_urlsafe(24)
        expires_at = now + self.ttl_seconds
        record = {"expires_at": expires_at, "payload": payload}
        destination = self.root / f"{token}.json"
        fd, temporary = tempfile.mkstemp(prefix=".selection-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(record, stream, ensure_ascii=False, separators=(",", ":"))
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, destination)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        self.purge(now)
        self.trim()
        return token, expires_at

    def get(self, token: str) -> dict[str, object] | None:
        if not TOKEN_RE.fullmatch(token):
            return None
        path = self.root / f"{token}.json"
        try:
            with path.open("r", encoding="utf-8") as stream:
                record = json.load(stream)
        except (FileNotFoundError, OSError, ValueError, TypeError):
            return None
        expires_at = record.get("expires_at") if isinstance(record, dict) else None
        if not isinstance(expires_at, (int, float)) or expires_at <= time.time():
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            return None
        payload = record.get("payload")
        return payload if isinstance(payload, dict) else None

    def purge(self, now: float | None = None) -> None:
        current = time.time() if now is None else now
        for path in self.root.glob("*.json"):
            try:
                with path.open("r", encoding="utf-8") as stream:
                    record = json.load(stream)
                if not isinstance(record, dict) or float(record.get("expires_at", 0)) <= current:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError):
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    pass

    def trim(self) -> None:
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        for path in files[: max(0, len(files) - self.max_entries)]:
            try:
                path.unlink()
            except OSError:
                pass


def _valid_http_url(value: object) -> bool:
    parsed = urlsplit(str(value or ""))
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _validate_payload(payload: object) -> tuple[dict[str, object] | None, str | None]:
    if not isinstance(payload, dict) or payload.get("v") != 1:
        return None, "unsupported payload"
    kind = payload.get("kind")
    if kind not in {"search", "recommendations"}:
        return None, "unsupported selection kind"
    heading = payload.get("heading")
    items = payload.get("items")
    if not isinstance(heading, str) or len(heading) > 200:
        return None, "invalid heading"
    if not isinstance(items, list) or not items or len(items) > MAX_ITEMS:
        return None, "invalid item count"
    for item in items:
        if not isinstance(item, dict):
            return None, "invalid item"
        if not isinstance(item.get("title"), str) or len(item["title"]) > 300:
            return None, "invalid item title"
        count = item.get("episode_count")
        episodes = item.get("episodes")
        if isinstance(episodes, list):
            if not episodes or len(episodes) > MAX_EPISODES:
                return None, "invalid episode count"
            for episode in episodes:
                if (
                    not isinstance(episode, dict)
                    or not isinstance(episode.get("name"), str)
                    or len(episode["name"]) > 200
                    or not _valid_http_url(episode.get("url"))
                ):
                    return None, "invalid episode"
        elif "episode_template" in item or "episode_url_template" in item:
            if not isinstance(count, int) or count < 1 or count > MAX_EPISODES:
                return None, "invalid episode template count"
        else:
            return None, "missing episodes"
    return payload, None


class Handler(BaseHTTPRequestHandler):
    server_version = "short-drama-state/1.0"

    @property
    def state(self) -> StateStore:
        return self.server.state_store  # type: ignore[attr-defined]

    @property
    def api_token(self) -> str:
        return self.server.api_token  # type: ignore[attr-defined]

    def _send_json(self, status: int, body: dict[str, object]) -> None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def do_GET(self) -> None:  # noqa: N802
        path = unquote(urlsplit(self.path).path)
        if path == "/health":
            self._send_json(HTTPStatus.OK, {"ok": True, "service": "short-drama-state"})
            return
        prefix = "/selections/"
        if path.startswith(prefix):
            payload = self.state.get(path[len(prefix) :])
            if payload is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "selection expired"})
            else:
                self._send_json(HTTPStatus.OK, payload)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/selections":
            self._send_json(HTTPStatus.NOT_FOUND, {"ok": False, "error": "not found"})
            return
        provided = self.headers.get("Authorization", "")
        expected = f"Bearer {self.api_token}"
        if not hmac.compare_digest(provided, expected):
            self._send_json(HTTPStatus.UNAUTHORIZED, {"ok": False, "error": "unauthorized"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length <= 0 or length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"ok": False, "error": "payload too large"})
            return
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": "invalid json"})
            return
        payload, error = _validate_payload(body.get("payload") if isinstance(body, dict) else None)
        if payload is None:
            self._send_json(HTTPStatus.BAD_REQUEST, {"ok": False, "error": error or "invalid payload"})
            return
        token, expires_at = self.state.put(payload)
        self._send_json(HTTPStatus.OK, {"ok": True, "token": token, "expires_at": expires_at})

    def log_message(self, fmt: str, *args: object) -> None:
        LOGGER.info("%s - %s", self.address_string(), fmt % args)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default=os.environ.get("SHORT_DRAMA_STATE_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.environ.get("SHORT_DRAMA_STATE_PORT", "6198")))
    parser.add_argument(
        "--directory",
        default=os.environ.get("SHORT_DRAMA_STATE_DIRECTORY", "/var/lib/short-drama-state"),
    )
    parser.add_argument("--ttl", type=int, default=int(os.environ.get("SHORT_DRAMA_STATE_TTL", "21600")))
    parser.add_argument("--max-entries", type=int, default=int(os.environ.get("SHORT_DRAMA_STATE_MAX_ENTRIES", "1000")))
    args = parser.parse_args()
    api_token = os.environ.get("SHORT_DRAMA_STATE_TOKEN", "").strip()
    if len(api_token) < 24:
        raise SystemExit("SHORT_DRAMA_STATE_TOKEN must contain at least 24 characters")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.state_store = StateStore(Path(args.directory), args.ttl, args.max_entries)  # type: ignore[attr-defined]
    server.api_token = api_token  # type: ignore[attr-defined]
    LOGGER.info("short-drama state listening on %s:%s", args.host, args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
