"""Anonymous public QR retrieval; no device state or entitlement decisions."""
from __future__ import annotations

from http.client import HTTPException
from pathlib import Path
import threading
import time
from urllib.request import Request, build_opener

from .client import _NoRedirect
from .config import load_channel
from .contact import (
    CONTACT_ENDPOINT, MAX_CONTACT_RESPONSE_BYTES, contact_snapshot, load_contact, parse_contact,
)
from .protocol import LicenseError, integer, parse_json


BUNDLED_CONTACT = Path(__file__).resolve().parents[1] / "licensing-contact.json"
CONTACT_CACHE_SECONDS = 60


def http_contact_transport(url: str) -> dict:
    request = Request(url, method="GET", headers={
        "Accept": "application/json", "Cache-Control": "no-cache",
    })
    try:
        with build_opener(_NoRedirect()).open(request, timeout=5) as response:
            raw = response.read(MAX_CONTACT_RESPONSE_BYTES + 1)
        if len(raw) > MAX_CONTACT_RESPONSE_BYTES:
            raise ValueError()
        return parse_json(raw)
    except (OSError, ValueError, HTTPException) as exc:
        raise LicenseError("contact_unavailable", "微信群二维码刷新失败，请稍后重试。") from exc


class ContactClient:
    def __init__(self, root: str | Path, *, bundled_path: Path | None = None,
                 transport=None, clock=None):
        self.root = Path(root)
        self.bundled_path = Path(bundled_path) if bundled_path is not None else BUNDLED_CONTACT
        self.transport = transport or http_contact_transport
        self.clock = clock or time
        self._lock = threading.Lock()
        self._record: dict | None = None
        self._source = "bundled"
        self._refresh_failed = False
        self._have_cache = False
        self._cache_key: str | None = None
        self._fetched_at = 0.0
        self._remote_empty: set[str] = set()
        self._server_time_verified = False
        self._trusted_time = self.clock.time()
        self._time_mark = self.clock.monotonic()

    def _now(self) -> float:
        monotonic = self.clock.monotonic()
        self._trusted_time += max(0, monotonic - self._time_mark)
        if not self._server_time_verified:
            self._trusted_time = max(self.clock.time(), self._trusted_time)
        self._time_mark = monotonic
        return self._trusted_time

    @staticmethod
    def _response(value: object) -> tuple[dict | None, int]:
        if not isinstance(value, dict) or set(value) != {"ok", "data", "server_time"} or value["ok"] is not True:
            raise LicenseError("contact_unavailable", "微信群二维码刷新失败，请稍后重试。")
        server_time = integer(value["server_time"])
        if server_time > 253402214400:
            raise LicenseError("contact_invalid", "微信群二维码资料无效，请稍后刷新。")
        record = parse_contact(value["data"]) if value["data"] is not None else None
        return record, server_time

    def _bundled(self, *, failed: bool, service_url: str | None) -> None:
        self._refresh_failed = failed
        # A known remote tombstone must not resurrect the bundled invitation on
        # a subsequent failed refresh in the same manager process.
        if service_url in self._remote_empty:
            self._record, self._source = None, "remote"
            return
        self._source = "bundled"
        try:
            self._record = load_contact(self.bundled_path)
        except (OSError, ValueError):
            self._record = None

    def _refresh(self, service_url: str | None) -> None:
        if not service_url:
            self._bundled(failed=service_url is None, service_url=service_url)
            return
        started = self.clock.monotonic()
        try:
            record, server_time = self._response(self.transport(service_url + CONTACT_ENDPOINT))
        except (OSError, ValueError, HTTPException):
            self._bundled(failed=True, service_url=service_url)
            return
        elapsed = max(0, self.clock.monotonic() - started)
        # A successful server response replaces an untrusted local estimate.
        # Once anchored, neither wall-clock changes nor older server timestamps
        # may move QR time backward or revive an expired invitation.
        previous = self._now() if self._server_time_verified else 0
        self._trusted_time = max(previous, server_time + elapsed)
        self._time_mark = self.clock.monotonic()
        self._server_time_verified = True
        self._record, self._source, self._refresh_failed = record, "remote", False
        if record is None:
            self._remote_empty.add(service_url)
        else:
            self._remote_empty.discard(service_url)

    def snapshot(self, *, force: bool = False) -> dict:
        if type(force) is not bool:
            raise LicenseError("invalid_data", "二维码刷新参数无效，请重试。")
        with self._lock:
            try:
                service_url = load_channel(self.root).service_url
            except (OSError, ValueError):
                service_url = None
            elapsed = self.clock.monotonic() - self._fetched_at
            if (force or not self._have_cache or service_url != self._cache_key
                    or not 0 <= elapsed < CONTACT_CACHE_SECONDS):
                self._refresh(service_url)
                self._have_cache = True
                self._cache_key = service_url
                self._fetched_at = self.clock.monotonic()
            result = contact_snapshot(self._record, self._now(), self._source)
            if self._refresh_failed:
                result["message"] = {
                    "ready": "二维码刷新失败，当前显示安装包内置的有效微信群二维码，可稍后重试。",
                    "expired": "二维码刷新失败，内置微信群二维码已到期，请稍后重试。",
                    "unavailable": "二维码刷新失败，暂时没有可用的微信群二维码，请稍后重试。",
                }[result["state"]]
            return result


__all__ = ["ContactClient"]
