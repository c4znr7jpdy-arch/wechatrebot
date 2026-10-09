"""Token-based multi-episode HLS player served by the AstrBot plugin."""

from __future__ import annotations

import asyncio
import base64
import copy
import ipaddress
import logging
import posixpath
import re
import secrets
import time
from dataclasses import dataclass
from html import escape
from io import BytesIO
from typing import Awaitable, Callable
from urllib.parse import (
    SplitResult,
    quote,
    unquote,
    urlencode,
    urljoin,
    urlsplit,
    urlunsplit,
)

import aiohttp
from aiohttp import web
from PIL import Image, ImageDraw

from .search import CollectionPage, Episode, SearchResult, normalize_title


logger = logging.getLogger("jiang_short_drama")
_ROUTE_PREFIX = "/short-drama"
_HLS_URI_RE = re.compile(r"URI=(?P<quote>['\"])(?P<url>[^'\"]+)(?P=quote)")


def _encode_hls_resource(url: str) -> str:
    return base64.urlsafe_b64encode(url.encode("utf-8")).decode("ascii").rstrip("=")


def _decode_hls_resource(value: str) -> str:
    try:
        padding = "=" * (-len(value) % 4)
        return base64.urlsafe_b64decode(value + padding).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return ""


def _is_proxyable_hls_url(parsed: SplitResult) -> bool:
    scheme = getattr(parsed, "scheme", "")
    hostname = getattr(parsed, "hostname", None)
    if scheme not in {"http", "https"} or not hostname:
        return False

    host = hostname.lower()
    if host == "localhost" or host.endswith(".localhost"):
        return False

    try:
        return ipaddress.ip_address(host).is_global
    except ValueError:
        return True


def _rewrite_hls_playlist(content: str, upstream_url: str, proxy_prefix: str) -> str:
    def proxy_url(value: str) -> str:
        return f"{proxy_prefix}{_encode_hls_resource(urljoin(upstream_url, value))}"

    rewritten: list[str] = []
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            leading = line[: len(line) - len(line.lstrip())]
            line = f"{leading}{proxy_url(stripped)}"
        elif "URI=" in line:
            line = _HLS_URI_RE.sub(
                lambda match: (
                    f"URI={match.group('quote')}"
                    f"{proxy_url(match.group('url'))}{match.group('quote')}"
                ),
                line,
            )
        rewritten.append(line)
    return "\n".join(rewritten) + ("\n" if content.endswith("\n") else "")


@dataclass(frozen=True, slots=True)
class PlaylistRecord:
    title: str
    source: str
    cover_url: str
    episodes: tuple[Episode, ...]
    expires_at: float


@dataclass(frozen=True, slots=True)
class VariantRecord:
    query_title: str
    variants: tuple[SearchResult, ...]
    expires_at: float


@dataclass(slots=True)
class CollectionRecord:
    query_title: str
    pages: dict[int, CollectionPage]
    expires_at: float


@dataclass(slots=True)
class CardCoverRecord:
    source_urls: tuple[str, ...]
    expires_at: float
    jpeg_data: bytes | None = None

    @property
    def source_url(self) -> str:
        return self.source_urls[0] if self.source_urls else ""


@dataclass(frozen=True, slots=True)
class TweetPageRecord:
    tweet: dict
    expires_at: float


class EpisodePlaylistStore:
    """Keep short-lived playlists behind unguessable tokens."""

    def __init__(self, ttl_seconds: int = 21_600, max_entries: int = 200) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self._items: dict[str, PlaylistRecord] = {}

    def put(self, result: SearchResult, now: float | None = None) -> str:
        current = time.monotonic() if now is None else now
        self._purge(current)
        while len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda key: self._items[key].expires_at)
            self._items.pop(oldest, None)

        token = secrets.token_urlsafe(12)
        self._items[token] = PlaylistRecord(
            title=result.title,
            source=result.source,
            cover_url=result.cover_url,
            episodes=result.episodes,
            expires_at=current + self.ttl_seconds,
        )
        return token

    def get(self, token: str, now: float | None = None) -> PlaylistRecord | None:
        current = time.monotonic() if now is None else now
        record = self._items.get(token)
        if record is None:
            return None
        if record.expires_at <= current:
            self._items.pop(token, None)
            return None
        return record

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


class VariantStore:
    """Keep short-lived version choices behind unguessable tokens."""

    def __init__(self, ttl_seconds: int = 21_600, max_entries: int = 200) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self._items: dict[str, VariantRecord] = {}

    def put(
        self,
        query_title: str,
        variants: tuple[SearchResult, ...],
        now: float | None = None,
    ) -> str:
        current = time.monotonic() if now is None else now
        self._purge(current)
        while len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda key: self._items[key].expires_at)
            self._items.pop(oldest, None)
        token = secrets.token_urlsafe(12)
        self._items[token] = VariantRecord(
            query_title=query_title,
            variants=variants,
            expires_at=current + self.ttl_seconds,
        )
        return token

    def get(self, token: str, now: float | None = None) -> VariantRecord | None:
        current = time.monotonic() if now is None else now
        record = self._items.get(token)
        if record is None:
            return None
        if record.expires_at <= current:
            self._items.pop(token, None)
            return None
        return record

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


class CollectionStore:
    """Keep lazy-loaded replay collections behind unguessable tokens."""

    def __init__(self, ttl_seconds: int = 21_600, max_entries: int = 200) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self._items: dict[str, CollectionRecord] = {}

    def put(
        self,
        query_title: str,
        first_page: CollectionPage,
        now: float | None = None,
    ) -> str:
        current = time.monotonic() if now is None else now
        self._purge(current)
        while len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda key: self._items[key].expires_at)
            self._items.pop(oldest, None)
        token = secrets.token_urlsafe(12)
        self._items[token] = CollectionRecord(
            query_title=query_title,
            pages={first_page.page: first_page},
            expires_at=current + self.ttl_seconds,
        )
        return token

    def get(self, token: str, now: float | None = None) -> CollectionRecord | None:
        current = time.monotonic() if now is None else now
        record = self._items.get(token)
        if record is None:
            return None
        if record.expires_at <= current:
            self._items.pop(token, None)
            return None
        return record

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


class CardCoverStore:
    """Keep first-item recommendation covers behind short-lived JPEG URLs."""

    def __init__(self, ttl_seconds: int = 21_600, max_entries: int = 200) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self._items: dict[str, CardCoverRecord] = {}

    def put(self, source_url: str, now: float | None = None) -> str:
        return self.put_many((source_url,), now=now)

    def put_many(
        self,
        source_urls: tuple[str, ...],
        now: float | None = None,
    ) -> str:
        current = time.monotonic() if now is None else now
        self._purge(current)
        while len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda key: self._items[key].expires_at)
            self._items.pop(oldest, None)
        token = secrets.token_urlsafe(12)
        self._items[token] = CardCoverRecord(
            source_urls=source_urls,
            expires_at=current + self.ttl_seconds,
        )
        return token

    def get(self, token: str, now: float | None = None) -> CardCoverRecord | None:
        current = time.monotonic() if now is None else now
        record = self._items.get(token)
        if record is None:
            return None
        if record.expires_at <= current:
            self._items.pop(token, None)
            return None
        return record

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


class TweetPageStore:
    """Keep short-lived tweet snapshots behind unguessable tokens."""

    def __init__(self, ttl_seconds: int = 21_600, max_entries: int = 500) -> None:
        self.ttl_seconds = max(60, int(ttl_seconds))
        self.max_entries = max(10, int(max_entries))
        self._items: dict[str, TweetPageRecord] = {}

    def put(self, tweet: dict, now: float | None = None) -> str:
        current = time.monotonic() if now is None else now
        self._purge(current)
        while len(self._items) >= self.max_entries:
            oldest = min(self._items, key=lambda key: self._items[key].expires_at)
            self._items.pop(oldest, None)
        token = secrets.token_urlsafe(12)
        self._items[token] = TweetPageRecord(
            tweet=copy.deepcopy(tweet),
            expires_at=current + self.ttl_seconds,
        )
        return token

    def get(self, token: str, now: float | None = None) -> TweetPageRecord | None:
        current = time.monotonic() if now is None else now
        record = self._items.get(token)
        if record is None:
            return None
        if record.expires_at <= current:
            self._items.pop(token, None)
            return None
        return record

    def _purge(self, now: float) -> None:
        expired = [key for key, value in self._items.items() if value.expires_at <= now]
        for key in expired:
            self._items.pop(key, None)


def validate_public_base_url(value: object) -> str:
    """Return a usable public origin/base path or an empty string."""
    text = str(value or "").strip().rstrip("/")
    if not text:
        return ""
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    if parsed.query or parsed.fragment:
        return ""
    return text


def _safe_remote_url(value: object) -> str:
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return ""
    return escape(text, quote=True)


def _normalize_twitter_avatar_url(value: object) -> str:
    """Use Twitter's image CDN instead of any Nitter proxy for avatars."""
    text = str(value or "").strip()
    parsed = urlsplit(text)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return text
    decoded_path = unquote(parsed.path)
    # Nitter mirrors expose avatars under this stable path, even when the
    # mirror hostname is custom or no longer contains the word "nitter".
    if decoded_path.startswith("/pic/profile_images/"):
        return urlunsplit(("https", "pbs.twimg.com", decoded_path[4:], "", ""))
    return text


def _fallback_cover_jpeg() -> bytes:
    image = Image.new("RGB", (720, 720), (21, 29, 38))
    draw = ImageDraw.Draw(image)
    draw.ellipse((180, 180, 540, 540), fill=(19, 170, 153))
    draw.polygon(((325, 270), (325, 450), (455, 360)), fill=(255, 255, 255))
    output = BytesIO()
    image.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue()


def _tweet_media_html(tweet: dict, *, css_class: str = "media") -> str:
    images = "".join(
        f'<img class="{css_class}" src="{url}" alt="推文图片" loading="lazy">'
        for value in (tweet.get("images") or [])
        if (url := _safe_remote_url(value))
    )
    previews = tweet.get("video_previews") or []
    videos = []
    for index, value in enumerate(tweet.get("videos") or []):
        url = _safe_remote_url(value)
        if not url:
            continue
        preview = previews[index] if index < len(previews) else {}
        poster = _safe_remote_url(
            preview.get("poster") if isinstance(preview, dict) else ""
        )
        poster_attr = f' poster="{poster}"' if poster else ""
        videos.append(
            f'<video class="{css_class} video-player" controls playsinline '
            f'preload="metadata"{poster_attr}>'
            f'<source src="{url}">'
            f'<a href="{url}" rel="noreferrer">打开视频</a>'
            '</video>'
        )
    return images + "".join(videos)


def _tweet_page_html(tweet: dict) -> str:
    username = escape(str(tweet.get("username") or "").lstrip("@"))
    screen_name = escape(str(tweet.get("screen_name") or username))
    avatar = _safe_remote_url(tweet.get("avatar"))
    date = escape(str(tweet.get("display_date") or tweet.get("date") or ""))
    original_text = escape(str(tweet.get("text") or ""))
    translated_text = escape(str(tweet.get("translated_text") or ""))
    display_text = translated_text or original_text
    paragraphs = "<br>".join(display_text.splitlines())
    original_html = ""
    if translated_text and translated_text != original_text and original_text:
        original_html = (
            '<section class="original"><div class="label">原文</div>'
            f'<div>{"<br>".join(original_text.splitlines())}</div></section>'
        )
    media = _tweet_media_html(tweet)
    quote = tweet.get("quote") or {}
    quote_html = ""
    if quote:
        quote_username = escape(str(quote.get("username") or "").lstrip("@"))
        quote_name = escape(str(quote.get("author") or quote_username))
        quote_original = escape(str(quote.get("text") or ""))
        quote_translated = escape(str(quote.get("translated_text") or ""))
        quote_text = "<br>".join((quote_translated or quote_original).splitlines())
        quote_original_html = ""
        if quote_translated and quote_translated != quote_original and quote_original:
            quote_original_html = (
                '<div class="quote-original"><span>原文</span><br>'
                f'{"<br>".join(quote_original.splitlines())}</div>'
            )
        quote_media = _tweet_media_html(quote, css_class="quote-media")
        quote_html = (
            '<section class="quote">'
            f'<strong>{quote_name}</strong> <span>@{quote_username}</span>'
            f'<p>{quote_text}</p>{quote_original_html}{quote_media}'
            '</section>'
        )
    retweet = tweet.get("retweet") or {}
    retweet_html = ""
    if retweet:
        retweeter = escape(
            str(
                retweet.get("retweeter_screen_name")
                or retweet.get("retweeter_username")
                or ""
            )
        )
        if retweeter:
            retweet_html = f'<div class="retweet-context">{retweeter} 转发</div>'
    avatar_html = (
        f'<img class="avatar" src="{avatar}" alt="">'
        if avatar
        else '<div class="avatar fallback" aria-hidden="true"></div>'
    )
    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>@{username} 的推文</title>
