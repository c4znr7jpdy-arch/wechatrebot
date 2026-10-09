"""Short-link storage client for the public short-drama player.

Only the selection metadata is uploaded.  The player still requests HLS
manifests and segments directly from the source URL in the user's browser.
"""

from __future__ import annotations

import logging
from urllib.parse import quote, urlsplit

import aiohttp

from .search import SearchResult
from .watch_url import build_selection_payload


logger = logging.getLogger("jiang_short_drama")


class RemoteSelectionStore:
    """Upload a complete ranked selection and receive a short player URL."""

    def __init__(
        self,
        *,
        api_url: object = "",
        api_token: object = "",
        player_url: object = "",
        timeout_seconds: float = 8,
    ) -> None:
        self.api_url = str(api_url or "").strip().rstrip("/")
        self.api_token = str(api_token or "").strip()
        self.player_url = str(player_url or "").strip().rstrip("#")
        self.timeout_seconds = max(2.0, min(float(timeout_seconds or 8), 20.0))

    @property
    def enabled(self) -> bool:
        parsed = urlsplit(self.api_url)
        player = urlsplit(self.player_url)
        return bool(
            self.api_token
            and parsed.scheme == "https"
            and parsed.netloc
            and player.scheme == "https"
            and player.netloc
            and not player.query
            and not player.fragment
        )

    async def create_url(
        self,
        results: tuple[SearchResult, ...],
        *,
        heading: str,
        kind: str,
    ) -> str | None:
        if not self.enabled:
            return None
        payload = build_selection_payload(results, heading=heading, kind=kind)
        if payload is None:
            return None

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    self.api_url,
                    json={"payload": payload},
                    headers=headers,
                ) as response:
                    body = await response.json(content_type=None)
                    if response.status != 200 or not isinstance(body, dict):
                        logger.warning(
                            "[SHORT_DRAMA] selection store rejected payload status=%s",
                            response.status,
                        )
                        return None
                    token = str(body.get("token") or "").strip()
                    if not token or len(token) > 96:
                        logger.warning("[SHORT_DRAMA] selection store returned invalid token")
                        return None
                    return f"{self.player_url}?s={quote(token, safe='')}"
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            logger.warning(
                "[SHORT_DRAMA] selection store unavailable: %s",
                type(exc).__name__,
            )
            return None
