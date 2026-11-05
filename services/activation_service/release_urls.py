"""Request URLs for mutable GitHub release metadata."""
from __future__ import annotations

import re
import urllib.parse
import uuid


def fresh_stable_manifest_url(url: str) -> str:
    """Avoid cached redirects after replacing the fixed stable release asset."""
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.fragment
            or not re.fullmatch(
                r"/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/stable/stable\.json",
                parsed.path,
            )):
        return url
    query = f"{parsed.query}&" if parsed.query else ""
    return urllib.parse.urlunsplit(parsed._replace(query=query + "_wrb_check=" + uuid.uuid4().hex))