<style>
:root{{color-scheme:light dark;--bg:#fff;--fg:#0f1419;--muted:#536471;--line:#cfd9de;--link:#1d9bf0;--panel:#fff}}
@media(prefers-color-scheme:dark){{:root{{--bg:#000;--fg:#e7e9ea;--muted:#71767b;--line:#2f3336;--panel:#000}}}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--fg);font-family:Arial,"Microsoft YaHei",sans-serif;letter-spacing:0}}
main{{width:min(640px,100%);margin:0 auto;min-height:100vh;border-left:1px solid var(--line);border-right:1px solid var(--line);background:var(--panel)}}
.top{{height:54px;display:flex;align-items:center;padding:0 18px;border-bottom:1px solid var(--line);font-size:18px;font-weight:700}}
article{{padding:18px}}header{{display:flex;gap:12px;align-items:center}}.avatar{{width:48px;height:48px;border-radius:50%;object-fit:cover;background:#cfd9de}}
.identity{{min-width:0}}.name{{font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}}.handle,.date{{color:var(--muted);font-size:14px}}
.retweet-context{{margin-bottom:12px;color:var(--muted);font-size:13px;font-weight:600}}.body{{margin:18px 0 14px;font-size:18px;line-height:1.5;overflow-wrap:anywhere}}.media,.quote-media{{display:block;width:100%;max-height:720px;margin-top:12px;border:1px solid var(--line);border-radius:8px;object-fit:contain}}
.video-player{{background:#000}}.quote{{margin-top:14px;padding:12px;border:1px solid var(--line);border-radius:8px}}.quote span{{color:var(--muted)}}.quote p{{margin:8px 0 0;line-height:1.45}}
.original,.quote-original{{margin:14px 0;padding-top:12px;border-top:1px solid var(--line);color:var(--muted);font-size:14px;line-height:1.45}}.original .label{{margin-bottom:6px;font-weight:700}}
.footer{{padding-top:14px;border-top:1px solid var(--line);color:var(--muted);font-size:13px}}
</style></head><body><main><div class="top">推文</div><article>
{retweet_html}<header>{avatar_html}<div class="identity"><div class="name">{screen_name}</div><div class="handle">@{username}</div></div></header>
<div class="body">{paragraphs}</div>{original_html}{quote_html}{media}<div class="footer">🕒 {date or "时间未知"}</div>
</article></main></body></html>"""


class ShortDramaPlayerServer:
    def __init__(
        self,
        *,
        bind_host: str = "127.0.0.1",
        port: int = 6197,
        public_base_url: object = "",
        token_ttl_seconds: int = 21_600,
        max_playlists: int = 200,
    ) -> None:
        self.bind_host = str(bind_host or "127.0.0.1").strip()
        self.port = max(1, min(int(port), 65_535))
        self.public_base_url = validate_public_base_url(public_base_url)
        self.store = EpisodePlaylistStore(token_ttl_seconds, max_playlists)
        self.variant_store = VariantStore(token_ttl_seconds, max_playlists)
        self.collection_store = CollectionStore(token_ttl_seconds, max_playlists)
        self.cover_store = CardCoverStore(token_ttl_seconds, max_playlists)
        self.tweet_store = TweetPageStore(token_ttl_seconds, max_playlists * 2)
        self.collection_loader: (
            Callable[[str, int], Awaitable[CollectionPage]] | None
        ) = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None
        self.started = False

    @property
    def configured(self) -> bool:
        return bool(self.public_base_url)

    async def start(self) -> None:
        if self.started or not self.configured:
            return
        app = web.Application()
        app.router.add_get(f"{_ROUTE_PREFIX}/health", self._handle_health)
        app.router.add_get(f"{_ROUTE_PREFIX}/watch/{{token}}", self._handle_watch)
        app.router.add_get(f"{_ROUTE_PREFIX}/api/{{token}}", self._handle_api)
        app.router.add_get(
            f"{_ROUTE_PREFIX}/hls/{{token}}/{{episode}}/{{resource}}",
            self._handle_hls_proxy,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/variants/{{token}}",
            self._handle_variants,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/search/{{token}}",
            self._handle_search,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/people/{{token}}",
            self._handle_people,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/choose/{{token}}/{{index}}",
            self._handle_choose,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/recommendations/{{token}}",
            self._handle_recommendations,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/cover/{{token}}.jpg",
            self._handle_card_cover,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/twitter/{{token}}",
            self._handle_tweet,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/collection/{{token}}",
            self._handle_collection,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/collection-api/{{token}}",
            self._handle_collection_api,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/collection-choose/{{token}}/{{page}}/{{index}}",
            self._handle_collection_choose,
        )
        app.router.add_get(
            f"{_ROUTE_PREFIX}/collection.js",
            self._handle_collection_javascript,
        )
        app.router.add_get(f"{_ROUTE_PREFIX}/player.js", self._handle_javascript)

        self._runner = web.AppRunner(app, access_log=None)
        try:
            await self._runner.setup()
            self._site = web.TCPSite(
                self._runner,
                host=self.bind_host,
                port=self.port,
            )
            await self._site.start()
        except Exception as exc:
            logger.error(
                "[SHORT_DRAMA] episode player failed to listen on %s:%s: %s: %s",
                self.bind_host,
                self.port,
                type(exc).__name__,
                exc,
            )
            await self.stop()
            return

        self.started = True
        logger.info(
            "[SHORT_DRAMA] episode player listening on %s:%s, public base=%s",
            self.bind_host,
            self.port,
            self.public_base_url,
        )

    async def stop(self) -> None:
        self.started = False
        if self._site is not None:
            await self._site.stop()
            self._site = None
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def create_watch_url(
        self,
        result: SearchResult,
        selected: Episode,
        *,
        force_episode: bool = False,
    ) -> str | None:
        if not self.started or not self.public_base_url or not result.episodes:
            return None
        try:
            selected_index = result.episodes.index(selected)
        except ValueError:
            selected_index = 0
        token = self.store.put(result)
        query_params = {"ep": selected_index + 1}
        if force_episode:
            query_params["force"] = 1
        query = urlencode(query_params)
        return (
            f"{self.public_base_url}{_ROUTE_PREFIX}/watch/{token}?{query}"
        )

    def create_variants_url(
        self,
        query_title: str,
        variants: tuple[SearchResult, ...],
    ) -> str | None:
        if (
            not self.started
            or not self.public_base_url
            or len(variants) < 2
        ):
            return None
        token = self.variant_store.put(query_title, variants)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/variants/{token}"

    def create_search_url(
        self,
        query_title: str,
        media_label: str,
        results: tuple[SearchResult, ...],
    ) -> str | None:
        """Create a short-lived relevance-ranked search result page."""
        if not self.started or not self.public_base_url or not results:
            return None
        heading = f"{media_label or '影视资源'}｜搜索“{query_title or '影视'}”"
        token = self.variant_store.put(heading, results)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/search/{token}"

    def create_people_url(
        self,
        query_title: str,
        media_label: str,
        results: tuple[SearchResult, ...],
    ) -> str | None:
        if not self.started or not self.public_base_url or not results:
            return None
        token = self.variant_store.put(f"{query_title}相关{media_label}", results)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/people/{token}"

    def create_collection_url(
        self,
        query_title: str,
        first_page: CollectionPage,
    ) -> str | None:
        if (
            not self.started
            or not self.public_base_url
            or not first_page.items
        ):
            return None
        token = self.collection_store.put(query_title, first_page)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/collection/{token}"

    def create_recommendations_url(
        self,
        recommendations: tuple[SearchResult, ...],
        heading: str = "最新短剧推荐",
    ) -> str | None:
        if not self.started or not self.public_base_url or not recommendations:
            return None
        token = self.variant_store.put(heading, recommendations)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/recommendations/{token}"

    def create_card_cover_url(self, source_url: str) -> str | None:
        if not self.started or not self.public_base_url:
            return None
        source_url = _normalize_twitter_avatar_url(source_url)
        parsed = urlsplit(str(source_url or "").strip())
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            return None
        token = self.cover_store.put(source_url)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/cover/{token}.jpg"

    def create_recommendation_cover_url(
        self,
        source_urls: tuple[str, ...],
    ) -> str | None:
        if not self.started or not self.public_base_url:
            return None
        candidates: list[str] = []
        seen: set[str] = set()
        for value in source_urls:
            source_url = str(value or "").strip()
            parsed = urlsplit(source_url)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or source_url in seen
            ):
                continue
            seen.add(source_url)
            candidates.append(source_url)
            if len(candidates) >= 6:
                break
        token = self.cover_store.put_many(tuple(candidates))
        return f"{self.public_base_url}{_ROUTE_PREFIX}/cover/{token}.jpg"

    def create_tweet_url(self, tweet_info: dict) -> str | None:
        if not self.started or not self.public_base_url or not tweet_info:
            return None
        token = self.tweet_store.put(tweet_info)
        return f"{self.public_base_url}{_ROUTE_PREFIX}/twitter/{token}"

    async def _handle_health(self, request: web.Request) -> web.Response:
        return web.json_response(
            {"ok": True, "plugin": "jiang_short_drama", "player": True},
            headers={"Cache-Control": "no-store"},
        )

    async def _load_card_cover_jpeg(
        self,
        source_urls: tuple[str, ...],
    ) -> bytes:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
        }
        timeout = aiohttp.ClientTimeout(total=None, connect=5)
        try:
            async with asyncio.timeout(12):
                async with aiohttp.ClientSession(
                    timeout=timeout,
                    headers=headers,
                    trust_env=False,
                ) as session:
                    for source_url in source_urls:
                        try:
                            async with session.get(
                                source_url,
                                allow_redirects=True,
                            ) as response:
                                if response.status != 200:
                                    logger.warning(
                                        "[SHORT_DRAMA] cover candidate returned %s: %s",
                                        response.status,
                                        source_url,
                                    )
                                    continue
                                raw = await response.read()
                            if not raw or len(raw) > 5_000_000:
                                logger.warning(
                                    "[SHORT_DRAMA] cover candidate has invalid size: %s",
                                    source_url,
                                )
                                continue
                            with Image.open(BytesIO(raw)) as source_image:
                                image = source_image.convert("RGB")
                                image.thumbnail((720, 720), Image.Resampling.LANCZOS)
                                output = BytesIO()
                                image.save(
                                    output,
                                    format="JPEG",
                                    quality=88,
                                    optimize=True,
                                )
                            return output.getvalue()
                        except (aiohttp.ClientError, OSError, ValueError):
                            logger.warning(
                                "[SHORT_DRAMA] cover candidate conversion failed: %s",
                                source_url,
                            )
        except (asyncio.TimeoutError, aiohttp.ClientError):
            logger.warning("[SHORT_DRAMA] cover candidate loading timed out")
        return _fallback_cover_jpeg()

    async def _handle_card_cover(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.cover_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="封面链接已失效")
        if record.jpeg_data is None:
            record.jpeg_data = await self._load_card_cover_jpeg(record.source_urls)
        return web.Response(
            body=record.jpeg_data,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=21600"},
        )

    async def _handle_tweet(self, request: web.Request) -> web.Response:
        record = self.tweet_store.get(request.match_info["token"])
        if record is None:
            raise web.HTTPNotFound(text="推文链接已失效")
        return web.Response(
            text=_tweet_page_html(record.tweet),
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    async def _handle_watch(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="播放链接已失效，请回到微信重新搜索")
        html = _player_html(record.title)
        return web.Response(
            text=html,
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    def _hls_fallback_url(
        self,
        token: str,
        episode_index: int,
        upstream_url: str,
    ) -> str | None:
        parsed = urlsplit(str(upstream_url or "").strip())
        if not _is_proxyable_hls_url(parsed):
            return None
        if self.store.get(token) is None or episode_index < 0:
            return None
        resource = _encode_hls_resource(upstream_url)
        return (
            f"{self.public_base_url}{_ROUTE_PREFIX}/hls/{token}/"
            f"{episode_index + 1}/{resource}"
        )

    def _resolve_hls_upstream(
        self,
        token: str,
        episode_number: int,
        encoded_resource: str,
    ) -> str | None:
        record = self.store.get(token)
        if (
            record is None
            or episode_number < 1
            or episode_number > len(record.episodes)
        ):
            return None

        episode_url = record.episodes[episode_number - 1].url
        episode = urlsplit(episode_url)
        target_url = _decode_hls_resource(encoded_resource)
        target = urlsplit(target_url)
        if (
            not _is_proxyable_hls_url(episode)
            or not _is_proxyable_hls_url(target)
            or target.scheme != episode.scheme
            or target.hostname != episode.hostname
            or target.port != episode.port
            or target.username is not None
            or target.password is not None
            or target.fragment
        ):
            return None

        episode_path = posixpath.normpath(unquote(episode.path))
        target_path = posixpath.normpath(unquote(target.path))
        episode_directory = posixpath.dirname(episode_path).rstrip("/") + "/"
        if not target_path.startswith(episode_directory):
            return None
        return target_url

    async def _handle_hls_proxy(self, request: web.Request) -> web.StreamResponse:
        try:
            episode_number = int(request.match_info["episode"])
        except ValueError:
            raise web.HTTPNotFound(text="播放资源不存在")
        upstream_url = self._resolve_hls_upstream(
            request.match_info["token"],
            episode_number,
            request.match_info["resource"],
        )
        if upstream_url is None:
            raise web.HTTPNotFound(text="播放资源不存在")

        forwarded_headers = {
            name: request.headers[name]
            for name in ("Range", "If-Range")
            if name in request.headers
        }
        forwarded_headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                    "Chrome/137.0 Mobile Safari/537.36"
                ),
                "Accept": "*/*",
            }
        )
        timeout = aiohttp.ClientTimeout(total=None, connect=8, sock_read=30)
        try:
            async with aiohttp.ClientSession(
                timeout=timeout,
                trust_env=False,
            ) as session:
                async with session.get(
                    upstream_url,
                    headers=forwarded_headers,
                    allow_redirects=False,
                    ssl=False,
                ) as upstream:
                    if 300 <= upstream.status < 400:
                        raise web.HTTPBadGateway(text="量子资源线路重定向异常")
                    if upstream.status >= 400:
                        raise web.HTTPBadGateway(text="量子资源线路读取失败")

                    response_headers = {
                        name: upstream.headers[name]
                        for name in (
                            "Content-Length",
                            "Content-Range",
                            "Accept-Ranges",
                            "ETag",
                            "Last-Modified",
                        )
                        if name in upstream.headers
                    }
                    response_headers["Access-Control-Allow-Origin"] = "*"
                    content_type = upstream.headers.get("Content-Type", "")
                    is_playlist = (
                        urlsplit(upstream_url).path.lower().endswith(".m3u8")
                        or "mpegurl" in content_type.lower()
                    )
                    if is_playlist:
                        declared_size = int(upstream.headers.get("Content-Length", "0") or 0)
                        if declared_size > 5_000_000:
                            raise web.HTTPBadGateway(text="播放列表大小异常")
                        raw = await upstream.content.read(5_000_001)
                        if len(raw) > 5_000_000:
                            raise web.HTTPBadGateway(text="播放列表大小异常")
                        charset = upstream.charset or "utf-8"
                        playlist = raw.decode(charset, errors="replace")
                        prefix = (
                            f"{self.public_base_url}{_ROUTE_PREFIX}/hls/"
                            f"{request.match_info['token']}/{episode_number}/"
                        )
                        rewritten = _rewrite_hls_playlist(
                            playlist,
                            upstream_url,
                            prefix,
                        )
                        return web.Response(
                            text=rewritten,
                            status=upstream.status,
                            content_type="application/vnd.apple.mpegurl",
                            charset="utf-8",
                            headers={
                                "Access-Control-Allow-Origin": "*",
                                "Cache-Control": "private, max-age=30",
                            },
                        )

                    stream = web.StreamResponse(
                        status=upstream.status,
                        headers=response_headers,
                    )
                    if content_type:
                        stream.content_type = content_type.split(";", 1)[0]
                    await stream.prepare(request)
                    async for chunk in upstream.content.iter_chunked(256 * 1024):
                        await stream.write(chunk)
                    await stream.write_eof()
                    return stream
        except web.HTTPException:
            raise
        except asyncio.TimeoutError:
            raise web.HTTPGatewayTimeout(text="量子资源线路读取超时")
        except (aiohttp.ClientError, OSError) as exc:
            logger.warning(
                "[SHORT_DRAMA] Liangzi HLS fallback failed for %s: %s",
                upstream_url,
                exc,
            )
            raise web.HTTPBadGateway(text="量子资源线路读取失败")

    async def _handle_api(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.store.get(token)
        if record is None:
            return web.json_response(
                {"ok": False, "error": "播放链接已失效，请回到微信重新搜索"},
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {
                "ok": True,
                "title": record.title,
                "source": record.source,
                "cover": record.cover_url,
                "episodes": [
                    {
                        "name": episode.name,
                        "url": episode.url,
                        "fallback_url": self._hls_fallback_url(
                            token,
                            index,
                            episode.url,
                        ),
                    }
                    for index, episode in enumerate(record.episodes)
                ],
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _handle_variants(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.variant_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="版本选择链接已失效，请回到微信重新搜索")
        return web.Response(
            text=_variant_html(record, token),
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    async def _handle_search(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.variant_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="搜索结果链接已失效，请回到微信重新搜索")
        return web.Response(
            text=_search_html(record, token),
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    async def _handle_people(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.variant_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="相关作品链接已失效，请回到微信重新搜索")
        return web.Response(
            text=_people_html(record, token),
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    async def _handle_choose(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.variant_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="版本选择链接已失效，请回到微信重新搜索")
        try:
            index = int(request.match_info["index"]) - 1
        except ValueError:
            raise web.HTTPNotFound(text="无效的版本编号")
        if index < 0 or index >= len(record.variants):
            raise web.HTTPNotFound(text="无效的版本编号")
        result = record.variants[index]
        playlist_token = self.store.put(result)
        destination = (
            f"{self.public_base_url}{_ROUTE_PREFIX}/watch/{playlist_token}?ep=1"
        )
        raise web.HTTPFound(location=destination)

    async def _handle_recommendations(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.variant_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="推荐链接已失效，请回到微信重新获取")
        return web.Response(
            text=_recommendation_html(record, token),
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    async def _load_collection_page(
        self,
        record: CollectionRecord,
        page: int,
    ) -> CollectionPage | None:
        cached = record.pages.get(page)
        if cached is not None:
            return cached
        if self.collection_loader is None:
            return None
        loaded = await self.collection_loader(record.query_title, page)
        seen = {
            normalize_title(item.title)
            for existing_page, existing in record.pages.items()
            if existing_page != page
            for item in existing.items
        }
        if seen:
            loaded = CollectionPage(
                query=loaded.query,
                page=loaded.page,
                items=tuple(
                    item
                    for item in loaded.items
                    if normalize_title(item.title) not in seen
                ),
                has_more=loaded.has_more,
            )
        record.pages[page] = loaded
        return loaded

    async def _handle_collection(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.collection_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="赛事回放链接已失效，请回到微信重新搜索")
        return web.Response(
            text=_collection_html(record.query_title),
            content_type="text/html",
            charset="utf-8",
            headers=_page_headers(),
        )

    async def _handle_collection_api(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.collection_store.get(token)
        if record is None:
            return web.json_response(
                {"ok": False, "error": "赛事回放链接已失效，请重新搜索"},
                status=404,
                headers={"Cache-Control": "no-store"},
            )
        try:
            page = max(1, int(request.query.get("page", "1")))
        except ValueError:
            page = 1
        collection_page = await self._load_collection_page(record, page)
        if collection_page is None:
            return web.json_response(
                {"ok": False, "error": "暂时无法加载更多回放"},
                status=503,
                headers={"Cache-Control": "no-store"},
            )
        return web.json_response(
            {
                "ok": True,
                "query": record.query_title,
                "page": page,
                "has_more": collection_page.has_more,
                "items": [
                    {
                        "title": item.title,
                        "source": item.source,
                        "cover": item.cover_url,
                        "category": item.category,
                        "remarks": item.remarks,
                        "episodes": len(item.episodes),
                        "choose": (
                            f"../collection-choose/{quote(token, safe='')}/"
                            f"{page}/{index}"
                        ),
                    }
                    for index, item in enumerate(collection_page.items, start=1)
                ],
            },
            headers={"Cache-Control": "private, no-store"},
        )

    async def _handle_collection_choose(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        record = self.collection_store.get(token)
        if record is None:
            raise web.HTTPNotFound(text="赛事回放链接已失效，请回到微信重新搜索")
        try:
            page = max(1, int(request.match_info["page"]))
            index = int(request.match_info["index"]) - 1
        except ValueError:
            raise web.HTTPNotFound(text="无效的回放编号")
        collection_page = await self._load_collection_page(record, page)
        if (
            collection_page is None
            or index < 0
            or index >= len(collection_page.items)
        ):
            raise web.HTTPNotFound(text="无效的回放编号")
        result = collection_page.items[index]
        playlist_token = self.store.put(result)
        destination = (
            f"{self.public_base_url}{_ROUTE_PREFIX}/watch/{playlist_token}?ep=1"
        )
        raise web.HTTPFound(location=destination)

    async def _handle_collection_javascript(
        self,
        request: web.Request,
    ) -> web.Response:
        return web.Response(
            text=_COLLECTION_JAVASCRIPT,
            content_type="application/javascript",
            charset="utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )

    async def _handle_javascript(self, request: web.Request) -> web.Response:
        return web.Response(
            text=_PLAYER_JAVASCRIPT,
            content_type="application/javascript",
            charset="utf-8",
            headers={"Cache-Control": "public, max-age=300"},
        )


def _page_headers() -> dict[str, str]:
    return {
        "Cache-Control": "private, no-store",
        "Content-Security-Policy": (
            "default-src 'self'; "
            "script-src 'self' https://cdn.jsdelivr.net; "
            "style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: http: https:; "
            "media-src 'self' blob: http: https:; "
            "connect-src 'self' blob: http: https:"
        ),
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "SAMEORIGIN",
    }


def _search_html(record: VariantRecord, token: str) -> str:
    """Render a relevance-ranked search result page before playback."""
    safe_token = quote(token, safe="")
    safe_heading = escape(record.query_title or "影视搜索")
    cards: list[str] = []
    for index, result in enumerate(record.variants, start=1):
        title = escape(result.title or "影视作品")
        category = escape(result.category or "类型未知")
        year = escape(f"{result.year}年" if result.year else "年份未知")
        source = escape(result.source or "未知来源")
        actors = [
            value.strip()
            for value in re.split(r"[,，、/]+", result.actor)
            if value.strip()
        ][:2]
        actor_text = escape("、".join(actors)) if actors else ""
        cover = str(result.cover_url or "").strip()
        parsed_cover = urlsplit(cover)
        cover_html = ""
        if parsed_cover.scheme in {"http", "https"} and parsed_cover.netloc:
            cover_html = (
                f'<img src="{escape(cover, quote=True)}" alt="{title}封面" '
                'loading="lazy" referrerpolicy="no-referrer">'
            )
        tags = "".join(
            f"<span>{value}</span>"
            for value in (
                year,
                category,
                f"{len(result.episodes)}集"
                if len(result.episodes) > 1
                else "完整版",
            )
            if value
        )
        actor_html = f'<div class="meta">主演：{actor_text}</div>' if actor_text else ""
        match_label = "精准匹配" if result.score >= 10_000 else "高相关"
        cards.append(
            f"""
      <a class="search-card" href="../choose/{safe_token}/{index}">
        <div class="cover"><span>{escape((result.title or "剧")[0])}</span>{cover_html}<b>{index}</b></div>
        <div class="info">
          <div class="title"><strong>{title}</strong><em>{match_label}</em></div>
          <div class="tags">{tags}</div>
          {actor_html}
          <div class="source">{source}</div>
          <div class="play">进入播放页 <span>›</span></div>
        </div>
      </a>"""
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>{safe_heading}</title>
  <style>
    :root {{ color-scheme:dark; font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif; --accent:#20c5a5; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:#0b0c0f; color:#f4f6f8; }}
    main {{ width:min(100%,920px); margin:0 auto; padding:18px 12px max(28px,env(safe-area-inset-bottom)); }}
    header {{ margin:0 2px 16px; }}
    h1 {{ margin:0 0 6px; font-size:clamp(23px,6vw,33px); line-height:1.3; overflow-wrap:anywhere; }}
    header p {{ margin:0; color:#9298a2; font-size:14px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .search-card {{ display:grid; grid-template-columns:112px minmax(0,1fr); min-height:174px; overflow:hidden; border:1px solid #292d34; border-radius:15px; color:inherit; background:#17191e; text-decoration:none; box-shadow:0 8px 24px rgba(0,0,0,.18); cursor:pointer; transition:border-color .2s ease, transform .2s ease, box-shadow .2s ease; }}
    .cover {{ position:relative; min-height:174px; overflow:hidden; display:flex; align-items:center; justify-content:center; color:#68717d; background:linear-gradient(145deg,#232a33,#111419); font-size:40px; font-weight:750; }}
    .cover img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
    .cover b {{ position:absolute; top:8px; left:8px; min-width:27px; height:27px; padding:0 7px; border-radius:9px; color:#061813; background:var(--accent); font-size:13px; line-height:27px; text-align:center; box-shadow:0 3px 10px rgba(0,0,0,.25); }}
    .info {{ display:flex; min-width:0; flex-direction:column; padding:12px 12px 10px; }}
    .title {{ display:flex; align-items:flex-start; justify-content:space-between; gap:7px; min-width:0; }}
    .title strong {{ display:-webkit-box; min-width:0; overflow:hidden; font-size:16px; line-height:1.38; -webkit-box-orient:vertical; -webkit-line-clamp:2; }}
    .title em {{ flex:0 0 auto; padding:3px 5px; border-radius:6px; color:#061813; background:#8ce8d3; font-size:10px; font-style:normal; font-weight:750; white-space:nowrap; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:9px; }}
    .tags span {{ padding:3px 6px; border:1px solid #343943; border-radius:6px; color:#c8cdd4; background:#202329; font-size:11px; }}
    .meta,.source {{ overflow:hidden; margin-top:8px; color:#9399a3; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }}
    .source {{ margin-top:4px; color:#6f7681; }}
    .play {{ display:flex; align-items:center; justify-content:space-between; margin-top:auto; padding-top:8px; color:var(--accent); font-size:13px; font-weight:700; }}
    .play span {{ font-size:23px; line-height:14px; }}
    .tip {{ margin:16px 2px 0; color:#717883; font-size:12px; text-align:center; }}
    @media (max-width:680px) {{
      main {{ padding:16px 10px max(24px,env(safe-area-inset-bottom)); }}
      .grid {{ grid-template-columns:1fr; gap:10px; }}
      .search-card {{ grid-template-columns:108px minmax(0,1fr); min-height:164px; border-radius:13px; }}
      .cover {{ min-height:164px; }}
    }}
    @media (hover:hover) {{ .search-card:hover {{ border-color:rgba(32,197,165,.7); transform:translateY(-1px); }} }}
    .search-card:focus-visible {{ outline:2px solid var(--accent); outline-offset:3px; }}
  </style>
</head>
<body>
  <main>
    <header><h1>{safe_heading}</h1><p>共 {len(record.variants)} 个结果，按相关性排序；点击作品进入播放页</p></header>
    <section class="grid">{''.join(cards)}</section>
    <p class="tip">优先展示标题最匹配且可播放的资源；同名版本可根据年份、类型和主演判断。</p>
  </main>
</body>
</html>"""


def _variant_html(record: VariantRecord, token: str) -> str:
    safe_query = escape(record.query_title or "短剧")
    safe_token = quote(token, safe="")
    cards: list[str] = []
    for index, result in enumerate(record.variants, start=1):
        title = escape(result.title or record.query_title)
        category = escape(result.category or "类型未知")
        year = escape(f"{result.year}年" if result.year else "年份未知")
        source = escape(result.source or "未知来源")
        actors = [
            value.strip()
            for value in re.split(r"[,，、/]+", result.actor)
            if value.strip()
        ][:2]
        actor_text = escape("、".join(actors)) if actors else "演员信息暂无"
        cover = str(result.cover_url or "").strip()
        parsed_cover = urlsplit(cover)
        cover_html = ""
        if parsed_cover.scheme in {"http", "https"} and parsed_cover.netloc:
            cover_html = (
                f'<img src="{escape(cover, quote=True)}" alt="{title}封面" '
                'loading="lazy" referrerpolicy="no-referrer">'
            )
        cards.append(
            f"""
      <a class="version-card" href="../choose/{safe_token}/{index}">
        <div class="cover"><span>{escape((result.title or "剧")[0])}</span>{cover_html}</div>
        <div class="info">
          <div class="title"><span class="number">{index}</span><strong>{title}</strong></div>
          <div class="tags"><span>{year}</span><span>{category}</span><span>{len(result.episodes)}集</span></div>
          <div class="actor">{actor_text}</div>
          <div class="source">{source}</div>
          <div class="play">播放此版本 <span>›</span></div>
        </div>
      </a>"""
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>选择《{safe_query}》的版本</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui,-apple-system,"Microsoft YaHei",sans-serif; --accent:#20c5a5; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:#0b0c0f; color:#f4f6f8; }}
    main {{ width:min(100%,920px); margin:0 auto; padding:20px 14px max(28px,env(safe-area-inset-bottom)); }}
    header {{ margin:0 2px 18px; }}
    h1 {{ margin:0 0 7px; font-size:clamp(22px,5.8vw,32px); line-height:1.3; }}
    header p {{ margin:0; color:#9298a2; font-size:14px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .version-card {{ display:grid; grid-template-columns:minmax(92px,38%) 1fr; min-height:164px; overflow:hidden; border:1px solid #292d34; border-radius:15px; color:inherit; background:#17191e; box-shadow:0 8px 24px rgba(0,0,0,.2); text-decoration:none; }}
    .cover {{ position:relative; min-height:164px; overflow:hidden; display:flex; align-items:center; justify-content:center; color:#68717d; background:linear-gradient(145deg,#232a33,#111419); font-size:42px; font-weight:750; }}
    .cover img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
    .info {{ display:flex; min-width:0; flex-direction:column; padding:12px 12px 10px; }}
    .title {{ display:flex; align-items:flex-start; gap:7px; min-width:0; }}
    .title strong {{ display:-webkit-box; overflow:hidden; font-size:16px; line-height:1.38; -webkit-box-orient:vertical; -webkit-line-clamp:2; }}
    .number {{ flex:0 0 auto; min-width:22px; height:22px; padding:0 5px; border-radius:7px; color:#061813; background:var(--accent); font-size:13px; font-weight:750; line-height:22px; text-align:center; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:9px; }}
    .tags span {{ padding:3px 6px; border:1px solid #343943; border-radius:6px; color:#c8cdd4; background:#202329; font-size:11px; }}
    .actor,.source {{ overflow:hidden; margin-top:7px; color:#9399a3; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }}
    .source {{ margin-top:3px; color:#6f7681; }}
    .play {{ display:flex; align-items:center; justify-content:space-between; margin-top:auto; padding-top:8px; color:var(--accent); font-size:13px; font-weight:650; }}
    .play span {{ font-size:23px; line-height:14px; }}
    .tip {{ margin:16px 2px 0; color:#717883; font-size:12px; text-align:center; }}
    @media (max-width:620px) {{
      main {{ padding:16px 10px max(22px,env(safe-area-inset-bottom)); }}
      header {{ margin-bottom:14px; }}
      .grid {{ grid-template-columns:1fr; gap:10px; }}
      .version-card {{ grid-template-columns:118px 1fr; min-height:168px; border-radius:13px; }}
      .cover {{ min-height:168px; }}
    }}
    @media (hover:hover) {{ .version-card:hover {{ border-color:rgba(32,197,165,.7); transform:translateY(-1px); }} }}
  </style>
</head>
<body>
  <main>
    <header><h1>选择《{safe_query}》的版本</h1><p>共 {len(record.variants)} 个可播放版本，点击封面或卡片即可观看</p></header>
    <section class="grid">{''.join(cards)}</section>
    <p class="tip">版本信息来自资源站；如封面或年份有误，请根据演员和集数判断。</p>
  </main>
</body>
</html>"""


def _people_html(record: VariantRecord, token: str) -> str:
    safe_token = quote(token, safe="")
    safe_heading = escape(record.query_title or "相关作品")
    cards: list[str] = []
    for index, result in enumerate(record.variants, start=1):
        title = escape(result.title or "影视作品")
        category = escape(result.category or "类型未知")
        year = escape(f"{result.year}年" if result.year else "年份未知")
        source = escape(result.source or "未知来源")
        actors = [
            value.strip()
            for value in re.split(r"[,，、/]+", result.actor)
            if value.strip()
        ][:3]
        directors = [
            value.strip()
            for value in re.split(r"[,，、/]+", result.director)
            if value.strip()
        ][:2]
        actor_text = escape("、".join(actors)) if actors else "暂无"
        director_text = escape("、".join(directors)) if directors else "暂无"
        cover = str(result.cover_url or "").strip()
        parsed_cover = urlsplit(cover)
        cover_html = ""
        if parsed_cover.scheme in {"http", "https"} and parsed_cover.netloc:
            cover_html = (
                f'<img src="{escape(cover, quote=True)}" alt="{title}封面" '
                'loading="lazy" referrerpolicy="no-referrer">'
            )
        cards.append(
            f"""
      <a class="work-card" href="../choose/{safe_token}/{index}">
        <div class="cover"><span>{escape((result.title or "影")[0])}</span>{cover_html}</div>
        <div class="info">
          <div class="title"><span class="number">{index}</span><strong>{title}</strong></div>
          <div class="tags"><span>{year}</span><span>{category}</span><span>{len(result.episodes)}集</span></div>
          <div class="meta">演员：{actor_text}</div>
          <div class="meta">导演：{director_text}</div>
          <div class="source">{source}</div>
          <div class="play">播放此作品 <span>›</span></div>
        </div>
      </a>"""
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>{safe_heading}</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui,-apple-system,"Microsoft YaHei",sans-serif; --accent:#20c5a5; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:#0b0c0f; color:#f4f6f8; }}
    main {{ width:min(100%,920px); margin:0 auto; padding:20px 14px max(28px,env(safe-area-inset-bottom)); }}
    header {{ margin:0 2px 18px; }}
    h1 {{ margin:0 0 7px; font-size:clamp(22px,5.8vw,32px); line-height:1.3; }}
    header p {{ margin:0; color:#9298a2; font-size:14px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .work-card {{ display:grid; grid-template-columns:minmax(92px,38%) 1fr; min-height:174px; overflow:hidden; border:1px solid #292d34; border-radius:15px; color:inherit; background:#17191e; box-shadow:0 8px 24px rgba(0,0,0,.2); text-decoration:none; }}
    .cover {{ position:relative; min-height:174px; overflow:hidden; display:flex; align-items:center; justify-content:center; color:#68717d; background:linear-gradient(145deg,#232a33,#111419); font-size:42px; font-weight:750; }}
    .cover img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
    .info {{ display:flex; min-width:0; flex-direction:column; padding:12px 12px 10px; }}
    .title {{ display:flex; align-items:flex-start; gap:7px; min-width:0; }}
    .title strong {{ display:-webkit-box; overflow:hidden; font-size:16px; line-height:1.38; -webkit-box-orient:vertical; -webkit-line-clamp:2; }}
    .number {{ flex:0 0 auto; min-width:22px; height:22px; padding:0 5px; border-radius:7px; color:#061813; background:var(--accent); font-size:13px; font-weight:750; line-height:22px; text-align:center; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:9px; }}
    .tags span {{ padding:3px 6px; border:1px solid #343943; border-radius:6px; color:#c8cdd4; background:#202329; font-size:11px; }}
    .meta,.source {{ overflow:hidden; margin-top:7px; color:#9399a3; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }}
    .source {{ margin-top:3px; color:#6f7681; }}
    .play {{ display:flex; align-items:center; justify-content:space-between; margin-top:auto; padding-top:8px; color:var(--accent); font-size:13px; font-weight:650; }}
    .play span {{ font-size:23px; line-height:14px; }}
    .tip {{ margin:16px 2px 0; color:#717883; font-size:12px; text-align:center; }}
    @media (max-width:620px) {{
      main {{ padding:16px 10px max(22px,env(safe-area-inset-bottom)); }}
      header {{ margin-bottom:14px; }}
      .grid {{ grid-template-columns:1fr; gap:10px; }}
      .work-card {{ grid-template-columns:118px 1fr; min-height:178px; border-radius:13px; }}
      .cover {{ min-height:178px; }}
    }}
    @media (hover:hover) {{ .work-card:hover {{ border-color:rgba(32,197,165,.7); transform:translateY(-1px); }} }}
  </style>
</head>
<body>
  <main>
    <header><h1>{safe_heading}</h1><p>共 {len(record.variants)} 部，按演员/导演、年份和可播放完整度排序</p></header>
    <section class="grid">{''.join(cards)}</section>
    <p class="tip">信息来自资源站；同名或客串内容请根据演员、导演和年份判断。</p>
  </main>
</body>
</html>"""


def _recommendation_html(record: VariantRecord, token: str) -> str:
    safe_token = quote(token, safe="")
    safe_heading = escape(record.query_title or "最新资源推荐")
    cards: list[str] = []
    for index, result in enumerate(record.variants, start=1):
        title = escape(result.title or "短剧")
        category = escape(result.category or "短剧")
        year = escape(result.year) if result.year else ""
        source = escape(result.source or "未知来源")
        cover = str(result.cover_url or "").strip()
        parsed_cover = urlsplit(cover)
        cover_html = ""
        if parsed_cover.scheme in {"http", "https"} and parsed_cover.netloc:
            cover_html = (
                f'<img src="{escape(cover, quote=True)}" alt="{title}封面" '
                'loading="lazy" referrerpolicy="no-referrer">'
            )
        tags = "".join(
            f"<span>{value}</span>"
            for value in (
                year,
                category,
                f"{len(result.episodes)}集" if len(result.episodes) > 1 else "完整版",
            )
            if value
        )
        cards.append(
            f"""
      <a class="recommend-card" href="../choose/{safe_token}/{index}">
        <div class="cover"><span>{escape((result.title or "剧")[0])}</span>{cover_html}<b>{index}</b></div>
        <div class="info">
          <strong>{title}</strong>
          <div class="tags">{tags}</div>
          <div class="source">{source}</div>
          <div class="play">点击播放 <span>›</span></div>
        </div>
      </a>"""
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>{safe_heading}</title>
  <style>
    :root {{ color-scheme:dark; font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif; --accent:#20c5a5; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:#0b0c0f; color:#f4f6f8; }}
    main {{ width:min(100%,920px); margin:0 auto; padding:18px 12px max(28px,env(safe-area-inset-bottom)); }}
    header {{ margin:0 2px 16px; }}
    h1 {{ margin:0 0 6px; font-size:clamp(24px,6vw,34px); line-height:1.25; }}
    header p {{ margin:0; color:#9298a2; font-size:14px; }}
    .grid {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }}
    .recommend-card {{ display:grid; grid-template-columns:112px minmax(0,1fr); min-height:158px; overflow:hidden; border:1px solid #292d34; border-radius:15px; color:inherit; background:#17191e; text-decoration:none; box-shadow:0 8px 24px rgba(0,0,0,.18); }}
    .cover {{ position:relative; min-height:158px; overflow:hidden; display:flex; align-items:center; justify-content:center; color:#68717d; background:linear-gradient(145deg,#232a33,#111419); font-size:40px; font-weight:750; }}
    .cover img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
    .cover b {{ position:absolute; top:8px; left:8px; min-width:27px; height:27px; padding:0 7px; border-radius:9px; color:#061813; background:var(--accent); font-size:13px; line-height:27px; text-align:center; box-shadow:0 3px 10px rgba(0,0,0,.25); }}
    .info {{ display:flex; min-width:0; flex-direction:column; padding:13px 12px 11px; }}
    .info strong {{ display:-webkit-box; overflow:hidden; font-size:17px; line-height:1.4; -webkit-box-orient:vertical; -webkit-line-clamp:2; }}
    .tags {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:10px; }}
    .tags span {{ padding:3px 7px; border:1px solid #343943; border-radius:7px; color:#c8cdd4; background:#202329; font-size:11px; }}
    .source {{ overflow:hidden; margin-top:8px; color:#777f8a; font-size:12px; text-overflow:ellipsis; white-space:nowrap; }}
    .play {{ display:flex; align-items:center; justify-content:space-between; margin-top:auto; padding-top:9px; color:var(--accent); font-size:13px; font-weight:700; }}
    .play span {{ font-size:23px; line-height:14px; }}
    .tip {{ margin:16px 2px 0; color:#717883; font-size:12px; text-align:center; }}
    @media (max-width:680px) {{
      main {{ padding:16px 10px max(24px,env(safe-area-inset-bottom)); }}
      .grid {{ grid-template-columns:1fr; gap:10px; }}
      .recommend-card {{ grid-template-columns:108px minmax(0,1fr); min-height:154px; border-radius:13px; }}
      .cover {{ min-height:154px; }}
    }}
    @media (hover:hover) {{ .recommend-card:hover {{ border-color:rgba(32,197,165,.7); transform:translateY(-1px); }} }}
  </style>
</head>
<body>
  <main>
    <header><h1>{safe_heading}</h1><p>共 {len(record.variants)} 部，按资源站更新时间排序</p></header>
    <section class="grid">{''.join(cards)}</section>
    <p class="tip">点击任一作品即可进入播放器；推荐结果会随资源站更新。</p>
  </main>
</body>
</html>"""


def _collection_html(query_title: str) -> str:
    safe_query = escape(query_title or "赛事回放")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>{safe_query} 比赛回放</title>
  <style>
    :root {{ color-scheme:dark; font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif; --accent:#20c5a5; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; min-height:100vh; background:#0b0c0f; color:#f4f6f8; }}
    main {{ width:min(100%,900px); margin:0 auto; padding:18px 12px max(28px,env(safe-area-inset-bottom)); }}
    header {{ position:sticky; z-index:4; top:0; margin:-18px -12px 12px; padding:16px 14px 12px; border-bottom:1px solid rgba(255,255,255,.07); background:rgba(11,12,15,.94); backdrop-filter:blur(10px); -webkit-backdrop-filter:blur(10px); }}
    h1 {{ margin:0 0 5px; font-size:clamp(21px,5.6vw,30px); }}
    header p {{ margin:0; color:#9298a2; font-size:13px; }}
    #list {{ display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:10px; }}
    .replay {{ display:grid; grid-template-columns:96px 1fr; min-height:116px; overflow:hidden; border:1px solid #292d34; border-radius:13px; color:inherit; background:#17191e; text-decoration:none; }}
    .cover {{ position:relative; display:flex; align-items:center; justify-content:center; overflow:hidden; color:#66707c; background:linear-gradient(145deg,#25303a,#111419); font-size:28px; font-weight:800; }}
    .cover img {{ position:absolute; inset:0; width:100%; height:100%; object-fit:cover; }}
    .info {{ display:flex; min-width:0; flex-direction:column; padding:10px 10px 8px; }}
    .title {{ display:-webkit-box; overflow:hidden; font-size:14px; font-weight:650; line-height:1.42; -webkit-box-orient:vertical; -webkit-line-clamp:3; }}
    .meta {{ display:flex; flex-wrap:wrap; gap:5px; margin-top:7px; }}
    .meta span {{ padding:2px 5px; border:1px solid #343943; border-radius:5px; color:#afb5bd; background:#202329; font-size:10px; }}
    .source {{ overflow:hidden; margin-top:auto; padding-top:5px; color:#707782; font-size:11px; text-overflow:ellipsis; white-space:nowrap; }}
    .footer {{ display:flex; flex-direction:column; align-items:center; gap:9px; padding:18px 0 4px; }}
    #status {{ min-height:18px; color:#858c96; font-size:12px; }}
    #more {{ min-width:150px; min-height:40px; padding:0 18px; border:1px solid #343a43; border-radius:20px; color:#e8ebef; background:#20242a; font:inherit; font-size:13px; }}
    #more:disabled {{ opacity:.55; }}
    #more[hidden] {{ display:none; }}
    @media (max-width:620px) {{
      #list {{ grid-template-columns:1fr; gap:8px; }}
      .replay {{ grid-template-columns:104px 1fr; min-height:122px; }}
      .title {{ font-size:14px; -webkit-line-clamp:3; }}
    }}
    @media (hover:hover) {{ .replay:hover {{ border-color:rgba(32,197,165,.7); }} #more:hover:not(:disabled) {{ border-color:var(--accent); }} }}
  </style>
</head>
<body>
  <main>
    <header><h1>{safe_query} 比赛回放</h1><p id="summary">正在载入最新回放…</p></header>
    <section id="list" aria-live="polite"></section>
    <div class="footer"><div id="sentinel"></div><div id="status"></div><button id="more" type="button" disabled>加载更多</button></div>
  </main>
  <script src="../collection.js?v=2.1.0"></script>
</body>
</html>"""


_COLLECTION_JAVASCRIPT = r"""
(() => {
  'use strict';
  const list = document.querySelector('#list');
  const summary = document.querySelector('#summary');
  const status = document.querySelector('#status');
  const more = document.querySelector('#more');
  const sentinel = document.querySelector('#sentinel');
  const apiUrl = new URL(location.href);
  apiUrl.pathname = apiUrl.pathname.replace('/collection/', '/collection-api/');
  apiUrl.search = '';
  let nextPage = 1;
  let loading = false;
  let hasMore = true;
  let total = 0;
  let autoAllowed = false;

  function safeCover(value) {
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) ? url.href : '';
    } catch (_error) { return ''; }
  }

  function replayCard(item) {
    const link = document.createElement('a');
    link.className = 'replay';
    link.href = item.choose;
    const cover = document.createElement('div');
    cover.className = 'cover';
    cover.textContent = '回放';
    const coverUrl = safeCover(item.cover);
    if (coverUrl) {
      const image = document.createElement('img');
      image.src = coverUrl;
      image.alt = `${item.title}封面`;
      image.loading = 'lazy';
      image.referrerPolicy = 'no-referrer';
      cover.appendChild(image);
    }
    const info = document.createElement('div');
    info.className = 'info';
    const title = document.createElement('div');
    title.className = 'title';
    title.textContent = item.title;
    const meta = document.createElement('div');
    meta.className = 'meta';
    [item.category, item.remarks, item.episodes > 1 ? `${item.episodes}段` : '正片']
      .filter(Boolean)
      .forEach((value) => {
        const tag = document.createElement('span');
        tag.textContent = value;
        meta.appendChild(tag);
      });
    const source = document.createElement('div');
    source.className = 'source';
    source.textContent = item.source;
    info.append(title, meta, source);
    link.append(cover, info);
    return link;
  }

  async function loadNext(triggeredByScroll = false) {
    if (loading || !hasMore) return;
    loading = true;
    autoAllowed = false;
    more.disabled = true;
    more.textContent = '正在加载…';
    status.textContent = `正在读取第 ${nextPage} 页`;
    try {
      const url = new URL(apiUrl);
      url.searchParams.set('page', String(nextPage));
      const response = await fetch(url, { cache: 'no-store', credentials: 'omit' });
      const body = await response.json();
      if (!response.ok || !body.ok) throw new Error(body.error || '加载失败');
      body.items.forEach((item) => list.appendChild(replayCard(item)));
      total += body.items.length;
      hasMore = Boolean(body.has_more);
      nextPage += 1;
      summary.textContent = `已加载 ${total} 场，向下浏览可继续加载`;
      status.textContent = body.items.length
        ? ''
        : (hasMore ? '本页没有匹配回放，可继续加载下一页' : '已加载全部回放');
      autoAllowed = body.items.length > 0 && hasMore;
      more.hidden = !hasMore;
      more.disabled = !hasMore;
      more.textContent = hasMore ? '加载更多' : '已加载全部';
    } catch (error) {
      status.textContent = error.message || '加载失败，请重试';
      more.disabled = false;
      more.textContent = '重试';
      if (triggeredByScroll) autoAllowed = false;
    } finally {
      loading = false;
    }
  }

  more.addEventListener('click', () => loadNext(false));
  const observer = new IntersectionObserver((entries) => {
    if (entries.some((entry) => entry.isIntersecting) && autoAllowed) loadNext(true);
  }, { rootMargin: '500px 0px' });
  observer.observe(sentinel);
  loadNext(false);
})();
""".strip()


def _player_html(title: str) -> str:
    safe_title = escape(title or "短剧播放器")
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
  <title>{safe_title}</title>
  <style>
    :root {{ color-scheme: dark; font-family: system-ui,-apple-system,"Microsoft YaHei",sans-serif; --accent: #19b89b; }}
    * {{ box-sizing: border-box; }}
    html, body {{ min-height: 100%; }}
    body {{ margin: 0; background: #0b0c0f; color: #f3f5f7; }}
    body.sheet-open {{ overflow: hidden; }}
    button {{ font: inherit; }}
    button:focus-visible, input:focus-visible, a:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
    main {{ width: min(100%, 980px); margin: 0 auto; padding: 12px 12px max(22px, env(safe-area-inset-bottom)); }}
    .watch-stage {{ display: flex; min-height: calc(100vh - 24px); min-height: calc(var(--player-viewport-height, 100vh) - 24px); flex-direction: column; justify-content: center; }}
    .heading {{ margin: 1px 2px 10px; }}
    h1 {{ display: -webkit-box; overflow: hidden; margin: 0 0 3px; font-size: clamp(17px, 4.4vw, 21px); font-weight: 650; line-height: 1.35; -webkit-box-orient: vertical; -webkit-line-clamp: 2; }}
    #meta {{ color: #8f949d; font-size: 13px; }}
    .video-shell {{ position: relative; width: 100%; max-width: 100%; aspect-ratio: 16/9; margin: 0 auto; overflow: hidden; border-radius: 12px; background: #000; box-shadow: 0 8px 30px rgba(0,0,0,.28); transition: width .18s ease, aspect-ratio .18s ease; }}
    .video-shell.player--cinema, .video-shell.player--landscape {{ width: 100%; aspect-ratio: 16/9; }}
    .video-shell.player--square, .video-shell.player--portrait {{ width: min(100%, var(--inline-stage-width, 100%)); max-height: 68vh; max-height: 68svh; aspect-ratio: var(--video-aspect, 1); }}
    .video-shell:fullscreen, .video-shell:-webkit-full-screen, .video-shell.pseudo-fullscreen {{ width: 100vw !important; height: 100vh !important; height: 100dvh !important; max-width: none !important; aspect-ratio: auto !important; border-radius: 0; }}
    .video-shell.pseudo-fullscreen {{ position: fixed; z-index: 100; inset: 0; margin: 0; }}
    .video-shell:fullscreen .mobile-control-panel, .video-shell:-webkit-full-screen .mobile-control-panel, .video-shell.pseudo-fullscreen .mobile-control-panel, .video-shell:fullscreen #mobileEpisodeButtonTop, .video-shell:-webkit-full-screen #mobileEpisodeButtonTop, .video-shell.pseudo-fullscreen #mobileEpisodeButtonTop {{ display: none !important; }}
    video {{ position: absolute; inset: 0; display: block; width: 100%; height: 100%; background: #000; object-fit: contain; }}
    .video-actions {{ position: absolute; inset: 0; z-index: 2; opacity: 1; transition: opacity 220ms ease; pointer-events: none; }}
    .video-actions.controls-hidden {{ opacity: 0; pointer-events: none; }}
    .video-actions.controls-hidden, .video-actions.controls-hidden * {{ pointer-events: none !important; }}
    .icon-button, .pill-button {{ display: inline-flex; align-items: center; justify-content: center; border: 1px solid rgba(255,255,255,.16); color: #fff; background: rgba(10,12,15,.66); box-shadow: 0 2px 10px rgba(0,0,0,.22); backdrop-filter: blur(7px); -webkit-backdrop-filter: blur(7px); pointer-events: auto; }}
    .icon-button {{ position: absolute; top: 50%; width: 36px; height: 36px; padding: 0; border-radius: 50%; transform: translateY(-50%); }}
    .icon-button svg {{ width: 20px; height: 20px; fill: currentColor; }}
    #previous {{ left: 8px; }}
    #next {{ right: 8px; }}
    .icon-button:disabled {{ visibility: hidden; }}
    .pill-button {{ position: absolute; top: 8px; min-height: 31px; padding: 0 10px; border-radius: 16px; font-size: 12px; line-height: 1; }}
    #episodeOpen {{ left: 8px; min-width: 52px; }}
    #currentEpisode {{ position: absolute; z-index: 3; top: 8px; left: 50%; max-width: 46%; min-height: 31px; overflow: hidden; padding: 0 10px; color: #fff; font-size: 13px; line-height: 31px; text-overflow: ellipsis; text-shadow: 0 1px 4px #000; white-space: nowrap; transform: translateX(-50%); pointer-events: none; }}
    #mobileEpisodeButtonTop {{ display: none; }}
    .mobile-control-panel #mobileEpisodeButton {{ display: none !important; }}
    .top-right-actions {{ position: absolute; z-index: 3; top: 8px; right: 8px; display: flex; align-items: center; gap: 6px; pointer-events: none; }}
    .top-right-actions .pill-button {{ position: static; top: auto; pointer-events: auto; }}
    #qualityButton {{ min-width: 52px; }}
    #qualityButton:disabled {{ opacity: .72; }}
    #fullscreenButton {{ flex: 0 0 auto; width: 31px; height: 31px; min-height: 31px; padding: 6px; border-radius: 50%; }}
    #fullscreenButton svg {{ width: 100%; height: 100%; fill: currentColor; }}
    #fullscreenButton .exit-fullscreen-icon {{ display: none; }}
    .video-shell.is-fullscreen #fullscreenButton .enter-fullscreen-icon {{ display: none; }}
    .video-shell.is-fullscreen #fullscreenButton .exit-fullscreen-icon {{ display: block; }}
    #holdSpeedHint {{ position: absolute; z-index: 5; top: 48px; left: 10px; display: inline-flex; align-items: center; justify-content: center; min-width: 36px; min-height: 26px; padding: 0 8px; border: 1px solid rgba(255,255,255,.08); border-radius: 13px; color: rgba(255,255,255,.82); background: rgba(8,10,13,.3); font-size: 12px; font-weight: 650; pointer-events: none; backdrop-filter: blur(4px); -webkit-backdrop-filter: blur(4px); }}
    #holdSpeedHint[hidden] {{ display: none; }}
    #playbackOverlay {{ position: absolute; z-index: 4; top: 50%; left: 50%; display: none; width: 60px; height: 60px; padding: 0; place-items: center; border: 1px solid rgba(255,255,255,.2); border-radius: 50%; color: #fff; background: rgba(8,10,13,.48); opacity: 1; transform: translate(-50%,-50%); transition: opacity .2s ease; cursor: pointer; pointer-events: auto; }}
    #playbackOverlay[hidden] {{ display: none !important; }}
    #playbackOverlay svg {{ width: 26px; height: 26px; fill: currentColor; }}
    #playbackOverlay .pause-mark {{ display: none; }}
    #playbackOverlay.is-playing .play-mark {{ display: none; }}
    #playbackOverlay.is-playing .pause-mark {{ display: block; }}
    .quality-menu {{ position: absolute; z-index: 4; top: 44px; right: 8px; display: grid; grid-template-columns: repeat(2,minmax(68px,1fr)); gap: 3px; min-width: 156px; max-height: calc(100% - 52px); overflow-y: auto; padding: 5px; border: 1px solid #343840; border-radius: 10px; background: rgba(22,24,29,.96); box-shadow: 0 10px 30px rgba(0,0,0,.4); pointer-events: auto; }}
    .quality-menu[hidden] {{ display: none; }}
    .quality-menu button {{ display: block; width: 100%; min-height: 35px; padding: 0 8px; border: 0; border-radius: 7px; color: #d9dde3; background: transparent; text-align: center; }}
    .quality-menu button.active {{ color: #071512; background: var(--accent); font-weight: 650; }}
    .mobile-control-panel {{ display: none; }}
    .below-player {{ display: flex; align-items: center; gap: 10px; min-height: 38px; padding: 8px 2px 0; }}
    .switch-label {{ display: inline-flex; flex: 0 0 auto; align-items: center; gap: 7px; color: #aeb3bc; font-size: 13px; cursor: pointer; }}
    .switch-label input {{ position: absolute; width: 1px; height: 1px; opacity: 0; }}
    .switch {{ position: relative; width: 32px; height: 18px; border-radius: 10px; background: #383c44; transition: background .16s; }}
    .switch::after {{ content: ""; position: absolute; top: 2px; left: 2px; width: 14px; height: 14px; border-radius: 50%; background: #fff; transition: transform .16s; }}
    .switch-label input:checked + .switch {{ background: var(--accent); }}
    .switch-label input:checked + .switch::after {{ transform: translateX(14px); }}
    #status {{ flex: 1 1 auto; min-width: 0; margin: 0; color: #e4ae59; font-size: 12px; text-align: center; }}
    #retry {{ flex: 0 0 auto; min-height: 28px; padding: 0 9px; border: 1px solid #3a3e46; border-radius: 7px; color: #dce0e6; background: #202329; font-size: 12px; }}
    #retry[hidden] {{ display: none; }}
    #direct {{ flex: 0 0 auto; color: #8ca9ce; font-size: 12px; text-decoration: none; }}
    .inline-episodes {{ margin: 14px 0 2px; }}
    .inline-episodes-header {{ display: flex; align-items: baseline; justify-content: space-between; padding: 0 2px 10px; }}
    .inline-episodes-title {{ font-size: 18px; font-weight: 650; }}
    #episodeCount {{ color: #8f949d; font-size: 13px; }}
    .inline-episode-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(68px, 1fr)); gap: 8px; width: 100%; overflow: visible; }}
    .inline-episode-grid .episode-item {{ min-height: 48px; }}
    .sheet {{ position: fixed; z-index: 20; inset: 0; display: flex; align-items: flex-end; justify-content: center; padding-top: 48px; }}
    .sheet[hidden] {{ display: none; }}
    .sheet-backdrop {{ position: absolute; inset: 0; width: 100%; border: 0; background: rgba(0,0,0,.58); }}
    .sheet-panel {{ position: relative; width: min(100%, 720px); max-height: min(68vh, 620px); max-height: min(68svh, 620px); overflow: hidden; border: 1px solid #30333a; border-bottom: 0; border-radius: 16px 16px 0 0; background: #17191e; box-shadow: 0 -12px 38px rgba(0,0,0,.36); }}
    .sheet-header {{ display: flex; align-items: center; justify-content: space-between; min-height: 52px; padding: 0 14px; border-bottom: 1px solid #292c32; }}
    .sheet-title {{ font-size: 16px; font-weight: 650; }}
    .sheet-close {{ width: 34px; height: 34px; padding: 0; border: 0; border-radius: 50%; color: #bfc4cc; background: transparent; font-size: 25px; line-height: 1; }}
    .episode-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(72px,1fr)); gap: 8px; max-height: calc(min(68vh, 620px) - 53px); max-height: calc(min(68svh, 620px) - 53px); overflow-y: auto; padding: 12px 12px max(18px, env(safe-area-inset-bottom)); overscroll-behavior: contain; }}
    .episode-item {{ min-height: 40px; padding: 0 8px; border: 1px solid #343840; border-radius: 9px; color: #d7dbe1; background: #22252b; font-size: 13px; }}
    .episode-item.active {{ border-color: var(--accent); color: #071512; background: var(--accent); font-weight: 650; }}
    .episode-item:disabled {{ opacity: .45; }}
    @media (max-width: 560px) {{
      main {{ padding: 12px 12px max(20px, env(safe-area-inset-bottom)); }}
      .watch-stage {{ min-height: auto; justify-content: flex-start; padding-top: 36px; }}
      .heading {{ margin: 0 4px 14px; }}
      h1 {{ margin-bottom: 5px; font-size: 22px; }}
      .video-shell {{ border: 1px solid #2d3138; border-radius: 8px; box-shadow: none; }}
      .video-shell:not(.is-fullscreen) .desktop-video-control {{ display: none !important; }}
      .video-shell:not(.is-fullscreen) .top-right-actions {{ display: flex; right: max(8px, env(safe-area-inset-right)); }}
      .video-shell:not(.is-fullscreen) #playbackOverlay {{ display: grid; }}
      .mobile-control-panel {{ position: absolute; z-index: 5; right: 0; bottom: 0; left: 0; display: flex; flex-direction: column; gap: 4px; margin: 0; padding: 26px max(12px, env(safe-area-inset-left)) max(8px, env(safe-area-inset-bottom)) max(12px, env(safe-area-inset-right)); border: 0; border-radius: 0; background: linear-gradient(to bottom, transparent 0%, rgba(8,10,13,.2) 35%, rgba(8,10,13,.68) 100%); pointer-events: auto; }}
      .mobile-time-row {{ display: flex; order: 2; align-items: center; justify-content: space-between; color: #f3f5f7; font-size: 14px; font-variant-numeric: tabular-nums; }}
      .mobile-seek-wrap {{ display: flex; order: 3; height: 36px; align-items: center; }}
      #mobileSeek {{ width: 100%; height: 28px; margin: 0; appearance: none; -webkit-appearance: none; background: transparent; cursor: pointer; touch-action: none; --seek-progress: 0%; }}
      #mobileSeek::-webkit-slider-runnable-track {{ height: 4px; border-radius: 2px; background: linear-gradient(to right,var(--accent) 0,var(--accent) var(--seek-progress),#50545b var(--seek-progress),#50545b 100%); }}
      #mobileSeek::-webkit-slider-thumb {{ width: 18px; height: 18px; margin-top: -7px; appearance: none; -webkit-appearance: none; border: 2px solid #fff; border-radius: 50%; background: var(--accent); box-shadow: 0 1px 5px rgba(0,0,0,.45); }}
      #mobileSeek::-moz-range-track {{ height: 4px; border: 0; border-radius: 2px; background: #50545b; }}
      #mobileSeek::-moz-range-progress {{ height: 4px; border-radius: 2px; background: var(--accent); }}
      #mobileSeek::-moz-range-thumb {{ width: 16px; height: 16px; border: 2px solid #fff; border-radius: 50%; background: var(--accent); box-shadow: 0 1px 5px rgba(0,0,0,.45); }}
      .mobile-action-row {{ display: grid; grid-template-columns: minmax(0,1fr) auto; align-items: center; gap: 8px; }}
      .mobile-action-row {{ display: flex; order: 1; justify-content: flex-end; }}
      .mobile-episode-button {{ display: flex; min-width: 0; min-height: 44px; align-items: center; gap: 5px; padding: 0 8px; overflow: hidden; border: 0; border-radius: 7px; color: #f2f4f6; background: #24272d; font-size: 13px; text-align: left; }}
      .mobile-episode-button svg {{ flex: 0 0 auto; width: 16px; height: 16px; fill: currentColor; }}
      .mobile-episode-button > span:first-of-type {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
      #mobileEpisodeLabel {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
      .episode-chevron {{ flex: 0 0 auto; color: #aeb4bd; font-size: 16px; }}
      .mobile-actions-right {{ display: flex; align-items: center; gap: 2px; }}
      .mobile-tool-wrap {{ position: relative; }}
      .mobile-tool-button {{ display: inline-flex; min-width: 40px; height: 44px; align-items: center; justify-content: center; padding: 0 8px; border: 0; border-radius: 7px; color: #e5e8ec; background: transparent; }}
      .mobile-tool-button svg {{ width: 22px; height: 22px; fill: currentColor; }}
      .mobile-tool-button:active, .mobile-episode-button:active {{ background: #30343b; }}
      #mobileQualityButton {{ min-width: 48px; color: #cfd3d9; font-size: 13px; }}
      #mobileQualityButton {{ display: none; }}
      #mobileQualityButton:disabled {{ opacity: .72; }}
      #mobileMuteButton .muted-icon {{ display: none; }}
      #mobileMuteButton.is-muted .volume-icon {{ display: none; }}
      #mobileMuteButton.is-muted .muted-icon {{ display: block; }}
      .mobile-popup-menu {{ position: absolute; z-index: 12; right: 0; bottom: 50px; min-width: 132px; overflow: hidden; padding: 5px; border: 1px solid #343840; border-radius: 8px; background: #202329; box-shadow: 0 10px 24px rgba(0,0,0,.4); }}
      .mobile-popup-menu[hidden] {{ display: none; }}
      .mobile-popup-menu button {{ display: block; width: 100%; min-height: 40px; padding: 0 12px; border: 0; border-radius: 6px; color: #e2e5e9; background: transparent; text-align: left; }}
      .mobile-popup-menu button.active {{ color: #071512; background: var(--accent); font-weight: 650; }}
      .below-player {{ min-height: 58px; margin-top: 10px; gap: 8px; padding: 8px 14px; border: 1px solid #292d34; border-radius: 8px; background: #17191e; }}
      .switch-label {{ min-height: 42px; color: #edf0f3; font-size: 14px; }}
      .switch {{ width: 38px; height: 22px; border-radius: 11px; }}
      .switch::after {{ width: 18px; height: 18px; }}
      .switch-label input:checked + .switch::after {{ transform: translateX(16px); }}
      #status {{ font-size: 11px; text-align: center; }}
      #direct {{ display: inline-flex; min-height: 40px; align-items: center; color: #63aaf2; font-size: 14px; }}
      #direct::after {{ content: "›"; margin-left: 6px; font-size: 22px; line-height: 1; }}
      .episode-grid {{ grid-template-columns: repeat(4, minmax(0,1fr)); }}
      .inline-episodes {{ margin-top: 18px; }}
      .inline-episode-grid {{ grid-template-columns: repeat(5, minmax(0,1fr)); gap: 7px; }}
      .inline-episode-grid .episode-item {{ min-height: 46px; padding: 0 3px; border-radius: 8px; }}
    }}
    @media (max-width: 560px) {{
      #mobileEpisodeButtonTop {{ display: inline-flex; position: absolute; top: 8px; left: max(12px, env(safe-area-inset-left)); z-index: 6; min-height: 38px; max-width: min(72vw, 220px); align-items: center; gap: 5px; padding: 0 11px; overflow: hidden; border: 1px solid rgba(255,255,255,.16); border-radius: 18px; color: #f2f4f6; background: rgba(10,12,15,.52); font-size: 13px; text-align: left; backdrop-filter: blur(5px); -webkit-backdrop-filter: blur(5px); pointer-events: auto; }}
      #mobileEpisodeButtonTop > span {{ min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
      #mobileEpisodeButtonTop .episode-chevron {{ flex: 0 0 auto; margin-left: 4px; color: #aeb4bd; font-size: 17px; }}
    }}
    @media (max-width: 350px) {{
      main {{ padding-right: 10px; padding-left: 10px; }}
      .mobile-control-panel {{ padding-right: 10px; padding-left: 10px; }}
      .mobile-action-row {{ gap: 4px; }}
      .mobile-episode-button {{ gap: 3px; padding: 0 5px; font-size: 11.5px; }}
      .mobile-episode-button svg {{ width: 14px; height: 14px; }}
      .episode-chevron {{ font-size: 14px; }}
      .mobile-actions-right {{ gap: 0; }}
      .mobile-tool-button {{ min-width: 40px; padding: 0 6px; }}
      #mobileQualityButton {{ min-width: 44px; }}
    }}
    @media (prefers-reduced-motion: reduce) {{
      *, *::before, *::after {{ scroll-behavior: auto !important; transition-duration: .01ms !important; }}
    }}
    @media (hover: hover) {{ .icon-button:hover, .pill-button:hover, .episode-item:hover {{ border-color: rgba(25,184,155,.8); }} }}
    /* Inline mobile player: reveal a light overlay only while the viewer is interacting. */
    .video-actions button, .video-actions .pill-button, .mobile-control-panel input {{ touch-action: manipulation; }}
    .video-actions button, .video-actions .pill-button {{ cursor: pointer; }}
    #episodeOpen {{ min-height: 38px; }}
    #qualityButton {{ min-height: 38px; }}
    #qualityButton:disabled {{ cursor: default; opacity: .72; }}
    .quality-menu {{ top: 56px; right: max(12px, env(safe-area-inset-right)); min-width: 156px; border-color: rgba(255,255,255,.14); background: rgba(22,24,29,.96); }}
    .quality-menu button {{ min-height: 40px; cursor: pointer; }}
    #mobileEpisodeButtonTop {{ min-height: 40px; top: max(12px, env(safe-area-inset-top)); left: max(12px, env(safe-area-inset-left)); }}
    .mobile-episode-button .episode-list-icon {{ flex: 0 0 auto; width: 18px; height: 18px; fill: currentColor; }}
    .top-right-actions {{ top: max(12px, env(safe-area-inset-top)); right: max(12px, env(safe-area-inset-right)); gap: 8px; }}
    @media (max-width: 560px) {{
      .video-shell:not(.is-fullscreen) #previous, .video-shell:not(.is-fullscreen) #next {{ width: 44px; height: 44px; opacity: .82; }}
      .video-shell:not(.is-fullscreen) #previous {{ left: max(12px, env(safe-area-inset-left)); }}
      .video-shell:not(.is-fullscreen) #next {{ right: max(12px, env(safe-area-inset-right)); }}
      .video-shell:not(.is-fullscreen) #previous, .video-shell:not(.is-fullscreen) #next {{ display: none !important; }}
      /* Leave a clear visual lane between the centered play control and the bottom tool row. */
      .video-shell:not(.is-fullscreen) #playbackOverlay {{ top: 34%; }}
      .mobile-control-panel {{
        display: grid;
        grid-template-columns: max-content minmax(20px, 1fr) max-content;
        grid-template-rows: 44px max-content 28px;
        grid-template-areas: "actions actions actions" "time . duration" "seek seek seek";
        align-items: center;
        column-gap: 8px;
        row-gap: 2px;
        padding-top: 18px;
        padding-right: max(12px, env(safe-area-inset-right));
        padding-left: max(12px, env(safe-area-inset-left));
      }}
      .mobile-time-row {{ display: contents; }}
      #mobileCurrentTime {{ grid-area: time; }}
      #mobileDuration {{ grid-area: duration; }}
      .mobile-seek-wrap {{ grid-area: seek; min-width: 0; height: 28px; }}
      #mobileSeek {{ height: 28px; }}
      .mobile-action-row {{
        display: flex;
        grid-area: actions;
        width: auto;
        max-width: none;
        align-items: center;
        justify-content: flex-end;
        gap: 8px;
      }}
      .mobile-action-row .mobile-episode-button {{ display: none !important; }}
      .mobile-actions-right, .mobile-action-row > .mobile-tool-wrap {{ display: flex; align-items: center; gap: 8px; margin-left: 0; }}
      .mobile-tool-button {{
        flex: 0 0 44px;
        min-width: 44px;
        width: 44px;
        height: 44px;
        padding: 0;
        border: 1px solid rgba(255,255,255,.14);
        border-radius: 12px;
        background: rgba(10,12,15,.58);
        cursor: pointer;
      }}
      .mobile-tool-button svg {{ width: 19px; height: 19px; }}
    }}
    @media (max-width: 350px) {{
      .video-shell:not(.is-fullscreen) #mobileEpisodeButtonTop {{ max-width: 105px; padding-right: 7px; padding-left: 7px; gap: 3px; font-size: 11.5px; white-space: nowrap; }}
      .video-shell:not(.is-fullscreen) #mobileEpisodeButtonTop .episode-list-icon {{ width: 14px; height: 14px; }}
      .video-shell:not(.is-fullscreen) #playbackOverlay {{ top: 23%; width: 44px; height: 44px; }}
      .video-shell:not(.is-fullscreen) #playbackOverlay svg {{ width: 20px; height: 20px; }}
    }}

    /* Reference composition: one calm, dark player surface with a strict control hierarchy. */
    :root {{ --accent: #18c4aa; --canvas: #090b0e; --surface: #111419; --surface-2: #191d23; --line: rgba(255,255,255,.14); --muted: #8b929d; }}
    html, body {{ background: var(--canvas); }}
    body {{ background-image: radial-gradient(circle at 50% -10%, rgba(28,45,48,.2), transparent 42%); }}
    main {{ width: min(100%, 680px); padding: 18px 14px max(32px, env(safe-area-inset-bottom)); }}
    .watch-stage {{ justify-content: center; }}
    .page-heading {{ display: grid; grid-template-columns: 44px minmax(0,1fr) 44px; align-items: center; min-height: 64px; margin: 0 -2px 4px; }}
    .page-nav-button {{ display: inline-flex; width: 44px; height: 44px; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 12px; color: #f5f7f8; background: transparent; cursor: pointer; transition: background-color .18s ease, color .18s ease; }}
    .page-nav-button svg {{ width: 25px; height: 25px; fill: currentColor; }}
    .page-nav-button:hover, .page-nav-button:focus-visible {{ color: var(--accent); background: rgba(255,255,255,.08); }}
    .heading-copy {{ min-width: 0; text-align: center; }}
    .heading-copy h1 {{ margin: 0; overflow: hidden; color: #f4f6f7; font-size: clamp(20px, 5.3vw, 26px); font-weight: 700; line-height: 1.2; text-overflow: ellipsis; white-space: nowrap; }}
    .heading-subtitle {{ display: block; margin-top: 3px; color: #777f89; font-size: 12px; line-height: 1.2; }}
    .episode-meta {{ display: flex; align-items: center; gap: 7px; min-height: 28px; margin: 0 4px 12px; color: #aeb4bd; font-size: 15px; }}
    .episode-meta-label {{ color: #8b929d; }}
    .episode-meta #meta {{ overflow: hidden; color: #aeb4bd; text-overflow: ellipsis; white-space: nowrap; }}
    .video-shell {{ border: 1px solid var(--line); border-radius: 14px; background: #000; box-shadow: 0 12px 34px rgba(0,0,0,.28); }}
    .video-actions {{ padding: 0 !important; }}
    .video-actions .topbar {{ top: 14px; left: 14px; right: 14px; z-index: 6; }}
    .top-left, .top-right, .top-right-actions {{ align-items: center; gap: 8px; }}
    .top-right-actions {{ position: absolute; top: 14px; right: 14px; z-index: 6; display: flex; pointer-events: auto; }}
    .icon-button, .pill-button {{ border-color: rgba(255,255,255,.18); color: #f5f7f8; background: rgba(10,12,15,.58); box-shadow: 0 4px 16px rgba(0,0,0,.2); backdrop-filter: blur(9px); -webkit-backdrop-filter: blur(9px); }}
    .pill-button {{ min-height: 40px; padding: 0 14px; border-radius: 20px; font-size: 14px; }}
    .top-left #currentEpisode {{ position: static; min-height: 40px; padding: 0 14px; line-height: 40px; }}
    .video-actions > #currentEpisode {{ top: 14px; min-height: 40px; line-height: 40px; }}
    #mobileEpisodeButtonTop {{ display: none; }}
    .mobile-episode-button {{ min-height: 40px; padding: 0 13px; border: 1px solid rgba(255,255,255,.18); border-radius: 20px; color: #f5f7f8; background: rgba(10,12,15,.58); font-size: 14px; white-space: nowrap; backdrop-filter: blur(9px); -webkit-backdrop-filter: blur(9px); }}
    .mobile-episode-button .episode-list-icon {{ width: 18px; height: 18px; fill: currentColor; }}
    .center-nav {{ z-index: 3; padding: 0 14px; }}
    .center-nav .icon-button {{ width: 44px; height: 44px; border-radius: 12px; }}
    #playbackOverlay {{ width: 72px; height: 72px; border-color: rgba(255,255,255,.25); background: rgba(8,10,13,.5); box-shadow: 0 8px 24px rgba(0,0,0,.3); }}
    #playbackOverlay svg {{ width: 30px; height: 30px; }}
    .mobile-control-panel {{ display: none; }}
    .below-player {{ display: flex; align-items: center; gap: 12px; min-height: 66px; margin-top: 12px; padding: 10px 16px; border: 1px solid var(--line); border-radius: 12px; background: linear-gradient(120deg, rgba(25,30,36,.82), rgba(14,18,22,.92)); }}
    .switch-label {{ display: inline-flex; flex: 0 0 auto; min-height: 44px; align-items: center; gap: 9px; color: #edf0f3; font-size: 15px; cursor: pointer; }}
    .switch {{ position: relative; width: 50px; height: 28px; border-radius: 16px; background: #3a414a; transition: background-color .18s ease; }}
    .switch::after {{ content: ""; position: absolute; top: 3px; left: 3px; width: 22px; height: 22px; border-radius: 50%; background: #fff; box-shadow: 0 1px 5px rgba(0,0,0,.2); transition: transform .18s ease; }}
    .switch-label input:checked + .switch {{ background: var(--accent); }}
    .switch-label input:checked + .switch::after {{ transform: translateX(22px); }}
    #status {{ flex: 1 1 auto; min-width: 0; margin: 0; overflow: hidden; color: var(--accent); font-size: 14px; text-align: center; text-overflow: ellipsis; white-space: nowrap; }}
    #status.error {{ color: #f0a19c; }}
    #direct {{ display: inline-flex; flex: 0 0 auto; min-height: 44px; align-items: center; color: #82b9ee; font-size: 15px; text-decoration: none; }}
    #direct::after {{ content: "›"; margin-left: 6px; font-size: 23px; line-height: 1; }}
    .inline-episodes {{ margin: 22px 0 2px; }}
    .inline-episodes-header {{ display: flex; align-items: center; justify-content: space-between; padding: 0 4px 12px; }}
    .inline-episodes-title {{ color: #f4f6f7; font-size: 21px; font-weight: 700; }}
    .inline-episodes-tools {{ display: inline-flex; align-items: center; gap: 8px; }}
    #episodeCount {{ color: var(--muted); font-size: 13px; }}
    .episode-expand-button {{ display: inline-flex; width: 36px; height: 36px; align-items: center; justify-content: center; padding: 0; border: 0; border-radius: 10px; color: #edf0f3; background: transparent; cursor: pointer; transition: background-color .18s ease, transform .18s ease; }}
    .episode-expand-button svg {{ width: 22px; height: 22px; fill: currentColor; }}
    .episode-expand-button:hover, .episode-expand-button:focus-visible {{ background: rgba(255,255,255,.08); }}
    .inline-episodes.is-expanded .episode-expand-button {{ transform: rotate(180deg); }}
    .inline-episodes.is-collapsed .inline-episode-grid .episode-item:nth-child(n+11) {{ display: none; }}
    .inline-episode-grid {{ display: grid; grid-template-columns: repeat(5, minmax(0,1fr)); gap: 10px; }}
    .inline-episode-grid .episode-item {{ min-height: 58px; overflow: hidden; padding: 0 4px; border: 1px solid #353b44; border-radius: 11px; color: #d6dbe1; background: var(--surface-2); font-size: 15px; text-overflow: ellipsis; white-space: nowrap; cursor: pointer; transition: border-color .18s ease, background-color .18s ease, color .18s ease; }}
    .inline-episode-grid .episode-item:hover, .inline-episode-grid .episode-item:focus-visible {{ border-color: rgba(24,196,170,.78); }}
    .inline-episode-grid .episode-item.active {{ border-color: var(--accent); color: #061713; background: var(--accent); font-weight: 700; }}
    .sheet {{ z-index: 20; }}
    .sheet-panel {{ border-color: #343a43; border-radius: 18px 18px 0 0; background: #15191e; }}
    .sheet-header {{ min-height: 58px; }}
    .sheet-title {{ font-size: 17px; font-weight: 700; }}
    .sheet-close {{ width: 40px; height: 40px; }}
    .episode-grid {{ gap: 9px; }}
    .episode-item {{ min-height: 48px; border-radius: 10px; }}
    @media (max-width: 560px) {{
      main {{ width: 100%; padding: 10px 14px max(28px, env(safe-area-inset-bottom)); }}
      .watch-stage {{ min-height: auto; justify-content: flex-start; padding-top: 4px; }}
      .page-heading {{ min-height: 62px; margin-bottom: 1px; }}
      .heading-copy h1 {{ font-size: 23px; }}
      .heading-subtitle {{ display: none; }}
      .episode-meta {{ margin: 0 4px 12px; font-size: 15px; }}
      .video-shell {{ border-radius: 13px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) .desktop-video-control {{ display: none !important; }}
      .video-actions .topbar {{ top: 13px; left: 13px; right: 13px; }}
      .video-actions .top-right {{ display: flex; }}
      .video-actions .top-right #fullscreenButton {{ display: none; }}
      #mobileEpisodeButtonTop {{ display: inline-flex; position: static; min-height: 40px; max-width: min(72vw, 230px); align-items: center; gap: 6px; }}
      .top-right-actions {{ top: 13px; right: 13px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #playbackOverlay {{ display: grid; top: 50%; width: 60px; height: 60px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #playbackOverlay svg {{ width: 27px; height: 27px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #previous, .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #next {{ display: none !important; }}
      .mobile-control-panel {{ position: absolute; z-index: 5; right: 0; bottom: 0; left: 0; display: grid !important; grid-template-columns: max-content minmax(40px,1fr) max-content max-content; grid-template-rows: 44px; grid-template-areas: "time seek duration actions"; align-items: center; column-gap: 8px; row-gap: 0; padding: 8px max(13px, env(safe-area-inset-left)) max(9px, env(safe-area-inset-bottom)) max(13px, env(safe-area-inset-right)); border: 0; border-radius: 0; background: linear-gradient(to bottom, transparent 0%, rgba(8,10,13,.24) 20%, rgba(8,10,13,.82) 100%); }}
      .mobile-time-row {{ display: contents; color: #f3f5f7; font-size: 13px; font-variant-numeric: tabular-nums; text-shadow: 0 1px 4px #000; }}
      #mobileCurrentTime {{ grid-area: time; }}
      #mobileDuration {{ grid-area: duration; }}
      .mobile-seek-wrap {{ display: flex; grid-area: seek; min-width: 0; height: 28px; align-items: center; }}
      #mobileSeek {{ width: 100%; height: 28px; margin: 0; }}
      .mobile-action-row {{ display: flex; grid-area: actions; width: auto; max-width: none; align-items: center; justify-content: flex-end; gap: 6px; }}
      .mobile-action-row .mobile-episode-button, #mobileQualityButton {{ display: none !important; }}
      .mobile-actions-right, .mobile-action-row > .mobile-tool-wrap {{ display: flex; align-items: center; gap: 6px; margin-left: 0; }}
      .mobile-tool-button {{ display: inline-flex; flex: 0 0 40px; width: 40px; min-width: 40px; height: 40px; align-items: center; justify-content: center; padding: 0; border: 1px solid rgba(255,255,255,.16); border-radius: 10px; color: #f2f4f6; background: rgba(10,12,15,.58); cursor: pointer; }}
      .mobile-tool-button svg {{ width: 18px; height: 18px; fill: currentColor; }}
      .below-player {{ min-height: 68px; margin-top: 10px; padding: 10px 14px; border-radius: 13px; }}
      .switch-label {{ gap: 8px; font-size: 14px; }}
      #status {{ font-size: 13px; }}
      #direct {{ font-size: 14px; }}
      .inline-episodes {{ margin-top: 21px; }}
      .inline-episodes-header {{ padding-right: 2px; padding-left: 2px; }}
      .inline-episodes-title {{ font-size: 21px; }}
      #episodeCount {{ display: none; }}
      .inline-episode-grid {{ gap: 8px; }}
      .inline-episode-grid .episode-item {{ min-height: 58px; border-radius: 11px; font-size: 15px; }}
    }}
    @media (max-width: 350px) {{
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #mobileEpisodeButtonTop {{ max-width: 105px; padding-right: 7px; padding-left: 7px; gap: 3px; font-size: 11.5px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #mobileEpisodeButtonTop .episode-list-icon {{ width: 14px; height: 14px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #playbackOverlay {{ top: 50%; width: 44px; height: 44px; }}
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #playbackOverlay svg {{ width: 20px; height: 20px; }}
    }}
    /* Mobile uses the browser/OS video controls. Keep only episode and quality overlays. */
    @media (max-width: 560px) {{
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) .mobile-control-panel,
      .video-shell:not(:fullscreen):not(.pseudo-fullscreen) #playbackOverlay {{ display: none !important; }}
    }}
  </style>
</head>
<body>
  <main>
    <section class="watch-stage">
      <header class="heading page-heading">
        <button id="pageBackButton" class="page-nav-button" type="button" aria-label="返回">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m14.7 5.3-1.4-1.4L5.2 12l8.1 8.1 1.4-1.4L8 12l6.7-6.7Z"/></svg>
        </button>
        <div class="heading-copy"><h1 id="title">{safe_title}</h1><span class="heading-subtitle">短剧播放器</span></div>
        <button id="pageMoreButton" class="page-nav-button" type="button" aria-label="更多选项">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"/></svg>
        </button>
      </header>
      <div class="episode-meta"><span class="episode-meta-label">当前：</span><span id="meta">正在载入剧集…</span></div>
      <div id="videoShell" class="video-shell player--landscape">
        <video id="video" controls controlslist="nodownload noremoteplayback" disablepictureinpicture playsinline webkit-playsinline preload="metadata"></video>
        <div class="video-actions">
          <div id="holdSpeedHint" role="status" aria-live="polite" hidden>2×</div>
          <button id="playbackOverlay" type="button" aria-label="播放或暂停">
            <svg class="play-mark" viewBox="0 0 24 24"><path d="M8 5v14l11-7L8 5Z"/></svg>
            <svg class="pause-mark" viewBox="0 0 24 24"><path d="M7 5h4v14H7V5Zm6 0h4v14h-4V5Z"/></svg>
          </button>
        <button id="episodeOpen" class="pill-button desktop-video-control" type="button" aria-label="选择剧集">选集</button>
        <div id="currentEpisode" class="desktop-video-control" aria-live="polite">第01集</div>
        <button id="mobileEpisodeButtonTop" class="mobile-episode-button" type="button" aria-label="选择剧集"><svg class="episode-list-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h2v2H4V5Zm4 0h12v2H8V5ZM4 11h2v2H4v-2Zm4 0h12v2H8v-2ZM4 17h2v2H4v-2Zm4 0h12v2H8v-2Z"/></svg>选集 · <span id="mobileEpisodeLabelTop">第01集</span><span class="episode-chevron">›</span></button>
        <div class="top-right-actions">
          <button id="qualityButton" class="pill-button" type="button" aria-label="选择清晰度" disabled><span id="qualityLabel">原画</span></button>
          <button id="fullscreenButton" class="pill-button desktop-video-control" type="button" aria-label="全屏播放">
            <svg class="enter-fullscreen-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3H3v4h2V5h2V3Zm14 4V3h-4v2h2v2h2ZM5 17H3v4h4v-2H5v-2Zm16 0h-2v2h-2v2h4v-4Z"/></svg>
            <svg class="exit-fullscreen-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3H3v4h4V5H5V3Zm14 0v2h-2v2h4V3h-2ZM7 17H3v4h2v-2h2v-2Zm14 0h-4v2h2v2h2v-4Z"/></svg>
          </button>
        </div>
        <div id="qualityMenu" class="quality-menu" role="menu" hidden></div>
        <button id="previous" class="icon-button" type="button" aria-label="上一集">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M15.4 7.4 10.8 12l4.6 4.6L14 18l-6-6 6-6 1.4 1.4Z"/></svg>
        </button>
        <button id="next" class="icon-button" type="button" aria-label="下一集">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m8.6 16.6 4.6-4.6-4.6-4.6L10 6l6 6-6 6-1.4-1.4Z"/></svg>
        </button>
        <section id="mobileControls" class="mobile-control-panel" aria-label="播放控制">
          <div class="mobile-time-row"><span id="mobileCurrentTime">0:00</span><span id="mobileDuration">0:00</span></div>
          <div class="mobile-seek-wrap"><input id="mobileSeek" type="range" min="0" max="1000" value="0" step="1" aria-label="播放进度"></div>
          <div class="mobile-action-row">
            <button id="mobileEpisodeButton" class="mobile-episode-button" type="button" aria-label="选择剧集">
              <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h12v2H4V5Zm0 6h9v2H4v-2Zm0 6h7v2H4v-2Zm14-7v4h-3l4 4 4-4h-3v-4h-2Z"/></svg>
              <span>选集 · <span id="mobileEpisodeLabel">第01集</span></span><span class="episode-chevron">›</span>
            </button>
            <div class="mobile-actions-right">
              <div class="mobile-tool-wrap">
                <button id="mobileQualityButton" class="mobile-tool-button" type="button" aria-label="选择清晰度" disabled><span id="mobileQualityLabel">原画</span></button>
                <div id="mobileQualityMenu" class="mobile-popup-menu" role="menu" hidden></div>
              </div>
              <button id="mobileMuteButton" class="mobile-tool-button" type="button" aria-label="静音">
                <svg class="volume-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4Zm12.5 3a3.5 3.5 0 0 0-2-3.15v6.3a3.5 3.5 0 0 0 2-3.15Zm-2-7.1v2.06a6 6 0 0 1 0 10.08v2.06a8 8 0 0 0 0-14.2Z"/></svg>
                <svg class="muted-icon" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 9v6h4l5 4V5L8 9H4Zm11.3.3L14 10.6l1.4 1.4-1.4 1.4 1.3 1.3 1.4-1.4 1.4 1.4 1.3-1.3L18 12l1.4-1.4-1.3-1.3-1.4 1.4-1.4-1.4Z"/></svg>
              </button>
              <button id="mobileFullscreenButton" class="mobile-tool-button" type="button" aria-label="全屏播放">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M7 3H3v4h2V5h2V3Zm14 4V3h-4v2h2v2h2ZM5 17H3v4h4v-2H5v-2Zm16 0h-2v2h-2v2h4v-4Z"/></svg>
              </button>
              <div class="mobile-tool-wrap">
                <button id="mobileMoreButton" class="mobile-tool-button" type="button" aria-label="更多播放设置">
                  <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 8a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Zm0 6a2 2 0 1 0 0-4 2 2 0 0 0 0 4Z"/></svg>
                </button>
                <div id="mobileMoreMenu" class="mobile-popup-menu" role="menu" hidden>
                  <button type="button" data-rate="0.75">0.75 倍速</button><button type="button" data-rate="1" class="active">正常速度</button><button type="button" data-rate="1.25">1.25 倍速</button><button type="button" data-rate="1.5">1.5 倍速</button><button type="button" data-rate="2">2 倍速</button>
                </div>
              </div>
            </div>
          </section>
        </div>
        <div id="episodeSheet" class="sheet" hidden aria-hidden="true">
          <button id="sheetBackdrop" class="sheet-backdrop" type="button" aria-label="关闭选集"></button>
          <section class="sheet-panel" role="dialog" aria-modal="true" aria-labelledby="sheetTitle">
            <header class="sheet-header"><span id="sheetTitle" class="sheet-title">选择剧集</span><button id="sheetClose" class="sheet-close" type="button" aria-label="关闭">×</button></header>
            <div id="episodeGrid" class="episode-grid"></div>
          </section>
        </div>
      </div>
      <div class="below-player">
        <label class="switch-label"><input id="autoNext" type="checkbox" checked><span class="switch"></span><span>自动连播</span></label>
        <p id="status"></p>
        <button id="retry" type="button" hidden>重试</button>
        <a id="direct" target="_blank" rel="noreferrer">线路</a>
      </div>
    </section>
    <section id="inlineEpisodes" class="inline-episodes" aria-labelledby="inlineEpisodesTitle">
      <header class="inline-episodes-header">
        <span id="inlineEpisodesTitle" class="inline-episodes-title">选集</span>
        <span class="inline-episodes-tools"><span id="episodeCount"></span><button id="episodeExpand" class="episode-expand-button" type="button" aria-label="展开选集" aria-expanded="false" hidden><svg viewBox="0 0 24 24" aria-hidden="true"><path d="m7.4 9.4 4.6 4.6 4.6-4.6L18 10.8l-6 6-6-6 1.4-1.4Z"/></svg></button></span>
      </header>
      <div id="inlineEpisodeGrid" class="inline-episode-grid"></div>
    </section>
  </main>
  <script src="https://cdn.jsdelivr.net/npm/hls.js@1/dist/hls.min.js"></script>
  <script src="../player.js?v=2.5.2"></script>
</body>
</html>"""


_PLAYER_JAVASCRIPT = r"""
(() => {
  'use strict';
  const video = document.querySelector('#video');
  const videoShell = document.querySelector('#videoShell');
  const previous = document.querySelector('#previous');
  const next = document.querySelector('#next');
  const videoActions = document.querySelector('.video-actions');
  const retry = document.querySelector('#retry');
  const autoNext = document.querySelector('#autoNext');
  const status = document.querySelector('#status');
  const direct = document.querySelector('#direct');
  const title = document.querySelector('#title');
  const meta = document.querySelector('#meta');
  const currentEpisode = document.querySelector('#currentEpisode');
  const episodeOpen = document.querySelector('#episodeOpen');
  const episodeSheet = document.querySelector('#episodeSheet');
  const episodeGrid = document.querySelector('#episodeGrid');
  const inlineEpisodes = document.querySelector('#inlineEpisodes');
  const inlineEpisodeGrid = document.querySelector('#inlineEpisodeGrid');
  const episodeCount = document.querySelector('#episodeCount');
  const sheetBackdrop = document.querySelector('#sheetBackdrop');
  const sheetClose = document.querySelector('#sheetClose');
  const qualityButton = document.querySelector('#qualityButton');
  const qualityLabel = document.querySelector('#qualityLabel');
  const qualityMenu = document.querySelector('#qualityMenu');
  const fullscreenButton = document.querySelector('#fullscreenButton');
  const holdSpeedHint = document.querySelector('#holdSpeedHint');
  const playbackOverlay = document.querySelector('#playbackOverlay');
  const mobileControls = document.querySelector('#mobileControls');
  const mobileCurrentTime = document.querySelector('#mobileCurrentTime');
  const mobileDuration = document.querySelector('#mobileDuration');
  const mobileSeek = document.querySelector('#mobileSeek');
  const mobileEpisodeButton = document.querySelector('#mobileEpisodeButton');
  const mobileEpisodeLabel = document.querySelector('#mobileEpisodeLabel');
  const mobileEpisodeButtonTop = document.querySelector('#mobileEpisodeButtonTop');
  const mobileEpisodeLabelTop = document.querySelector('#mobileEpisodeLabelTop');
  const mobileQualityButton = document.querySelector('#mobileQualityButton');
  const mobileQualityLabel = document.querySelector('#mobileQualityLabel');
  const mobileQualityMenu = document.querySelector('#mobileQualityMenu');
  const mobileMuteButton = document.querySelector('#mobileMuteButton');
  const mobileFullscreenButton = document.querySelector('#mobileFullscreenButton');
  const mobileMoreButton = document.querySelector('#mobileMoreButton');
  const mobileMoreMenu = document.querySelector('#mobileMoreMenu');
  const pageBackButton = document.querySelector('#pageBackButton');
  const pageMoreButton = document.querySelector('#pageMoreButton');
  const episodeExpand = document.querySelector('#episodeExpand');
  const mobileLayoutQuery = window.matchMedia('(max-width: 560px)');
  const VIDEO_ASPECT_CLASSES = [
    'player--cinema',
    'player--landscape',
    'player--square',
    'player--portrait',
  ];
  const token = location.pathname.split('/').filter(Boolean).pop() || 'unknown';
  const apiUrl = new URL(location.href);
  apiUrl.pathname = apiUrl.pathname.replace('/watch/', '/api/');
  apiUrl.search = '';
  apiUrl.hash = '';

  let data = null;
  let mediaStorageId = `session-${token}`;
  let current = 0;
  let hls = null;
  let usingFallback = false;
  let episodesExpanded = false;
  let nativeErrorHandler = null;
  function storageGet(key) {
    try { return localStorage.getItem(key); } catch (_error) { return null; }
  }

  function storageSet(key, value) {
    try { localStorage.setItem(key, value); } catch (_error) { /* private mode/quota */ }
  }

  function storageRemove(key) {
    try { localStorage.removeItem(key); } catch (_error) { /* private mode/quota */ }
  }

  function stableMediaId(body) {
    const identity = [body.title, body.source, body.cover]
      .map((value) => String(value || '').trim())
      .join('\n');
    let hash = 2166136261;
    for (let index = 0; index < identity.length; index += 1) {
      hash ^= identity.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0).toString(36);
  }

  let qualityPreference = storageGet('short-drama:quality') || 'auto';
  let lastProgressSecond = -1;
  let holdSpeedTimer = null;
  let holdSpeedHintTimer = null;
  let holdSpeedPointer = null;
  let holdSpeedOriginalRate = 1;
  let holdSpeedActive = false;
  let suppressVideoClickUntil = 0;
  let playbackOverlayTimer = null;
  let controlsHideTimer = null;
  let controlsPointerActive = false;

  const progressKey = (index) => `short-drama:history:${mediaStorageId}:${index}:time`;
  const episodeKey = () => `short-drama:history:${mediaStorageId}:episode`;
  const legacyProgressKey = (index) => `short-drama:${token}:${index}:time`;
  const legacyEpisodeKey = `short-drama:${token}:episode`;

  const savedAutoNext = storageGet('short-drama:auto-next');
  autoNext.checked = savedAutoNext === null ? true : savedAutoNext === 'true';

  function show(message, canRetry = false) {
    status.textContent = message || '';
    retry.hidden = !canRetry;
  }

  function updatePlayerViewportHeight() {
    const visibleHeight = window.visualViewport?.height || window.innerHeight;
    document.documentElement.style.setProperty(
      '--player-viewport-height',
      `${Math.max(320, Math.round(visibleHeight))}px`,
    );
  }

  function handlePlayerViewportResize() {
    updatePlayerViewportHeight();
    applyVideoAspect();
    syncNativeControls();
  }

  function destroyHls() {
    if (hls) { hls.destroy(); hls = null; }
    if (nativeErrorHandler) {
      video.removeEventListener('error', nativeErrorHandler);
      nativeErrorHandler = null;
    }
    qualityMenu.hidden = true;
    qualityMenu.textContent = '';
    qualityLabel.textContent = '原画';
    qualityButton.disabled = true;
    mobileQualityMenu.hidden = true;
    mobileQualityMenu.textContent = '';
    mobileQualityLabel.textContent = '原画';
    mobileQualityButton.disabled = true;
    video.removeAttribute('src');
    video.load();
    resetVideoAspect();
  }

  function updateButtons() {
    previous.disabled = current <= 0;
    next.disabled = !data || current >= data.episodes.length - 1;
    if (data) {
      const label = data.episodes[current]?.name || `第${current + 1}集`;
      currentEpisode.textContent = label;
      meta.textContent = label;
      mobileEpisodeLabel.textContent = label;
      mobileEpisodeLabelTop.textContent = label;
    }
    document.querySelectorAll('.episode-item').forEach((button) => {
      button.classList.toggle('active', Number(button.dataset.index) === current);
    });
  }

  function stopHoldSpeed() {
    if (holdSpeedTimer !== null) {
      window.clearTimeout(holdSpeedTimer);
      holdSpeedTimer = null;
    }
    holdSpeedPointer = null;
    if (holdSpeedHintTimer !== null) {
      window.clearTimeout(holdSpeedHintTimer);
      holdSpeedHintTimer = null;
    }
    holdSpeedHint.hidden = true;
    if (!holdSpeedActive) return;
    video.playbackRate = holdSpeedOriginalRate;
    holdSpeedActive = false;
    suppressVideoClickUntil = Date.now() + 350;
  }

  function showHoldSpeedHint() {
    if (holdSpeedHintTimer !== null) window.clearTimeout(holdSpeedHintTimer);
    holdSpeedHint.hidden = false;
    holdSpeedHintTimer = window.setTimeout(() => {
      holdSpeedHint.hidden = true;
      holdSpeedHintTimer = null;
    }, 1200);
  }

  function startHoldSpeed(event) {
    if ((event.pointerType === 'mouse' && event.button !== 0) || video.paused || video.ended) return;
    const bounds = video.getBoundingClientRect();
    const nativeControlsHeight = Math.min(72, Math.max(48, bounds.height * .18));
    if (event.clientY >= bounds.bottom - nativeControlsHeight) return;
    stopHoldSpeed();
    holdSpeedPointer = event.pointerId;
    holdSpeedTimer = window.setTimeout(() => {
      holdSpeedTimer = null;
      if (video.paused || video.ended) return;
      holdSpeedOriginalRate = video.playbackRate;
      video.playbackRate = 2;
      holdSpeedActive = true;
      showHoldSpeedHint();
    }, 350);
  }

  function finishHoldSpeed(event) {
    if (holdSpeedPointer !== null && event.pointerId !== holdSpeedPointer) return;
    stopHoldSpeed();
  }

  function videoAspectMode(width, height) {
    const ratio = Number(width) / Number(height);
    if (!Number.isFinite(ratio) || ratio <= 0) return 'landscape';
    if (ratio >= 1.9) return 'cinema';
    if (ratio >= 1.2) return 'landscape';
    if (ratio >= 0.8) return 'square';
    return 'portrait';
  }

  function resetVideoAspect() {
    videoShell.classList.remove(...VIDEO_ASPECT_CLASSES);
    videoShell.classList.add('player--landscape');
    videoShell.style.removeProperty('width');
    videoShell.style.removeProperty('--video-aspect');
    videoShell.style.removeProperty('--inline-stage-width');
  }

  function applyVideoAspect() {
    if (
      activeFullscreenElement() === videoShell
      || videoShell.classList.contains('is-fullscreen')
      || videoShell.classList.contains('pseudo-fullscreen')
    ) return;
    if (!video.videoWidth || !video.videoHeight) return;

    const ratio = video.videoWidth / video.videoHeight;
    const mode = videoAspectMode(video.videoWidth, video.videoHeight);
    videoShell.classList.remove(...VIDEO_ASPECT_CLASSES);
    videoShell.classList.add(`player--${mode}`);
    videoShell.style.removeProperty('width');
    videoShell.style.removeProperty('--video-aspect');
    videoShell.style.removeProperty('--inline-stage-width');

    if (mode === 'cinema' || mode === 'landscape') return;
    const availableWidth = Math.max(1, videoShell.parentElement?.clientWidth || 0);
    const viewportHeight = window.visualViewport?.height || window.innerHeight;
    const computedMaxHeight = parseFloat(getComputedStyle(videoShell).maxHeight);
    const cssMaxHeight = Number.isFinite(computedMaxHeight)
      ? computedMaxHeight
      : viewportHeight * .68;
    const maxHeight = Math.max(1, Math.min(viewportHeight * .68, cssMaxHeight));
    const stageWidth = Math.min(availableWidth, maxHeight * ratio);
    videoShell.style.setProperty('--video-aspect', String(ratio));
    videoShell.style.setProperty('--inline-stage-width', `${stageWidth}px`);
  }

  function activeFullscreenElement() {
    return document.fullscreenElement || document.webkitFullscreenElement || null;
  }

  function updateFullscreenState() {
    const active = activeFullscreenElement() === videoShell
      || videoShell.classList.contains('pseudo-fullscreen');
    videoShell.classList.toggle('is-fullscreen', active);
    fullscreenButton.setAttribute('aria-label', active ? '退出全屏' : '全屏播放');
    mobileFullscreenButton.setAttribute('aria-label', active ? '退出全屏' : '全屏播放');
    syncNativeControls();
    if (!active) applyVideoAspect();
  }

  function syncNativeControls() {
    video.controls = true;
    clearControlsHideTimer();
    videoActions.classList.remove('controls-hidden');
  }

  function formatTime(seconds) {
    if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
    const whole = Math.floor(seconds);
    const hours = Math.floor(whole / 3600);
    const minutes = Math.floor((whole % 3600) / 60);
    const remaining = String(whole % 60).padStart(2, '0');
    return hours > 0
      ? `${hours}:${String(minutes).padStart(2, '0')}:${remaining}`
      : `${minutes}:${remaining}`;
  }

  function updateMobileProgress() {
    const duration = Number.isFinite(video.duration) ? video.duration : 0;
    const currentTime = Number.isFinite(video.currentTime) ? video.currentTime : 0;
    const ratio = duration > 0 ? Math.min(1, Math.max(0, currentTime / duration)) : 0;
    mobileCurrentTime.textContent = formatTime(currentTime);
    mobileDuration.textContent = formatTime(duration);
    mobileSeek.value = String(Math.round(ratio * 1000));
    mobileSeek.style.setProperty('--seek-progress', `${ratio * 100}%`);
    mobileSeek.disabled = duration <= 0;
  }

  function updateMuteButton() {
    const muted = video.muted || video.volume === 0;
    mobileMuteButton.classList.toggle('is-muted', muted);
    mobileMuteButton.setAttribute('aria-label', muted ? '取消静音' : '静音');
  }

  function isInlineMobile() {
    return mobileLayoutQuery.matches
      && !activeFullscreenElement()
      && !videoShell.classList.contains('pseudo-fullscreen')
      && !videoShell.classList.contains('is-fullscreen');
  }

  function clearControlsHideTimer() {
    if (controlsHideTimer !== null) {
      window.clearTimeout(controlsHideTimer);
      controlsHideTimer = null;
    }
  }

  function scheduleControlsHide() {
    clearControlsHideTimer();
    if (!isInlineMobile() || video.paused || controlsPointerActive || !mobileMoreMenu.hidden || !qualityMenu.hidden) return;
    controlsHideTimer = window.setTimeout(() => {
      if (!video.paused && !controlsPointerActive && mobileMoreMenu.hidden && qualityMenu.hidden) {
        videoActions.classList.add('controls-hidden');
      }
      controlsHideTimer = null;
    }, 2800);
  }

  function showControls() {
    videoActions.classList.remove('controls-hidden');
    scheduleControlsHide();
  }

  function beginControlsInteraction() {
    controlsPointerActive = true;
    clearControlsHideTimer();
    showControls();
  }

  function endControlsInteraction() {
    controlsPointerActive = false;
    scheduleControlsHide();
  }

  function updatePlaybackOverlay() {
    if (playbackOverlayTimer !== null) {
      window.clearTimeout(playbackOverlayTimer);
      playbackOverlayTimer = null;
    }
    playbackOverlay.hidden = false;
    playbackOverlay.classList.toggle('is-playing', !video.paused);
    if (video.paused) showControls();
    else scheduleControlsHide();
  }

  async function togglePlayerFullscreen() {
    const active = activeFullscreenElement() === videoShell
      || videoShell.classList.contains('pseudo-fullscreen');
    if (active) {
      if (document.exitFullscreen && activeFullscreenElement()) await document.exitFullscreen();
      else if (document.webkitExitFullscreen && activeFullscreenElement()) document.webkitExitFullscreen();
      else videoShell.classList.remove('pseudo-fullscreen');
      updateFullscreenState();
      return;
    }

    try {
      if (videoShell.requestFullscreen) await videoShell.requestFullscreen({ navigationUI: 'hide' });
      else if (videoShell.webkitRequestFullscreen) videoShell.webkitRequestFullscreen();
      else videoShell.classList.add('pseudo-fullscreen');
    } catch (_error) {
      videoShell.classList.add('pseudo-fullscreen');
    }
    updateFullscreenState();
  }

  function closeEpisodeSheet() {
    episodeSheet.hidden = true;
    episodeSheet.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('sheet-open');
  }

  function openEpisodeSheet() {
    if (!data) return;
    qualityMenu.hidden = true;
    mobileQualityMenu.hidden = true;
    mobileMoreMenu.hidden = true;
    episodeSheet.hidden = false;
    episodeSheet.setAttribute('aria-hidden', 'false');
    document.body.classList.add('sheet-open');
    const active = episodeGrid.querySelector('.episode-item.active');
    if (active) active.scrollIntoView({ block: 'center' });
    sheetClose.focus();
  }

  function openEpisodeSelector() {
    const fullscreen = activeFullscreenElement() === videoShell
      || videoShell.classList.contains('pseudo-fullscreen');
    if (fullscreen) {
      openEpisodeSheet();
      return;
    }
    inlineEpisodes.scrollIntoView({ behavior: 'smooth', block: 'start' });
  }

  function episodeButtonLabel(episode, index) {
    const name = episode.name || `第${index + 1}集`;
    const match = /^\u7b2c\s*0*(\d+)\s*\u96c6$/.exec(name);
    return match ? String(Number(match[1])) : name;
  }

  function createEpisodeButton(episode, index, inSheet) {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'episode-item';
    button.dataset.index = String(index);
    button.textContent = episodeButtonLabel(episode, index);
    button.setAttribute('aria-label', episode.name || `第${index + 1}集`);
    button.addEventListener('click', () => {
      if (inSheet) closeEpisodeSheet();
      loadEpisode(index, true);
      if (!inSheet) videoShell.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
    return button;
  }

  function renderEpisodes() {
    episodesExpanded = false;
    episodeGrid.textContent = '';
    inlineEpisodeGrid.textContent = '';
    episodeCount.textContent = `共${data.episodes.length}集`;
    const canExpand = data.episodes.length > 10;
    inlineEpisodes.classList.toggle('is-collapsed', canExpand);
    inlineEpisodes.classList.remove('is-expanded');
    episodeExpand.hidden = !canExpand;
    episodeExpand.setAttribute('aria-expanded', 'false');
    episodeExpand.setAttribute('aria-label', '展开选集');
    data.episodes.forEach((episode, index) => {
      episodeGrid.appendChild(createEpisodeButton(episode, index, true));
      inlineEpisodeGrid.appendChild(createEpisodeButton(episode, index, false));
    });
  }

  function levelName(level) {
    if (level?.height) return `${level.height}P`;
    if (level?.bitrate) return `${Math.round(level.bitrate / 1000)}K`;
    return '清晰度';
  }

  function updateQualityActive(forcedLevel = null) {
    const manualLevel = !hls?.autoLevelEnabled
      ? (hls.currentLevel >= 0 ? hls.currentLevel : forcedLevel)
      : null;
    [qualityMenu, mobileQualityMenu].forEach((menu) => menu.querySelectorAll('button').forEach((button) => {
      const autoActive = button.dataset.level === 'auto' && hls?.autoLevelEnabled;
      const levelActive = button.dataset.level !== 'auto'
        && !hls?.autoLevelEnabled
        && Number(button.dataset.level) === manualLevel;
      button.classList.toggle('active', Boolean(autoActive || levelActive));
    }));
    let label = '原画';
    if (hls && hls.levels.length > 1) {
      label = hls.autoLevelEnabled ? '自动' : levelName(hls.levels[manualLevel]);
    }
    qualityLabel.textContent = label;
    mobileQualityLabel.textContent = label;
  }

  function chooseQuality(value) {
    if (!hls) return;
    if (value === 'auto') {
      hls.currentLevel = -1;
      qualityPreference = 'auto';
    } else {
      const index = Number(value);
      const level = hls.levels[index];
      if (!level) return;
      hls.currentLevel = index;
      qualityPreference = level.height ? `height:${level.height}` : `bitrate:${level.bitrate}`;
    }
    storageSet('short-drama:quality', qualityPreference);
    qualityMenu.hidden = true;
    mobileQualityMenu.hidden = true;
    updateQualityActive(value === 'auto' ? null : Number(value));
  }

  function setupQualityMenu() {
    qualityMenu.textContent = '';
    mobileQualityMenu.textContent = '';
    const levels = hls?.levels || [];
    if (levels.length <= 1) {
      qualityButton.disabled = true;
      mobileQualityButton.disabled = true;
      qualityLabel.textContent = '原画';
      mobileQualityLabel.textContent = '原画';
      return;
    }
    qualityButton.disabled = false;
    mobileQualityButton.disabled = false;
    const entries = [
      { index: 'auto', label: '自动' },
      ...levels.map((level, index) => ({ index: String(index), label: levelName(level) })).reverse(),
    ];
    entries.forEach((entry) => {
      [qualityMenu, mobileQualityMenu].forEach((menu) => {
        const button = document.createElement('button');
        button.type = 'button';
        button.dataset.level = entry.index;
        button.textContent = entry.label;
        button.addEventListener('click', () => chooseQuality(entry.index));
        menu.appendChild(button);
      });
    });

    let preferredLevel = null;
    if (qualityPreference.startsWith('height:')) {
      const wanted = Number(qualityPreference.split(':')[1]);
      const match = levels.findIndex((level) => level.height === wanted);
      hls.currentLevel = match >= 0 ? match : -1;
      preferredLevel = match >= 0 ? match : null;
    } else if (qualityPreference.startsWith('bitrate:')) {
      const wanted = Number(qualityPreference.split(':')[1]);
      const match = levels.findIndex((level) => level.bitrate === wanted);
      hls.currentLevel = match >= 0 ? match : -1;
      preferredLevel = match >= 0 ? match : null;
    } else {
      hls.currentLevel = -1;
    }
    updateQualityActive(preferredLevel);
  }

  function restoreTime(index) {
    const seconds = Number(storageGet(progressKey(index)) || 0);
    if (Number.isFinite(seconds) && seconds > 2) {
      const apply = () => {
        if (!Number.isFinite(video.duration) || seconds < video.duration - 10) {
          video.currentTime = seconds;
        }
      };
      if (video.readyState >= 1) apply();
      else video.addEventListener('loadedmetadata', apply, { once: true });
    }
  }

  function saveCurrentProgress() {
    if (!data || !Number.isFinite(video.currentTime) || video.currentTime <= 2) return;
    if (video.ended || (Number.isFinite(video.duration) && video.currentTime >= video.duration - 10)) {
      storageRemove(progressKey(current));
      return;
    }
    storageSet(progressKey(current), String(video.currentTime));
  }

  function migrateLegacyHistory() {
    const currentEpisodeKey = episodeKey();
    if (storageGet(currentEpisodeKey) === null) {
      const legacyEpisode = storageGet(legacyEpisodeKey);
      if (legacyEpisode !== null) storageSet(currentEpisodeKey, legacyEpisode);
    }
    for (let index = 0; index < data.episodes.length; index += 1) {
      const currentKey = progressKey(index);
      if (storageGet(currentKey) === null) {
        const legacyProgress = storageGet(legacyProgressKey(index));
        if (legacyProgress !== null) storageSet(currentKey, legacyProgress);
      }
    }
  }

  function loadEpisode(index, shouldPlay = false, forceFallback = false) {
    if (!data || index < 0 || index >= data.episodes.length) return;
    saveCurrentProgress();
    destroyHls();
    current = index;
    const episode = data.episodes[index];
    usingFallback = Boolean(forceFallback && episode.fallback_url);
    const playbackUrl = usingFallback ? episode.fallback_url : episode.url;
    direct.href = episode.url;
    direct.title = `${episode.name} 直链`;
    storageSet(episodeKey(), String(index));
    const shareUrl = new URL(location.href);
    shareUrl.searchParams.set('ep', String(index + 1));
    shareUrl.searchParams.delete('force');
    history.replaceState(null, '', shareUrl);
    updateButtons();
    show(`正在载入 ${episode.name}…`);
    lastProgressSecond = -1;
    updateMobileProgress();

    if (window.Hls && window.Hls.isSupported()) {
      hls = new window.Hls({ enableWorker: true });
      hls.loadSource(playbackUrl);
      hls.attachMedia(video);
      hls.on(window.Hls.Events.MANIFEST_PARSED, () => {
        setupQualityMenu();
        show('');
        restoreTime(index);
        if (shouldPlay) video.play().catch(() => {});
      });
      hls.on(window.Hls.Events.LEVEL_SWITCHED, () => updateQualityActive());
      hls.on(window.Hls.Events.ERROR, (_event, info) => {
        if (!info.fatal) return;
        if (
          info.type === window.Hls.ErrorTypes.NETWORK_ERROR
          && episode.fallback_url
          && !usingFallback
        ) {
          show('直连失败，正在切换备用线路…');
          loadEpisode(index, true, true);
          return;
        }
        if (info.type === window.Hls.ErrorTypes.MEDIA_ERROR) hls.recoverMediaError();
        else show('播放线路载入失败', true);
      });
      return;
    }

    if (video.canPlayType('application/vnd.apple.mpegurl')) {
      nativeErrorHandler = () => {
        nativeErrorHandler = null;
        if (episode.fallback_url && !usingFallback) {
          show('直连失败，正在切换备用线路…');
          loadEpisode(index, true, true);
          return;
        }
        show('播放线路载入失败', true);
      };
      video.addEventListener('error', nativeErrorHandler, { once: true });
      video.src = playbackUrl;
      video.addEventListener('loadedmetadata', () => {
        qualityLabel.textContent = '原画';
        mobileQualityLabel.textContent = '原画';
        show('');
        restoreTime(index);
        if (shouldPlay) video.play().catch(() => {});
      }, { once: true });
      return;
    }
    show('当前浏览器不支持 HLS 播放', true);
  }

  fetch(apiUrl, { cache: 'no-store', credentials: 'omit' })
    .then((response) => response.json().then((body) => ({ response, body })))
    .then(({ response, body }) => {
      if (!response.ok || !body.ok) throw new Error(body.error || '剧集载入失败');
      data = body;
      mediaStorageId = stableMediaId(body);
      migrateLegacyHistory();
      title.textContent = body.title;
      document.title = body.title;
      renderEpisodes();
      const pageUrl = new URL(location.href);
      const requested = Number(pageUrl.searchParams.get('ep')) - 1;
      const forced = pageUrl.searchParams.get('force') === '1';
      const saved = Number(storageGet(episodeKey()));
      const hasRequested = Number.isInteger(requested) && requested >= 0;
      const hasSaved = Number.isInteger(saved) && saved >= 0;
      const initial = forced && hasRequested
        ? requested
        : (hasSaved ? saved : (hasRequested ? requested : 0));
      loadEpisode(Math.min(initial, body.episodes.length - 1));
    })
    .catch((error) => show(error.message || '剧集载入失败', true));

  previous.addEventListener('click', () => loadEpisode(current - 1, true));
  next.addEventListener('click', () => loadEpisode(current + 1, true));
  fullscreenButton.addEventListener('click', togglePlayerFullscreen);
  mobileFullscreenButton.addEventListener('click', togglePlayerFullscreen);
  retry.addEventListener('click', () => loadEpisode(current, true));
  episodeOpen.addEventListener('click', openEpisodeSelector);
  mobileEpisodeButton.addEventListener('click', openEpisodeSelector);
  mobileEpisodeButtonTop.addEventListener('click', openEpisodeSelector);
  pageBackButton.addEventListener('click', () => {
    if (history.length > 1) history.back();
  });
  pageMoreButton.addEventListener('click', () => mobileMoreButton.click());
  episodeExpand.addEventListener('click', () => {
    episodesExpanded = !episodesExpanded;
    inlineEpisodes.classList.toggle('is-collapsed', !episodesExpanded);
    inlineEpisodes.classList.toggle('is-expanded', episodesExpanded);
    episodeExpand.setAttribute('aria-expanded', String(episodesExpanded));
    episodeExpand.setAttribute('aria-label', episodesExpanded ? '收起选集' : '展开选集');
  });
  sheetBackdrop.addEventListener('click', closeEpisodeSheet);
  sheetClose.addEventListener('click', closeEpisodeSheet);
  qualityButton.addEventListener('click', (event) => {
    event.stopPropagation();
    if (!qualityButton.disabled) {
      qualityMenu.hidden = !qualityMenu.hidden;
      showControls();
      if (!qualityMenu.hidden) clearControlsHideTimer();
    }
  });
  mobileQualityButton.addEventListener('click', (event) => {
    event.stopPropagation();
    mobileMoreMenu.hidden = true;
    if (!mobileQualityButton.disabled) mobileQualityMenu.hidden = !mobileQualityMenu.hidden;
  });
  mobileMuteButton.addEventListener('click', () => {
    video.muted = !video.muted;
    updateMuteButton();
    showControls();
  });
  mobileMoreButton.addEventListener('click', (event) => {
    event.stopPropagation();
    mobileQualityMenu.hidden = true;
    mobileMoreMenu.hidden = !mobileMoreMenu.hidden;
    showControls();
    if (!mobileMoreMenu.hidden) clearControlsHideTimer();
  });
  mobileMoreMenu.querySelectorAll('[data-rate]').forEach((button) => {
    button.addEventListener('click', () => {
      video.playbackRate = Number(button.dataset.rate) || 1;
      mobileMoreMenu.querySelectorAll('[data-rate]').forEach((item) => {
        item.classList.toggle('active', item === button);
      });
      mobileMoreMenu.hidden = true;
      showControls();
    });
  });
  document.addEventListener('click', (event) => {
    if (!qualityMenu.contains(event.target) && event.target !== qualityButton) {
      const wasOpen = !qualityMenu.hidden;
      qualityMenu.hidden = true;
      if (wasOpen) scheduleControlsHide();
    }
    if (!mobileQualityMenu.contains(event.target) && event.target !== mobileQualityButton) mobileQualityMenu.hidden = true;
    if (!mobileMoreMenu.contains(event.target) && event.target !== mobileMoreButton) {
      const wasOpen = !mobileMoreMenu.hidden;
      mobileMoreMenu.hidden = true;
      if (wasOpen) scheduleControlsHide();
    }
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') closeEpisodeSheet();
  });
  autoNext.addEventListener('change', () => {
    storageSet('short-drama:auto-next', String(autoNext.checked));
  });
  video.addEventListener('loadedmetadata', () => {
    applyVideoAspect();
    updateMobileProgress();
  });
  video.addEventListener('durationchange', updateMobileProgress);
  video.addEventListener('timeupdate', updateMobileProgress);
  video.addEventListener('volumechange', updateMuteButton);
  video.addEventListener('play', updatePlaybackOverlay);
  video.addEventListener('pause', updatePlaybackOverlay);
  mobileControls.addEventListener('pointerdown', beginControlsInteraction);
  window.addEventListener('pointerup', endControlsInteraction);
  window.addEventListener('pointercancel', endControlsInteraction);
  videoActions.addEventListener('pointerdown', (event) => {
    if (event.target.closest('button, input, .mobile-popup-menu')) beginControlsInteraction();
  }, true);
  mobileSeek.addEventListener('input', () => {
    if (!Number.isFinite(video.duration) || video.duration <= 0) return;
    video.currentTime = (Number(mobileSeek.value) / 1000) * video.duration;
    updateMobileProgress();
    showControls();
  });
  video.addEventListener('pointerdown', () => {
    if (isInlineMobile() && videoActions.classList.contains('controls-hidden')) showControls();
  });
  video.addEventListener('pointerdown', startHoldSpeed);
  window.addEventListener('pointerup', finishHoldSpeed);
  window.addEventListener('pointercancel', finishHoldSpeed);
  video.addEventListener('pointerleave', (event) => {
    if (event.pointerType === 'mouse') finishHoldSpeed(event);
  });
  video.addEventListener('contextmenu', (event) => event.preventDefault());
  video.addEventListener('click', (event) => {
    if (Date.now() < suppressVideoClickUntil) {
      event.preventDefault();
      event.stopImmediatePropagation();
      return;
    }
    if (mobileLayoutQuery.matches && !videoShell.classList.contains('is-fullscreen')) {
      event.preventDefault();
      if (videoActions.classList.contains('controls-hidden')) {
        showControls();
        return;
      }
      if (video.paused) video.play().catch(() => {});
      else video.pause();
      showControls();
    }
  }, true);
  playbackOverlay.addEventListener('click', (event) => {
    event.preventDefault();
    event.stopPropagation();
    if (video.paused) video.play().catch(() => {});
    else video.pause();
    showControls();
  });
  updatePlayerViewportHeight();
  syncNativeControls();
  updateMobileProgress();
  updateMuteButton();
  updatePlaybackOverlay();
  window.addEventListener('resize', handlePlayerViewportResize);
  mobileLayoutQuery.addEventListener?.('change', syncNativeControls);
  window.visualViewport?.addEventListener('resize', handlePlayerViewportResize);
  document.addEventListener('fullscreenchange', updateFullscreenState);
  document.addEventListener('webkitfullscreenchange', updateFullscreenState);
  video.addEventListener('timeupdate', () => {
    const second = Math.floor(video.currentTime);
    if (second >= 0 && (lastProgressSecond < 0 || second - lastProgressSecond >= 5)) {
      lastProgressSecond = second;
      saveCurrentProgress();
    }
  });
  video.addEventListener('seeked', saveCurrentProgress);
  window.addEventListener('pagehide', saveCurrentProgress);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') saveCurrentProgress();
  });
  video.addEventListener('ended', () => {
    storageRemove(progressKey(current));
    if (autoNext.checked && data && current < data.episodes.length - 1) {
      loadEpisode(current + 1, true);
    }
  });
})();
""".strip()
