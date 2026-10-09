"""构造不会把 m3u8 域名作为顶层页面打开的播放地址。"""

from __future__ import annotations

import base64
import json
import re
import zlib
from urllib.parse import quote, urlencode, urlsplit

from .search import Episode, SearchResult


DEFAULT_PLAYER_URL_TEMPLATE = "https://m3u8-player.cc/player?url={url}"

# WeChat's link-card/XML path is considerably less tolerant of long URLs than
# a normal browser.  Keep stateless selection links below a conservative
# ceiling so an oversized fragment is never downgraded to a plain URL message.
MAX_STATELESS_SELECTION_URL_LENGTH = 1800


def build_stateless_playlist_url(
    result: SearchResult,
    player_url: object,
    episode: Episode | None = None,
    *,
    force_episode: bool = False,
) -> str | None:
    """Embed a compressed playlist in the URL fragment.

    Fragments are never sent to Nginx, so the VPS serves only the small player
    HTML and never receives or proxies HLS URLs or video segments.
    """
    base_url = str(player_url or "").strip().rstrip("#")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or not result.episodes
    ):
        return None

    selected = episode or result.episodes[0]
    try:
        selected_index = result.episodes.index(selected)
    except ValueError:
        selected_index = 0
    payload = {
        "v": 1,
        "title": result.title,
        "source": result.source,
        "cover": result.cover_url,
        "episodes": [
            {"name": item.name, "url": item.url}
            for item in result.episodes
        ],
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    token = base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii")
    token = token.rstrip("=")
    fragment = urlencode(
        {
            "p": token,
            "ep": selected_index + 1,
            "force": 1 if force_episode else 0,
        }
    )
    return f"{base_url}#{fragment}"


def build_stateless_recommendations_url(
    recommendations: tuple[SearchResult, ...],
    player_url: object,
    heading: str = "最新短剧推荐",
) -> str | None:
    """Embed a compressed recommendation list in the URL fragment."""
    return _build_stateless_selection_url(
        recommendations,
        player_url,
        heading=heading,
        kind="recommendations",
    )


def build_stateless_search_url(
    results: tuple[SearchResult, ...],
    player_url: object,
    query: str,
    media_label: str = "影视资源",
) -> str | None:
    """Embed a relevance-ranked search result list in the URL fragment."""
    title = (query or "").strip() or "影视搜索"
    heading = f"搜索“{title}”"
    if media_label:
        heading = f"{media_label}｜{heading}"
    return _build_stateless_selection_url(
        results,
        player_url,
        heading=heading,
        kind="search",
    )


def build_selection_payload(
    results: tuple[SearchResult, ...],
    *,
    heading: str,
    kind: str,
) -> dict[str, object] | None:
    """Build the full selection payload used by stateless and remote pages.

    The remote short-link store uses this same representation, but unlike a
    URL fragment it can retain opaque CDN URLs for every episode.  Keeping the
    builder here prevents the two delivery paths from drifting apart.
    """
    items = [
        _selection_item_payload(item)
        for item in results
        if item.episodes
    ]
    if not items:
        return None
    return {
        "v": 1,
        "kind": kind,
        "heading": heading,
        "items": items,
    }


def _build_stateless_selection_url(
    results: tuple[SearchResult, ...],
    player_url: object,
    *,
    heading: str,
    kind: str,
) -> str | None:
    """Embed a compressed selectable work list in a URL fragment."""
    base_url = str(player_url or "").strip().rstrip("#")
    parsed = urlsplit(base_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.query
        or parsed.fragment
        or not results
    ):
        return None
    payload = build_selection_payload(results, heading=heading, kind=kind)
    if payload is None:
        return None

    full_url = _encode_selection_url(base_url, payload)
    if len(full_url) <= MAX_STATELESS_SELECTION_URL_LENGTH:
        return full_url

    # Search/recommendation pages should remain useful even when an upstream
    # source returns hundreds of episodes.  First use a URL template whenever
    # every episode URL is derived from its display name (a common CMS shape),
    # then progressively remove non-essential card metadata and retain only
    # the first playable episode for opaque URLs.  The page displays the real
    # episode count and labels the compact payload accordingly.
    compact_items = [
        _selection_item_payload(item, compact=True)
        for item in results
        if item.episodes
    ]
    candidate = {
        "v": 1,
        "kind": kind,
        "heading": heading,
        "compact": True,
        "items": compact_items,
    }
    candidate_url = _encode_selection_url(base_url, candidate)
    if len(candidate_url) <= MAX_STATELESS_SELECTION_URL_LENGTH:
        return candidate_url

    # Opaque CDN URLs can share a prefix/suffix while still carrying a large
    # list of random IDs.  Keeping that value table would make the card long
    # again, so retain full rule-based playlists and use the first episode for
    # opaque playlists while preserving every ranked result in the list.
    compact_first_items = [
        _selection_item_payload(
            item,
            compact=True,
            allow_url_templates=False,
        )
        for item in results
        if item.episodes
    ]
    candidate = {
        "v": 1,
        "kind": kind,
        "heading": heading,
        "compact": True,
        "items": compact_first_items,
    }
    candidate_url = _encode_selection_url(base_url, candidate)
    if len(candidate_url) <= MAX_STATELESS_SELECTION_URL_LENGTH:
        return candidate_url

    # An individual title/source can still be unusually long.  Keep a final
    # minimal item so the user gets a real selection page instead of a giant
    # URL body; if even that exceeds the limit, report no stateless URL and let
    # the caller use its normal text fallback.
    minimal_items = [
        _selection_item_payload(
            item,
            compact=True,
            minimal=True,
            allow_url_templates=False,
        )
        for item in results
        if item.episodes
    ]
    for keep in range(len(minimal_items), 0, -1):
        candidate = {
            "v": 1,
            "kind": kind,
            "heading": heading,
            "compact": True,
            "items": minimal_items[:keep],
        }
        candidate_url = _encode_selection_url(base_url, candidate)
        if len(candidate_url) <= MAX_STATELESS_SELECTION_URL_LENGTH:
            return candidate_url
    return None


def _selection_item_payload(
    item: SearchResult,
    *,
    compact: bool = False,
    minimal: bool = False,
    allow_url_templates: bool = True,
) -> dict[str, object]:
    episodes = tuple(item.episodes)
    payload: dict[str, object] = {
        "title": item.title,
        "source": item.source,
        "year": item.year,
        "category": item.category,
        "actor": item.actor,
        "director": item.director,
        "cover": item.cover_url,
        "episodes": [
            {"name": episode.name, "url": episode.url}
            for episode in episodes
        ],
    }
    if not compact:
        template = _episode_url_template(episodes)
        if template is not None:
            payload.pop("episodes")
            payload["episode_template"] = template
            payload.update(_episode_name_payload(episodes))
            payload["episode_count"] = len(episodes)
        else:
            value_template = _episode_url_value_template(episodes)
            if value_template is not None and allow_url_templates:
                payload.pop("episodes")
                payload["episode_url_template"] = value_template[0]
                payload["episode_url_values"] = value_template[1]
                payload.update(_episode_name_payload(episodes))
                payload["episode_count"] = len(episodes)
        return payload

    # Compact mode is specifically for oversized link cards.  Preserve enough
    # metadata to identify a work, while keeping the first playable episode so
    # clicking a result always enters a working player page.
    template = _episode_url_template(episodes)
    if minimal:
        payload = {
            "title": item.title,
            "source": item.source,
            "episode_count": len(episodes),
        }
        if template is not None:
            payload["episode_template"] = template
            payload.update(_episode_name_payload(episodes))
        else:
            value_template = _episode_url_value_template(episodes)
            if value_template is not None and allow_url_templates:
                payload["episode_url_template"] = value_template[0]
                payload["episode_url_values"] = value_template[1]
                payload.update(_episode_name_payload(episodes))
            else:
                payload["episodes"] = [
                    {"name": episodes[0].name, "url": episodes[0].url}
                ]
        return payload

    payload["episode_count"] = len(episodes)
    if template is not None:
        payload.pop("episodes")
        payload["episode_template"] = template
        payload.update(_episode_name_payload(episodes))
    else:
        value_template = _episode_url_value_template(episodes)
        if value_template is not None and allow_url_templates:
            payload.pop("episodes")
            payload["episode_url_template"] = value_template[0]
            payload["episode_url_values"] = value_template[1]
            payload.update(_episode_name_payload(episodes))
        else:
            payload["episodes"] = [
                {"name": episodes[0].name, "url": episodes[0].url}
            ]
            payload.pop("actor", None)
            payload.pop("director", None)
            payload.pop("cover", None)
    return payload


def _episode_name_payload(episodes: tuple[object, ...]) -> dict[str, object]:
    pattern = _numeric_episode_name_pattern(episodes)
    if pattern is not None:
        return {"episode_name_pattern": pattern}
    return {
        "episode_names": [
            str(getattr(episode, "name", ""))
            for episode in episodes
        ]
    }


def _numeric_episode_name_pattern(
    episodes: tuple[object, ...],
) -> dict[str, object] | None:
    """Compact a consecutive numeric episode-name sequence."""
    parsed: list[tuple[str, int, str, int]] = []
    for episode in episodes:
        name = str(getattr(episode, "name", ""))
        match = re.fullmatch(r"(.*?)(\d+)(\D*)", name)
        if match is None:
            return None
        digits = match.group(2)
        parsed.append((match.group(1), int(digits), match.group(3), len(digits)))
    if len(parsed) < 2:
        return None
    prefix, start, suffix, pad = parsed[0]
    if any(
        item_prefix != prefix
        or number != start + index
        or item_suffix != suffix
        or width < pad
        for index, (item_prefix, number, item_suffix, width) in enumerate(parsed)
    ):
        return None
    return {
        "prefix": prefix,
        "suffix": suffix,
        "start": start,
        "count": len(parsed),
        "pad": pad,
    }


def _episode_url_template(episodes: tuple[object, ...]) -> str | None:
    """Return a URL template when each URL is ``prefix + name + suffix``."""
    if len(episodes) < 2:
        return None
    names = [str(getattr(episode, "name", "")) for episode in episodes]
    urls = [str(getattr(episode, "url", "")) for episode in episodes]
    if not all(names) or not all(urls) or len(set(names)) != len(names):
        return None
    locations = [url.find(name) for name, url in zip(names, urls)]
    if any(location < 0 for location in locations):
        return None
    prefixes = [url[:location] for location, url in zip(locations, urls)]
    suffixes = [
        url[location + len(name):]
        for name, location, url in zip(names, locations, urls)
    ]
    if len(set(prefixes)) != 1 or len(set(suffixes)) != 1:
        return None
    prefix, suffix = prefixes[0], suffixes[0]
    if not prefix or not suffix:
        return None
    return f"{prefix}{{name}}{suffix}"


def _episode_url_value_template(
    episodes: tuple[object, ...],
) -> tuple[str, list[str]] | None:
    """Compact URLs that share a directory but do not contain episode names."""
    if len(episodes) < 2:
        return None
    urls = [str(getattr(episode, "url", "")) for episode in episodes]
    if not all(urls) or len(set(urls)) != len(urls):
        return None
    prefix = urls[0]
    for url in urls[1:]:
        common = 0
        limit = min(len(prefix), len(url))
        while common < limit and prefix[common] == url[common]:
            common += 1
        prefix = prefix[:common]
    if not prefix:
        return None
    max_suffix = min(len(url) - len(prefix) for url in urls)
    suffix = urls[0][len(urls[0]) - max_suffix:] if max_suffix > 0 else ""
    for url in urls[1:]:
        common = 0
        limit = min(len(suffix), len(url) - len(prefix))
        while common < limit and suffix[-1 - common] == url[-1 - common]:
            common += 1
        suffix = suffix[len(suffix) - common:] if common else ""
    values = [url[len(prefix):len(url) - len(suffix) if suffix else None] for url in urls]
    if not suffix or not all(values) or not all(
        url == f"{prefix}{value}{suffix}"
        for url, value in zip(urls, values)
    ):
        return None
    return f"{prefix}{{value}}{suffix}", values


def _encode_selection_url(base_url: str, payload: dict[str, object]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    token = base64.urlsafe_b64encode(zlib.compress(raw, level=9)).decode("ascii").rstrip("=")
    return f"{base_url}#r={quote(token, safe='')}"


def build_watch_url(
    result: SearchResult,
    configured_template: object = "",
    episode: Episode | None = None,
) -> str:
    """将目标集地址包进 HLS 播放页；配置为 ``direct`` 时才返回直链。"""
    target = episode or result.episodes[0]
    configured = str(configured_template or "").strip()
    if configured.lower() == "direct":
        return target.url

    template = configured or DEFAULT_PLAYER_URL_TEMPLATE
    try:
        return template.format(
            url=quote(target.url, safe=""),
            title=quote(result.title, safe=""),
            episode=quote(target.name, safe=""),
        )
    except (KeyError, ValueError):
        return DEFAULT_PLAYER_URL_TEMPLATE.format(
            url=quote(target.url, safe="")
        )
