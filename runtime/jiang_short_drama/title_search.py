"""Shared title matching and bounded catalog query expansion.

Only matching keys change here; callers retain the user's query and source titles.
"""

from __future__ import annotations

import re
import unicodedata

EXACT_TITLE_SCORE = 10_000
DEFAULT_MIN_FUZZY_SCORE = 3_000
MAX_FALLBACK_TERMS = 4

_KNOWN_TITLE_HINTS = {
    "全球杀机": ("全球杀戮",),
    "腊肉": ("致命的腊肉", "腊肉风云"),
}
_NOISE_RE = re.compile(
    r"(?:全集|高清|超清|蓝光|修复版|国语|中字|中文字幕|完整版|无删减|"
    r"4k|8k|bd|hd|web-?dl|webrip|bluray)$", re.IGNORECASE,
)
_BRACKET_NOISE_RE = re.compile(
    r"[（(【\[].*?(?:全集|高清|超清|4k|8k|国语|中字|完整版).*?[）)】\]]",
    re.IGNORECASE,
)
_EQUIVALENT_CHARACTERS = str.maketrans({"嬤": "嬷", "嫲": "嬷"})
_VERSION_RE = re.compile(r"\d+|第[一二三四五六七八九十百]+[部季]|[一二三四五六七八九十]+(?=[部季])")


def normalize_title(value: str) -> str:
    """Reuse the plugin's quality-noise cleanup, with limited character variants."""
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = _BRACKET_NOISE_RE.sub("", text)
    while True:
        cleaned = _NOISE_RE.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text.translate(_EQUIVALENT_CHARACTERS))


def _edit_distance(left: str, right: str, limit: int) -> int:
    """Bounded Levenshtein distance; discard distant pairs before full traversal."""
    if abs(len(left) - len(right)) > limit:
        return limit + 1
    previous = list(range(len(right) + 1))
    for index, char in enumerate(left, 1):
        current = [index] + [limit + 1] * len(right)
        for column in range(max(1, index - limit), min(len(right), index + limit) + 1):
            current[column] = min(previous[column] + 1, current[column - 1] + 1,
                                  previous[column - 1] + (char != right[column - 1]))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


def title_score(query: str, candidate: str, *, candidate_year: str = "") -> int:
    """Rank exact names first and admit only close, sufficiently long fuzzy names."""
    wanted, actual = normalize_title(query), normalize_title(candidate)
    if not wanted or not actual:
        return 0
    if wanted == actual:
        return EXACT_TITLE_SCORE
    wanted_versions, actual_versions = _VERSION_RE.findall(wanted), _VERSION_RE.findall(actual)
    if wanted_versions and not actual_versions:
        # Catalogs usually store the year outside the title, unlike sequel numbers.
        if (len(wanted_versions) == 1 and re.fullmatch(r"(?:19|20)\d{2}", wanted_versions[0])
                and wanted_versions[0] == str(candidate_year).strip()):
            actual += wanted_versions[0]
            actual_versions = wanted_versions
            if actual == wanted:
                return 9_000
        else:
            return 0
    if wanted_versions and actual_versions and wanted_versions != actual_versions:
        return 0
    difference = abs(len(actual) - len(wanted))
    if wanted in actual:
        return (7_000 - difference * 20) if actual.startswith(wanted) else (4_000 - difference * 25)
    if actual in wanted:
        # A short generic name such as 情书 is not the full work named by a long query.
        if len(actual) >= 4 and len(actual) / len(wanted) >= 0.6:
            return 5_500 - difference * 30
        return 0
    shortest, longest = sorted((wanted, actual), key=len)
    limit = 2 if len(shortest) >= 7 else 1 if len(shortest) >= 4 else 0
    if not limit:
        return 0
    distance = _edit_distance(wanted, actual, limit)
    if distance <= limit:
        return 6_500 - distance * 500 - difference * 30
    # Preserve the existing typo-plus-subtitle behavior, using bounded edit distance.
    # Scan only a few leading/trailing characters when the input is the longer name.
    if len(wanted) <= len(actual):
        windows = (actual[index:index + len(wanted)] for index in range(len(actual) - len(wanted) + 1))
    elif difference <= 3 and len(shortest) / len(longest) >= 0.65:
        windows = (wanted[index:index + len(actual)] for index in range(difference + 1))
    else:
        return 0
    if any(_edit_distance(shortest, window, limit) <= limit for window in windows):
        return 4_500 - difference * 30
    return 0


def known_title_hints(query: str) -> tuple[str, ...]:
    return _KNOWN_TITLE_HINTS.get(normalize_title(query), ())


def fuzzy_search_terms(query: str) -> tuple[str, ...]:
    """Try at most four useful phrases; never issue arbitrary single Han characters."""
    hints = known_title_hints(query)
    if hints:
        return hints
    text = unicodedata.normalize("NFKC", query or "").strip()
    if len(normalize_title(text)) < 4:
        return ()
    terms: list[str] = []

    def add(value: str) -> None:
        value = value.strip()
        if (len(normalize_title(value)) >= 3 and value != text and value not in terms
                and len(terms) < MAX_FALLBACK_TERMS):
            terms.append(value)

    canonical = text.translate(_EQUIVALENT_CHARACTERS)
    # Keep Latin word boundaries intact when shortening a mixed or Latin title.
    if re.search(r"[a-zA-Z]", canonical):
        words = canonical.split()
        add(canonical)
        if len(words) > 1:
            add(" ".join(words[1:]))
            add(" ".join(words[:-1]))
        return tuple(terms)
    # Leading filler is common in pasted titles. Shorter independent halves can
    # also recall a record when an internal typo survives prefix trimming.
    add(canonical[1:])
    add(canonical[2:])
    size = max(3, len(canonical) // 2)
    add(canonical[-size:])
    add(canonical[:size])
    return tuple(terms)
