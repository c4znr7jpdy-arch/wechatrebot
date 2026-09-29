"""Bounded, account-scoped portraits; network refresh never blocks the UI."""
from __future__ import annotations

import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import json
import logging
from pathlib import Path
import sqlite3
import threading
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler


_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="portrait")
_PENDING: set[tuple[str, str, str]] = set()
_LOCK = threading.Lock()
_LOG = logging.getLogger(__name__)


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _download(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname not in {"wx.qlogo.cn", "weixin.qq.com"} or parsed.port not in {None, 80, 443}:
        return ""
    request = Request(url, headers={"User-Agent": "WeChatReBot", "Accept": "image/*"})
    with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=3) as response:
        data = response.read(128 * 1024 + 1)
    if len(data) > 128 * 1024:
        return ""
    mime = "image/png" if data.startswith(b"\x89PNG\r\n\x1a\n") else "image/jpeg" if data.startswith(b"\xff\xd8\xff") else "image/webp" if data[:4] == b"RIFF" and data[8:12] == b"WEBP" else ""
    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}" if mime else ""


class PortraitCache:
    def __init__(self, root: str | Path):
        self.path = Path(root) / "data" / "portraits.db"

    @contextmanager
    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        try:
            with db:
                db.execute("CREATE TABLE IF NOT EXISTS portraits (account TEXT, entity TEXT, profile TEXT, image TEXT, checked REAL, PRIMARY KEY(account, entity))")
                yield db
        finally:
            db.close()

    def read(self, account: str, entity: str) -> tuple[dict, str, float]:
        with self._connect() as db:
            row = db.execute("SELECT profile,image,checked FROM portraits WHERE account=? AND entity=?", (account, entity)).fetchone()
        return (json.loads(row[0]), row[1], row[2]) if row else ({}, "", 0)

    def enrich(self, account: str, item: dict, entity: str, fetch=None, *, force=False) -> dict:
        try:
            cached, image, checked = self.read(account, entity)
        except (OSError, sqlite3.Error, ValueError):
            _LOG.warning("Portrait cache could not be read")
            return item
        merged = {**cached, **{k: v for k, v in item.items() if v not in (None, "")}}
        # Detailed contact portraits supersede incomplete/stale list records.
        merged["avatar"] = image or cached.get("avatar") or item.get("avatar", "")
        changed = item.get("member_count") is not None and item.get("member_count") != cached.get("member_count")
        if force or changed or time.time() - checked >= 300:
            key = (str(self.path), account, entity)
            with _LOCK:
                if key not in _PENDING and len(_PENDING) < 128:
                    _PENDING.add(key)
                    _POOL.submit(self._refresh, key, item, cached, image, fetch)
        return merged

    def _refresh(self, key, item, cached, image, fetch):
        try:
            profile = {**cached, **{k: v for k, v in item.items() if v not in (None, "")}}
            try:
                fresh = fetch() if fetch else {}
                profile.update({k: v for k, v in fresh.items() if v not in (None, "")})
                source = str(profile.get("avatar") or "")
                if source:
                    image = _download(source) or image
            except (OSError, ValueError):
                _LOG.debug("Portrait refresh unavailable; retaining last successful image")
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO portraits VALUES (?,?,?,?,?)", (key[1], key[2], json.dumps(profile, ensure_ascii=False), image, time.time()))
                db.execute("DELETE FROM portraits WHERE rowid IN (SELECT rowid FROM portraits ORDER BY checked DESC LIMIT -1 OFFSET 256)")
        except (OSError, sqlite3.Error, ValueError):
            _LOG.warning("Portrait cache could not be updated")
        finally:
            with _LOCK:
                _PENDING.discard(key)
