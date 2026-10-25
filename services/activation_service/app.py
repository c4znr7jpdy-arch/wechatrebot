"""Bounded public API; administrative operations are available only through CLI."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
import sqlite3
import time
from typing import Callable

from aiohttp import web
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from wechatrebot_manager.licensing.contact import CONTACT_ENDPOINT, load_contact
from wechatrebot_manager.licensing.protocol import LicenseError, issue_lease, parse_json, verify_request
from .store import LicenseStore, lease_payload

_LOG = logging.getLogger(__name__)


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"ok": False, "error": {"code": code, "message": message}}, status=status)


def create_app(
    store: LicenseStore, key: Ed25519PrivateKey, key_id: str, *,
    clock: Callable[[], float] = time.time, rate_requests: int = 120,
    contact_path: Path | None = None,
) -> web.Application:
    if not isinstance(key, Ed25519PrivateKey) or not key_id or rate_requests < 1:
        raise ValueError("Signing key, key ID and a positive rate limit are required")
    @web.middleware
    async def boundary(request: web.Request, handler):
        # Never trust arbitrary Forwarded/X-Forwarded-For headers. A local reverse
        # proxy must also apply a per-client limit before forwarding to this port.
        try:
            allowed = await asyncio.to_thread(
                store.allow_peer, request.remote or "unknown", clock(), requests=rate_requests,
            )
            if not allowed:
                response = _error("rate_limited", "请求过于频繁，请稍后重试。", 429)
                response.headers["Retry-After"] = "60"
            else:
                response = await handler(request)
        except LicenseError as exc:
            status = 401 if exc.code in {"invalid_request", "request_replayed"} else 409
            response = _error(exc.code, str(exc), status)
        except web.HTTPRequestEntityTooLarge:
            response = _error("request_too_large", "请求数据过大。", 413)
        except web.HTTPException as exc:
            response = _error("invalid_endpoint", "请求方法或地址无效。", exc.status)
        except (sqlite3.Error, OSError) as exc:
            _LOG.error("Activation storage unavailable (%s)", type(exc).__name__)
            response = _error("service_unavailable", "授权服务暂不可用，请稍后重试。", 503)
        except Exception as exc:
            # Do not log bodies, codes, signatures or exception messages.
            _LOG.error("Activation request failed (%s)", type(exc).__name__)
            response = _error("service_unavailable", "授权服务暂不可用，请稍后重试。", 503)
        response.headers.update({"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        return response

    async def dispatch(request: web.Request) -> web.Response:
        value = parse_json(await request.read())
        now = int(clock())
        action = request.match_info["action"]
        device, body = verify_request(value, action, now)
        await asyncio.to_thread(store.accept_nonce, device, value["nonce"], now)
        if action == "preview":
            data = await asyncio.to_thread(store.preview, device, body, now)
        elif action == "trial-status":
            data = await asyncio.to_thread(store.trial_status, device, body, now)
        else:
            if action == "redeem":
                record = await asyncio.to_thread(store.redeem, device, value["public_key"], body, now)
            elif action == "trial-claim":
                record = await asyncio.to_thread(store.claim_trial, device, value["public_key"], body, now)
            else:
                record = await asyncio.to_thread(getattr(store, action), device, body, now)
            # Sign after the transaction: a transient signing/delivery failure
            # must not resurrect a consumed activation code or lose its grant.
            data = {"lease": issue_lease(key, key_id, lease_payload(record, value["nonce"], now))}
        return web.json_response({"ok": True, "data": data})

    async def health(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "data": {"status": "ok", "schema_version": 1}})

    async def contact(request: web.Request) -> web.Response:
        # The location is fixed by the server operator, never by request input.
        record = await asyncio.to_thread(load_contact, contact_path) if contact_path is not None else None
        return web.json_response({"ok": True, "data": record, "server_time": int(clock())})

    app = web.Application(middlewares=[boundary], client_max_size=16 * 1024)
    app.router.add_post("/v1/{action:preview|redeem|refresh|bind|trial-status|trial-claim}", dispatch)
    app.router.add_get(CONTACT_ENDPOINT, contact)
    app.router.add_get("/health", health)
    return app
