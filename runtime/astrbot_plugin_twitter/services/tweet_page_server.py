"""Bridge Twitter card URLs to the existing public content server."""

from __future__ import annotations

import importlib
from urllib.parse import quote, urlsplit, urlunsplit


def _valid_public_base(value: object) -> str:
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    return text


class TweetPageBridge:
    """Create card URLs through the local page server with a public fallback."""

    def __init__(self, public_base_url: object = "") -> None:
        self.public_base_url = _valid_public_base(public_base_url)

    def _rewrite_public_base(self, url: str | None) -> str | None:
        if not url or not self.public_base_url:
            return url
        generated = urlsplit(url)
        override = urlsplit(self.public_base_url)
        base_path = override.path.rstrip("/")
        path = f"{base_path}{generated.path}" if base_path else generated.path
        return urlunsplit(
            (override.scheme, override.netloc, path, generated.query, "")
        )

    @staticmethod
    def _active_server():
        for module_name in (
            "data.plugins.jiang_short_drama.main",
            "jiang_short_drama.main",
        ):
            try:
                module = importlib.import_module(module_name)
                plugin = getattr(module, "_ACTIVE_PLUGIN", None)
                server = getattr(plugin, "player_server", None)
                if server is not None:
                    return server
            except Exception:
                continue
        return None

    @staticmethod
    def _direct_tweet_url(tweet_info: dict) -> str | None:
        """Return a safe public X status URL when no local page is available.

        The page server stores tweet snapshots in memory, so it can be disabled
        in deployments that only expose the stateless short-drama player.  A
        native WeChat link card can still be sent with the public status URL;
        keeping this fallback here prevents that infrastructure detail from
        changing the subscriber's selected delivery mode to plain text.
        """
        if not isinstance(tweet_info, dict):
            return None
        username = str(tweet_info.get("username") or "").strip().lstrip("@")
        tweet_id = str(tweet_info.get("tweet_id") or "").strip()
        if not username or not tweet_id.isdigit():
            return None
        if len(username) > 128 or any(
            character in username for character in "/?#\\"
        ):
            return None
        return f"https://x.com/{quote(username, safe='')}/status/{tweet_id}"

    def create_url(self, tweet_info: dict) -> str | None:
        try:
            server = self._active_server()
        except Exception:
            server = None
        if server is not None and hasattr(server, "create_tweet_url"):
            try:
                url = server.create_tweet_url(tweet_info)
            except Exception:
                url = None
            if url:
                return self._rewrite_public_base(url)
        return self._direct_tweet_url(tweet_info)

    def create_avatar_url(self, tweet_info: dict) -> str | None:
        source_url = str(tweet_info.get("avatar") or "").strip()
        if not source_url:
            return None
        try:
            server = self._active_server()
        except Exception:
            return None
        if server is None or not hasattr(server, "create_card_cover_url"):
            return None
        try:
            url = server.create_card_cover_url(source_url)
        except Exception:
            return None
        return self._rewrite_public_base(url)
