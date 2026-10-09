"""Small, live catalog search; indexing never widens the caller's permissions."""
from __future__ import annotations

import re
import unicodedata
from collections import Counter
from math import log1p, sqrt


# Conversation filler must not make an unrelated command look like a match.
_STOP_WORDS = frozenset("""
帮我 给我 请帮 请问 一下 一次 一个 一条 一部 一些 这个 那个 这些 那些
看看 看看吧 有没有 能不能 可以 可不可以 怎么 如何 什么 哪些 我想 我要
帮忙 现在 今天 当前 获取 查询 搜索 发送 返回 相关 功能 命令 机器人
please help the this that with for show find search get send
""".split())
_CHINESE = re.compile(r"[\u3400-\u9fff]+")
_LATIN = re.compile(r"[a-z0-9]+(?:[_-][a-z0-9]+)*")


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    text = re.sub(r"\[at:[^\]]+\]|@[\w-]+", " ", text)
    return " ".join(text.split())


def tokens(text: str) -> frozenset[str]:
    """Overlapping 2/3-character words keep unknown plugin names searchable."""
    result = set(_LATIN.findall(text))
    for word in tuple(result):
        result.update(re.split(r"[_-]", word))
    for match in _CHINESE.finditer(text):
        word = match.group()
        for size in (2, 3):
            result.update(word[i:i + size] for i in range(len(word) - size + 1))
    return frozenset(word for word in result if len(word) >= 2 and word not in _STOP_WORDS)


class CatalogQuery:
    def __init__(self, query: str):
        self.text = normalize(query)[:512]
        self.tokens = tokens(self.text)

    def score(self, text: str) -> int:
        normalized = normalize(text)
        return self._score(normalized, tokens(normalized))

    def _score(self, normalized, document_tokens, weights=None):
        if not self.text:
            return 1
        common = self.tokens & document_tokens
        score = sum((3 if len(word) >= 3 else 2) * (weights.get(word, 1) if weights else 1)
                    for word in common)
        # Full phrases and exact command names take priority over partial matches.
        # Latin words only match token boundaries ("cat" must not match "catalog").
        if self.text == normalized or (
            len(self.text) >= 2 and _CHINESE.search(self.text) and self.text in normalized
        ):
            score += 20 + len(self.text)
        return score

    def rank(self, items, primary, secondary=lambda item: "") -> list:
        documents = [(item, normalize(primary(item)), normalize(secondary(item))) for item in items]
        if not self.text:
            return [item for item, _, _ in documents]
        indexed = [(item, first, tokens(first), second, tokens(second))
                   for item, first, second in documents]
        frequency = Counter(word for _, _, first, _, second in indexed for word in first | second)
        weights = {word: log1p(len(indexed) / count) for word, count in frequency.items()}
        # Rare task words outrank common plugin boilerplate; long help text must
        # not win merely because it contains more matching fragments.
        scored = [(self._score(first, first_tokens, weights) * 3 / sqrt(1 + len(first_tokens) / 20)
                   + self._score(second, second_tokens, weights), item)
                  for item, first, first_tokens, second, second_tokens in indexed]
        return [item for score, item in sorted(scored, key=lambda row: row[0], reverse=True)
                if score > 0]


def match_score(query: str, text: str) -> int:
    return CatalogQuery(query).score(text)


def search_text(item: dict) -> str:
    """Index user-facing metadata, not JSON keys, permission flags or regex syntax."""
    fields = ("name", "display_name", "plugin_display_name", "plugin", "description",
              "effective_command", "original_command", "aliases", "examples")
    return " ".join(
        " ".join(str(part) for part in value) if isinstance(value, (list, tuple)) else str(value)
        for field in fields if (value := item.get(field))
    )
