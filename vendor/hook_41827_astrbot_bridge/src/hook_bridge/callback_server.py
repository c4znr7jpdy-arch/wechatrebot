from __future__ import annotations

import asyncio
import inspect
from json import JSONDecodeError
import logging
from typing import Any

from aiohttp import ContentTypeError, web


logger = logging.getLogger(__name__)


def create_callback_app(
    queue: asyncio.Queue[dict],
    max_body_bytes: int,
    callback_path: str = "/api/recvMsg",
    action_router: Any | None = None,
    status_provider: Any | None = None,
) -> web.Application:
    app = web.Application(client_max_size=max_body_bytes)

    async def receive(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            raise
        except (ContentTypeError, JSONDecodeError, UnicodeDecodeError):
            return web.json_response(
                {"ok": False, "error": "request body must be JSON"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "JSON root must be an object"}, status=400
            )
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            return web.json_response(
                {"ok": False, "error": "event queue is full"}, status=503
            )
        logger.debug(
            "accepted HOOK callback method=%s keys=%s message_type=%r",
            request.method,
            sorted(payload),
            payload.get("messageType"),
        )
        return web.json_response({"ok": True})

    async def send_raw_xml(request: web.Request) -> web.Response:
        try:
            payload = await request.json()
        except web.HTTPRequestEntityTooLarge:
            raise
        except (ContentTypeError, JSONDecodeError, UnicodeDecodeError):
            return web.json_response(
                {"ok": False, "error": "request body must be JSON"}, status=400
            )
        if not isinstance(payload, dict):
            return web.json_response(
                {"ok": False, "error": "JSON root must be an object"}, status=400
            )
        target = str(
            payload.get("to_wxid")
            or payload.get("group_id")
            or payload.get("user_id")
            or ""
        ).strip()
        content = str(payload.get("content") or "").strip()
        if not target or not content:
            return web.json_response(
                {"ok": False, "error": "to_wxid and content are required"},
                status=400,
            )
        if action_router is None:
            return web.json_response(
                {"ok": False, "error": "raw XML sender is unavailable"}, status=503
            )
        try:
            result = await action_router.handle(
                {
                    "action": "send_raw_xml",
                    "params": {
                        "to_wxid": target,
                        "content": content,
                        "send_type": payload.get("send_type"),
                        "type": payload.get("type"),
                    },
                }
            )
        except Exception:
            return web.json_response(
                {"ok": False, "error": "raw XML send failed"}, status=502
            )
        if not isinstance(result, dict) or result.get("status") != "ok":
            return web.json_response(
                {"ok": False, "error": "raw XML send failed"}, status=502
            )
        return web.json_response({"ok": True})

    async def health(_request: web.Request) -> web.Response:
        if status_provider is None:
            payload: Any = {"service_ready": True}
        else:
            try:
                payload = status_provider()
                if inspect.isawaitable(payload):
                    payload = await payload
            except Exception:
                logger.exception("health status provider failed")
                return web.json_response(
                    {"service_ready": True, "status_available": False},
                    status=503,
                )
        if not isinstance(payload, dict):
            return web.json_response(
                {"service_ready": True, "status_available": False},
                status=503,
            )
        return web.json_response(payload)

    app.router.add_put(callback_path, receive)
    app.router.add_post(callback_path, receive)
    app.router.add_get("/api/health", health)
    app.router.add_post("/send_raw_xml", send_raw_xml)
    return app
