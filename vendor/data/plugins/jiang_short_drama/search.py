"""分类影视资源站搜索、标题匹配与播放地址解析。"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import re
import time
import unicodedata
from dataclasses import dataclass, replace
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import aiohttp


logger = logging.getLogger("jiang_short_drama")


@dataclass(frozen=True, slots=True)
class Source:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class Episode:
    name: str
    url: str


@dataclass(frozen=True, slots=True)
class SearchResult:
    title: str
    source: str
    episodes: tuple[Episode, ...]
    cover_url: str = ""
    year: str = ""
    actor: str = ""
    director: str = ""
    category: str = ""
    remarks: str = ""
    score: int = 0
    match_role: str = ""
    duration_seconds: float = 0.0
    is_full_version: bool = False


@dataclass(frozen=True, slots=True)
class Recommendation:
    result: SearchResult
    updated_at: str
    sort_key: str

    @property
    def title(self) -> str:
        return self.result.title

    @property
    def source(self) -> str:
        return self.result.source


@dataclass(frozen=True, slots=True)
class CollectionPage:
    query: str
    page: int
    items: tuple[SearchResult, ...]
    has_more: bool


# 来源取自用户提供的「短百影视」UserScript。只启用已验证能返回 JSON 的
# HTTPS 源；单源故障不会影响其他源。
DEFAULT_SOURCES: tuple[Source, ...] = (
    Source("暴风资源", "https://bfzyapi.com/api.php/provide/vod/"),
    Source("非凡资源", "https://cj.ffzyapi.com/api.php/provide/vod/"),
    Source("量子资源", "https://cj.lziapi.com/api.php/provide/vod/"),
    Source("红牛资源", "https://www.hongniuzy2.com/api.php/provide/vod/from/hnm3u8/"),
    Source("极速资源", "https://jszyapi.com/api.php/provide/vod"),
    Source("百度云资源", "https://api.apibdzy.com/api.php/provide/vod/"),
    Source("金鹰资源", "https://jyzyapi.com/provide/vod/from/jinyingm3u8/at/json"),
    Source("飘零资源", "https://p2100.net/api.php/provide/vod/"),
)

_MEDIA_COMMAND_RE = re.compile(
    r"^(?P<media_type>短剧|电视剧|电影|动漫|综艺|剧)"
    r"[ \t]+(?P<query>\S(?:.*\S)?)$"
)
_SPORTS_COMMAND_RE = re.compile(r"^体育[ \t]+(?P<query>\S(?:.*\S)?)$")
_RECOMMENDATION_COMMAND = "短剧推荐"
_MEDIA_HELP_COMMAND = "剧"
_SHORT_DRAMA_TYPE_RE = re.compile(r"(?:短剧|漫剧)")
MEDIA_TYPES = ("短剧", "电视剧", "电影", "动漫", "综艺")
_TV_CATEGORY_RE = re.compile(
    r"(?:电视剧|连续剧|国产剧|香港剧|台湾剧|欧美剧|日本剧|韩国剧|"
    r"泰国剧|海外剧|日剧|韩剧|港剧|台剧|陆剧|美剧|英剧|新马剧|马泰剧)"
)
_ANIME_CATEGORY_RE = re.compile(r"(?:动漫|动画|卡通|番剧)")
_VARIETY_CATEGORY_RE = re.compile(r"(?:综艺|真人秀|脱口秀)")
_MOVIE_CATEGORY_RE = re.compile(r"(?:电影|片)")
_EPISODE_REQUEST_RE = re.compile(
    r"^(?P<title>.+?)[ \t]+第(?P<number>[1-9]\d{0,2})集$"
)
_VARIANT_REQUEST_RE = re.compile(
    r"^(?P<title>.+?)[ \t]+版本(?:[ \t]*(?P<number>[1-9]\d?))?$"
)
_SPORTS_COLLECTION_RE = re.compile(
    r"^(?P<league>CBA|NBA|WNBA|NCAA|浙BA|F1|UFC|WWE|篮球|足球|斯诺克|台球|"
    r"网球|羽毛球|乒乓球|排球|英超|西甲|德甲|意甲|法甲|欧冠|中超|中甲|"
    r"亚冠|世界杯|欧洲杯|美洲杯|奥运会|亚运会)"
    r"(?:[ \t_-]*(?P<year>20\d{2}))?$",
    re.IGNORECASE,
)
_SPORTS_CATEGORY_ALIASES: dict[str, tuple[str, ...]] = {
    "cba": ("篮球", "体育"),
    "nba": ("篮球", "体育"),
    "wnba": ("篮球", "体育"),
    "ncaa": ("篮球", "体育"),
    "浙ba": ("篮球", "体育"),
    "篮球": ("篮球",),
    "足球": ("足球",),
    "英超": ("足球",),
    "西甲": ("足球",),
    "德甲": ("足球",),
    "意甲": ("足球",),
    "法甲": ("足球",),
    "欧冠": ("足球",),
    "中超": ("足球",),
    "中甲": ("足球",),
    "亚冠": ("足球",),
    "世界杯": ("足球", "体育"),
    "欧洲杯": ("足球", "体育"),
    "美洲杯": ("足球", "体育"),
    "斯诺克": ("斯诺克", "台球"),
    "台球": ("斯诺克", "台球"),
    "网球": ("网球",),
    "羽毛球": ("羽毛球",),
    "乒乓球": ("乒乓球",),
    "排球": ("排球",),
    "f1": ("赛车", "一级方程式", "体育"),
    "ufc": ("格斗", "体育"),
    "wwe": ("摔角", "格斗", "体育"),
    "奥运会": ("奥运", "体育"),
    "亚运会": ("亚运", "体育"),
}
_EPISODE_RANGE_RE = re.compile(
    r"第?\s*(?P<start>\d+)\s*(?:[-~～—至到]\s*(?P<end>\d+)\s*)?集"
)
EXACT_TITLE_SCORE = 10_000
DEFAULT_MIN_FUZZY_SCORE = 3_000
_PERSON_ACTOR_SCORE = 8_000
_PERSON_DIRECTOR_SCORE = 7_000
_KNOWN_TITLE_HINTS: dict[str, tuple[str, ...]] = {
    "全球杀机": ("全球杀戮",),
    "腊肉": ("致命的腊肉", "腊肉风云"),
}
_NOISE_RE = re.compile(
    r"(?:全集|高清|超清|蓝光|修复版|国语|中字|中文字幕|完整版|无删减|"
    r"4k|8k|bd|hd|web-?dl|webrip|bluray)$",
    re.IGNORECASE,
)
_BRACKET_NOISE_RE = re.compile(
    r"[（(【\[].*?(?:全集|高清|超清|4k|8k|国语|中字|完整版).*?[）)】\]]",
    re.IGNORECASE,
)
_VERSION_TITLE_SUFFIX_RE = re.compile(
    r"(?:动画版|动画|动漫版|电视剧版|电视剧|真人版|新版|老版)$",
    re.IGNORECASE,
)
_NON_VARIANT_RE = re.compile(r"(?:解说|预告|花絮|盘点|剪辑)")


def extract_query(text: str) -> str | None:
    """严格解析“短剧 + ASCII 空白 + 剧名”，不接受命令前缀或全角空格。"""
    parsed = extract_media_query(text)
    if parsed is None or parsed[0] != "短剧":
        return None
    return parsed[1]


def extract_media_query(text: str) -> tuple[str, str] | None:
    """严格解析分类或全分类影视命令，返回命令类型和查询词。"""
    match = _MEDIA_COMMAND_RE.fullmatch((text or "").rstrip())
    if not match:
        return None
    return match.group("media_type"), match.group("query")


def extract_sports_query(text: str) -> str | None:
    """严格解析“体育 + ASCII 空白 + 查询词”；单独“体育”不匹配。"""
    match = _SPORTS_COMMAND_RE.fullmatch((text or "").rstrip())
    if not match:
        return None
    return match.group("query")


def is_recommendation_command(text: str) -> bool:
    """只接受完全一致的“短剧推荐”，不容忍任何前后缀或空白。"""
    return (text or "") == _RECOMMENDATION_COMMAND


def is_media_help_command(text: str) -> bool:
    """仅精确消息“剧”触发影视命令帮助。"""
    return (text or "") == _MEDIA_HELP_COMMAND


def is_media_category(media_type: str, category: str) -> bool:
    """按命令类型严格判断资源站分类，不接受分类缺失的资源。"""
    normalized = normalize_title(category)
    if not normalized or media_type not in MEDIA_TYPES:
        return False
    if media_type == "短剧":
        return bool(_SHORT_DRAMA_TYPE_RE.search(normalized))
    if media_type == "电视剧":
        return bool(_TV_CATEGORY_RE.search(normalized)) and not (
            "短剧" in normalized
            or _ANIME_CATEGORY_RE.search(normalized)
            or _VARIETY_CATEGORY_RE.search(normalized)
        )
    if media_type == "电影":
        return bool(_MOVIE_CATEGORY_RE.search(normalized)) and not (
            "解说" in normalized or "预告" in normalized
        )
    if media_type == "动漫":
        return bool(_ANIME_CATEGORY_RE.search(normalized))
    return bool(_VARIETY_CATEGORY_RE.search(normalized))


def _recommendation_sort_key(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if raw.isdigit() and len(raw) == 10:
        try:
            return datetime.fromtimestamp(int(raw)).strftime("%Y%m%d%H%M%S")
        except (OSError, OverflowError, ValueError):
            return ""
    digits = re.sub(r"\D", "", raw)
    return digits[:14].ljust(14, "0") if len(digits) >= 8 else ""


def _sports_replay_sort_key(title: str) -> str:
    dates = re.findall(r"20\d{6}", normalize_title(title))
    return max(dates) if dates else ""


def _media_type_ids(
    classes: Iterable[dict[str, Any]],
    media_type: str,
) -> set[str]:
    rows = [row for row in classes if isinstance(row, dict)]
    selected = {
        str(row.get("type_id"))
        for row in rows
        if _is_recommendation_category(
            media_type,
            str(row.get("type_name") or ""),
        )
    }
    while True:
        descendants = {
            str(row.get("type_id"))
            for row in rows
            if str(row.get("type_pid")) in selected
        }
        expanded = selected | descendants
        if expanded == selected:
            return selected
        selected = expanded


def _is_recommendation_category(media_type: str, category: str) -> bool:
    if media_type == "体育":
        return is_sports_category(category)
    return is_media_category(media_type, category)


def recommendation_candidates(
    payload: dict[str, Any],
    source_name: str,
    media_type: str | None = "短剧",
) -> list[Recommendation]:
    """提取指定分类中有 HLS 播放地址的最新资源并保留更新时间。"""
    type_ids = (
        _media_type_ids(payload.get("class") or (), media_type)
        if media_type
        else set()
    )
    candidates: list[Recommendation] = []
    for item in payload.get("list") or ():
        if not isinstance(item, dict):
            continue
        type_name = str(item.get("type_name") or "")
        type_id = str(item.get("type_id") or "")
        if media_type and type_id not in type_ids and not _is_recommendation_category(
            media_type,
            type_name,
        ):
            continue
        result = _result_from_item(item, source_name, EXACT_TITLE_SCORE)
        if result is None:
            continue
        updated_at = str(
            item.get("vod_time")
            or item.get("vod_time_add")
            or item.get("vod_addtime")
            or ""
        ).strip()
        candidates.append(
            Recommendation(
                result=result,
                updated_at=updated_at,
                sort_key=_recommendation_sort_key(updated_at),
            )
        )
    return candidates


def parse_episode_request(query: str) -> tuple[str, int | None]:
    """解析可选的“剧名 第N集”；普通剧名原样返回。"""
    match = _EPISODE_REQUEST_RE.fullmatch((query or "").strip())
    if not match:
        return (query or "").strip(), None
    return match.group("title").rstrip(), int(match.group("number"))


def parse_variant_request(query: str) -> tuple[str, int | None] | None:
    """解析“剧名 版本”或“剧名 版本2”的显式版本选择命令。"""
    match = _VARIANT_REQUEST_RE.fullmatch((query or "").strip())
    if not match:
        return None
    number = match.group("number")
    return match.group("title").rstrip(), int(number) if number else None


def parse_sports_collection_query(query: str) -> tuple[str, str | None] | None:
    """识别“斯诺克”或“CBA2023”这类体育集合查询。"""
    match = _SPORTS_COLLECTION_RE.fullmatch((query or "").strip())
    if not match:
        return None
    return match.group("league").upper(), match.group("year") or None


def is_sports_category(category: str) -> bool:
    """判断资源站分类是否属于已支持的体育赛事分类。"""
    normalized_category = normalize_title(category)
    if not normalized_category:
        return False
    aliases = {
        normalize_title(alias)
        for values in _SPORTS_CATEGORY_ALIASES.values()
        for alias in values
        if alias
    }
    return any(alias in normalized_category for alias in aliases)


def is_exact_sports_replay_result(query: str, result: SearchResult) -> bool:
    """仅接受标题完全一致且分类属于体育的播放结果。"""
    normalized_query = normalize_title(query)
    return bool(
        normalized_query
        and normalize_title(result.title) == normalized_query
        and is_sports_category(result.category)
    )


def is_sports_replay_item(
    keyword: str,
    year: str | None,
    item: dict[str, Any],
) -> bool:
    """同时校验标题关键词、可选年份及体育分类，排除同名影视。"""
    title = normalize_title(str(item.get("vod_name") or ""))
    normalized_keyword = normalize_title(keyword)
    if not normalized_keyword or normalized_keyword not in title:
        return False
    if year and year not in title:
        return False
    category = normalize_title(
        str(item.get("type_name") or item.get("vod_class") or "")
    )
    aliases = _SPORTS_CATEGORY_ALIASES.get(
        normalized_keyword,
        (normalized_keyword,),
    )
    return any(alias in category for alias in aliases)


def select_episode(result: SearchResult, requested: int | None) -> Episode | None:
    """按单集编号或“第1-20集”分段范围选择播放线路。"""
    if not result.episodes:
        return None
    if requested is None:
        return result.episodes[0]

    for episode in result.episodes:
        match = _EPISODE_RANGE_RE.search(episode.name)
        if not match:
            continue
        start = int(match.group("start"))
        end = int(match.group("end") or start)
        if start <= requested <= end:
            return episode

    if 1 <= requested <= len(result.episodes):
        return result.episodes[requested - 1]
    return None


def normalize_title(value: str) -> str:
    """生成仅用于匹配的标题，不改变最终展示标题。"""
    text = unicodedata.normalize("NFKC", value or "").strip().lower()
    text = _BRACKET_NOISE_RE.sub("", text)
    while True:
        cleaned = _NOISE_RE.sub("", text).strip()
        if cleaned == text:
            break
        text = cleaned
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", text)


def title_score(query: str, candidate: str) -> int:
    """标题越接近输入分数越高；短标题的泛匹配会受到明显惩罚。"""
    wanted = normalize_title(query)
    actual = normalize_title(candidate)
    if not wanted or not actual:
        return 0
    if wanted == actual:
        return 10_000
    if actual.startswith(wanted):
        return 7_000 - max(0, len(actual) - len(wanted)) * 20
    if wanted.startswith(actual):
        return 5_500 - max(0, len(wanted) - len(actual)) * 30
    if wanted in actual:
        return 4_000 - max(0, len(actual) - len(wanted)) * 25
    if actual in wanted:
        return 3_000 - max(0, len(wanted) - len(actual)) * 35

    # 容忍一个错字，例如“全球杀机”与“全球杀戮：……”。只比较等长窗口，
    # 避免长标题的公共字过多导致误判。
    if len(wanted) >= 3 and len(actual) >= len(wanted):
        window_size = len(wanted)
        best_ratio = max(
            SequenceMatcher(
                None,
                wanted,
                actual[index : index + window_size],
                autojunk=False,
            ).ratio()
            for index in range(len(actual) - window_size + 1)
        )
        if best_ratio >= 0.75:
            return int(3_000 + best_ratio * 4_000)

    overlap = len(set(wanted) & set(actual))
    ratio = overlap / max(len(set(wanted)), 1)
    return int(ratio * 1_000) if ratio >= 0.7 else 0


def known_title_hints(query: str) -> tuple[str, ...]:
    return _KNOWN_TITLE_HINTS.get(normalize_title(query), ())


def fuzzy_search_terms(query: str) -> tuple[str, ...]:
    """生成安全的二次检索词；短词不盲目拆成单字。"""
    hints = known_title_hints(query)
    if hints:
        return hints

    text = (query or "").strip()
    if len(text) < 4:
        return ()
    terms: list[str] = []
    for value in (text[:-1].strip(), text[1:].strip()):
        if len(value) >= 2 and value != text and value not in terms:
            terms.append(value)
    return tuple(terms)


def candidate_pool(
    candidates: Iterable[SearchResult],
    *,
    min_fuzzy_score: int = DEFAULT_MIN_FUZZY_SCORE,
) -> tuple[list[SearchResult], bool]:
    """精准候选存在时隔离模糊结果；否则启用达到阈值的模糊候选。"""
    available = list(candidates)
    exact = [item for item in available if item.score >= EXACT_TITLE_SCORE]
    if exact:
        return exact, True
    fuzzy = [item for item in available if item.score >= min_fuzzy_score]
    return fuzzy, False


def playlist_duration_seconds(content: str) -> float:
    """累计媒体播放清单中的 EXTINF 时长。"""
    return sum(
        float(value)
        for value in re.findall(r"(?m)^#EXTINF:([0-9]+(?:\.[0-9]+)?)", content or "")
    )


def master_playlist_variant(content: str) -> str | None:
    """读取 master playlist 的第一条媒体变体相对地址。"""
    waiting_for_uri = False
    for raw_line in (content or "").splitlines():
        line = raw_line.strip()
        if line.startswith("#EXT-X-STREAM-INF:"):
            waiting_for_uri = True
            continue
        if waiting_for_uri and line and not line.startswith("#"):
            return line
    return None


def _decode_json(raw: bytes) -> dict[str, Any]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            data = json.loads(raw.decode(encoding))
            return data if isinstance(data, dict) else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            last_error = exc
    if last_error:
        raise last_error
    return {}


def parse_episodes(play_url: Any) -> tuple[Episode, ...]:
    """从苹果 CMS 的 vod_play_url 中选择集数最多的一条 HLS 线路。"""
    if not isinstance(play_url, str) or not play_url.strip():
        return ()

    best: list[Episode] = []
    for route in play_url.split("$$$"):
        episodes: list[Episode] = []
        seen: set[str] = set()
        for index, item in enumerate(route.split("#"), start=1):
            item = item.strip()
            if not item:
                continue
            if "$" in item:
                name, url = item.split("$", 1)
            else:
                name, url = f"第{index}集", item
            url = url.strip()
            if not re.match(r"^https?://", url, flags=re.IGNORECASE):
                continue
            if ".m3u8" not in url.lower() or url in seen:
                continue
            seen.add(url)
            episodes.append(Episode(name=name.strip() or f"第{index}集", url=url))
        if len(episodes) > len(best):
            best = episodes
    return tuple(best)


def _result_from_item(
    item: dict[str, Any],
    source_name: str,
    score: int,
    *,
    match_role: str = "",
) -> SearchResult | None:
    title = str(item.get("vod_name") or "").strip()
    episodes = parse_episodes(item.get("vod_play_url"))
    if not title or not episodes:
        return None
    return SearchResult(
        title=title,
        source=source_name,
        episodes=episodes,
        cover_url=str(item.get("vod_pic") or "").strip(),
        year=str(item.get("vod_year") or "").strip(),
        actor=str(item.get("vod_actor") or "").strip(),
        director=str(item.get("vod_director") or "").strip(),
        category=str(
            item.get("type_name") or item.get("vod_class") or ""
        ).strip(),
        remarks=str(item.get("vod_remarks") or "").strip(),
        score=score,
        match_role=match_role,
    )


def matching_items(
    query: str,
    items: Iterable[Any],
    source_name: str,
    media_type: str | None = None,
) -> list[SearchResult]:
    """保留标题及指定媒体分类均匹配的可播放候选。"""
    candidates: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = str(
            item.get("type_name") or item.get("vod_class") or ""
        ).strip()
        if media_type and not is_media_category(media_type, category):
            continue
        title = str(item.get("vod_name") or "").strip()
        score = title_score(query, title)
        if score <= 0:
            continue
        result = _result_from_item(item, source_name, score)
        if result is not None:
            candidates.append(result)
    return candidates


def _person_field_matches(query: str, field_value: Any) -> bool:
    wanted = normalize_title(query)
    if not wanted:
        return False
    parts = [
        normalize_title(part)
        for part in re.split(r"[,，、/\s]+", str(field_value or ""))
        if normalize_title(part)
    ]
    return wanted in parts or any(wanted in part for part in parts)


def _year_sort_value(value: str) -> int:
    match = re.search(r"(19|20)\d{2}", value or "")
    return int(match.group(0)) if match else 0


def _person_result_sort_key(result: SearchResult) -> tuple[int, int, int, int, str]:
    role_rank = 2 if result.match_role == "actor" else 1
    metadata_rank = sum(
        1
        for value in (
            result.cover_url,
            result.year,
            result.category,
            result.actor,
            result.director,
        )
        if value
    )
    return (
        role_rank,
        _year_sort_value(result.year),
        len(result.episodes),
        metadata_rank,
        result.title,
    )


def person_matching_items(
    query: str,
    items: Iterable[Any],
    source_name: str,
    media_type: str | None = None,
    limit: int = 10,
) -> list[SearchResult]:
    """保留演员或导演命中且可播放的候选，并按人名搜索优先级排序。"""
    candidates: list[SearchResult] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        category = str(
            item.get("type_name") or item.get("vod_class") or ""
        ).strip()
        if media_type and not is_media_category(media_type, category):
            continue
        role = ""
        score = 0
        if _person_field_matches(query, item.get("vod_actor")):
            role = "actor"
            score = _PERSON_ACTOR_SCORE
        elif _person_field_matches(query, item.get("vod_director")):
            role = "director"
            score = _PERSON_DIRECTOR_SCORE
        if not role:
            continue
        result = _result_from_item(
            item,
            source_name,
            score,
            match_role=role,
        )
        if result is not None:
            candidates.append(result)

    unique: dict[tuple[str, str, str, str], SearchResult] = {}
    for result in sorted(candidates, key=_person_result_sort_key, reverse=True):
        unique.setdefault(variant_identity(result), result)
        if len(unique) >= max(1, int(limit)):
            break
    return list(unique.values())


def variant_identity(result: SearchResult) -> tuple[str, str, str, str]:
    """生成跨资源站版本指纹，忽略资源站名称和播放地址。"""
    title_key = _VERSION_TITLE_SUFFIX_RE.sub("", normalize_title(result.title))
    year_key = normalize_title(result.year)
    if year_key and title_key.endswith(year_key):
        title_key = title_key[: -len(year_key)]
    category = normalize_title(result.category)
    if re.search(r"(?:动漫|动画|漫剧)", category):
        media_kind = "动画"
    elif "电影" in category:
        media_kind = "电影"
    elif "剧" in category:
        media_kind = "电视剧"
    else:
        media_kind = category
    actors = [
        normalize_title(actor)
        for actor in re.split(r"[,，、/\s]+", result.actor)
        if normalize_title(actor)
    ]
    lead_actor = actors[0] if actors else ""
    return (
        title_key,
        year_key,
        media_kind,
        lead_actor,
    )


def is_selectable_variant(result: SearchResult) -> bool:
    """过滤解说、预告等并非正片版本的搜索结果。"""
    metadata = f"{result.title} {result.category} {result.remarks}"
    return not _NON_VARIANT_RE.search(metadata)


def best_item(
    query: str,
    items: Iterable[Any],
    source_name: str,
    media_type: str | None = None,
) -> SearchResult | None:
    candidates = matching_items(query, items, source_name, media_type)
    if not candidates:
        return None
    return max(candidates, key=lambda result: (result.score, len(result.episodes)))


class ShortDramaSearcher:
    def __init__(
        self,
        *,
        sources: tuple[Source, ...] = DEFAULT_SOURCES,
        timeout_seconds: float = 10,
        max_concurrent: int = 4,
        cache_ttl_seconds: int = 6 * 3600,
        full_version_min_minutes: float = 30,
        min_fuzzy_score: int = DEFAULT_MIN_FUZZY_SCORE,
    ) -> None:
        self.sources = sources
        self.timeout_seconds = max(2.0, float(timeout_seconds))
        self.max_concurrent = max(1, min(int(max_concurrent), 8))
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.full_version_min_seconds = max(
            5 * 60,
            float(full_version_min_minutes) * 60,
        )
        self.min_fuzzy_score = max(1, min(int(min_fuzzy_score), 9_999))
        self._cache: dict[str, tuple[float, SearchResult | None]] = {}
        self._variant_cache: dict[
            str,
            tuple[float, tuple[SearchResult, ...]],
        ] = {}
        self._collection_cache: dict[
            tuple[str, int],
            tuple[float, CollectionPage],
        ] = {}
        self._collection_source_pages: dict[tuple[str, str], int] = {}
        self._sports_exact_cache: dict[
            str,
            tuple[float, SearchResult | None],
        ] = {}
        self._recommendation_cache: dict[
            str,
            tuple[float, tuple[SearchResult, ...]],
        ] = {}
        self._people_cache: dict[
            str,
            tuple[float, tuple[SearchResult, ...]],
        ] = {}

    async def search_sports_replay(self, query: str) -> SearchResult | None:
        """只返回标题完全一致且分类为体育赛事的可播放结果。"""
        normalized_query = normalize_title(query)
        if not normalized_query:
            return None
        cached = self._sports_exact_cache.get(normalized_query)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        semaphore = asyncio.Semaphore(self.max_concurrent)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as session:
            source_results = await asyncio.gather(
                *(
                    self._search_source_variants(
                        session,
                        semaphore,
                        source,
                        query,
                    )
                    for source in self.sources
                )
            )
            exact_results = [
                item
                for source_result in source_results
                for item in source_result
                if is_exact_sports_replay_result(query, item)
            ]
            best = await self._choose_best_result(
                session,
                semaphore,
                exact_results,
                prefer_full=False,
            )

        now = time.monotonic()
        self._sports_exact_cache = {
            key: value
            for key, value in self._sports_exact_cache.items()
            if value[0] > now
        }
        self._sports_exact_cache[normalized_query] = (now + 10 * 60, best)
        logger.info(
            "[SHORT_DRAMA] sports exact query=%r candidates=%s selected=%s",
            query,
            len(exact_results),
            best.title if best else "none",
        )
        return best

    async def search_collection_page(
        self,
        query: str,
        page: int = 1,
    ) -> CollectionPage:
        """按需读取赛事回放的一页；只保留同时命中联赛与年份的正片。"""
        parsed = parse_sports_collection_query(query)
        page_number = max(1, int(page))
        if parsed is None:
            return CollectionPage(query=query, page=page_number, items=(), has_more=False)
        league, year = parsed
        cache_query = f"{league}{year or ''}"
        cache_key = (cache_query, page_number)
        cached = self._collection_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        if page_number == 1:
            sources = self.sources
        else:
            known = [
                source
                for source in self.sources
                if self._collection_source_pages.get(
                    (cache_query, source.name),
                    page_number,
                )
                >= page_number
            ]
            sources = tuple(known)
        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        semaphore = asyncio.Semaphore(self.max_concurrent)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as session:
            source_pages = await asyncio.gather(
                *(
                    self._collection_source_page(
                        session,
                        semaphore,
                        source,
                        league,
                        year,
                        page_number,
                    )
                    for source in sources
                )
            )

        candidates: list[SearchResult] = []
        has_more = False
        for source_name, items, page_count in source_pages:
            self._collection_source_pages[(cache_query, source_name)] = page_count
            candidates.extend(items)
            has_more = has_more or page_number < page_count
        unique: dict[str, SearchResult] = {}
        for item in sorted(
            candidates,
            key=lambda result: (_sports_replay_sort_key(result.title), result.title),
            reverse=True,
        ):
            unique.setdefault(normalize_title(item.title), item)
        result = CollectionPage(
            query=cache_query,
            page=page_number,
            items=tuple(unique.values()),
            has_more=has_more,
        )
        now = time.monotonic()
        self._collection_cache = {
            key: value
            for key, value in self._collection_cache.items()
            if value[0] > now
        }
        self._collection_cache[cache_key] = (now + 10 * 60, result)
        logger.info(
            "[SHORT_DRAMA] collection query=%s page=%s items=%s more=%s",
            cache_query,
            page_number,
            len(result.items),
            result.has_more,
        )
        return result

    async def search_variants(
        self,
        query: str,
        limit: int = 6,
        *,
        media_type: str | None = None,
    ) -> tuple[SearchResult, ...]:
        """返回可供用户辨认和选择的同名/近名版本。"""
        wanted = max(1, min(int(limit), 10))
        cache_key = f"{media_type or '*'}:{normalize_title(query)}"
        cached = self._variant_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1][:wanted]

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        semaphore = asyncio.Semaphore(self.max_concurrent)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as session:
            source_results = await asyncio.gather(
                *(
                    self._search_source_variants(
                        session,
                        semaphore,
                        source,
                        query,
                        media_type=media_type,
                    )
                    for source in self.sources
                )
            )
            candidates = [item for result in source_results for item in result]
            variants = self._rank_variants(candidates, wanted)
            if not variants:
                terms = fuzzy_search_terms(query)
                if terms:
                    secondary_results = await asyncio.gather(
                        *(
                            self._search_source_variants(
                                session,
                                semaphore,
                                source,
                                term,
                                match_query=query,
                                media_type=media_type,
                            )
                            for term in terms
                            for source in self.sources
                        )
                    )
                    candidates = [
                        item
                        for result in secondary_results
                        for item in result
                    ]
                    variants = self._rank_variants(candidates, wanted)

        self._remember_variants(cache_key, variants)
        logger.info(
            "[SHORT_DRAMA] variants query=%r selected=%s candidates=%s",
            query,
            len(variants),
            len(candidates),
        )
        return variants

    def cached_variants(
        self,
        query: str,
        limit: int = 6,
        *,
        media_type: str | None = None,
    ) -> tuple[SearchResult, ...]:
        """读取普通搜索顺带生成的版本候选，不发起网络请求。"""
        cache_key = f"{media_type or '*'}:{normalize_title(query)}"
        cached = self._variant_cache.get(cache_key)
        if not cached or cached[0] <= time.monotonic():
            return ()
        return cached[1][: max(1, min(int(limit), 10))]

    def _rank_variants(
        self,
        candidates: Iterable[SearchResult],
        wanted: int,
    ) -> tuple[SearchResult, ...]:
        available = [
            item
            for item in candidates
            if item.score >= self.min_fuzzy_score
            and is_selectable_variant(item)
        ]
        ordered = sorted(
            available,
            key=lambda item: (
                item.score,
                bool(item.year),
                bool(item.category),
                bool(item.actor),
                len(item.episodes),
            ),
            reverse=True,
        )
        unique: dict[tuple[str, str, str, str], SearchResult] = {}
        for item in ordered:
            key = variant_identity(item)
            unique.setdefault(key, item)
            if len(unique) >= wanted:
                break
        return tuple(unique.values())

    def _remember_variants(
        self,
        cache_key: str,
        variants: tuple[SearchResult, ...],
    ) -> None:
        now = time.monotonic()
        self._variant_cache = {
            key: value
            for key, value in self._variant_cache.items()
            if value[0] > now
        }
        self._variant_cache[cache_key] = (now + 10 * 60, variants)

    async def recommend_results(
        self,
        limit: int = 12,
        *,
        media_type: str | None = "短剧",
    ) -> tuple[SearchResult, ...]:
        """返回资源站最近更新且可直接播放的指定分类结果。"""
        wanted = max(1, min(int(limit), 50))
        cache_key = media_type or "*"
        cached = self._recommendation_cache.get(cache_key)
        if cached and cached[0] > time.monotonic() and len(cached[1]) >= wanted:
            return cached[1][:wanted]

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        semaphore = asyncio.Semaphore(self.max_concurrent)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        candidates: list[Recommendation] = []
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as session:
            for page in range(1, 11):
                page_results = await asyncio.gather(
                    *(
                        self._recommendation_source_page(
                            session,
                            semaphore,
                            source,
                            page,
                            media_type,
                        )
                        for source in self.sources
                    )
                )
                candidates.extend(
                    recommendation
                    for result in page_results
                    for recommendation in result
                )
                unique_count = len(
                    {normalize_title(item.title) for item in candidates}
                )
                if unique_count >= wanted:
                    break

            unique: dict[str, Recommendation] = {}
            for candidate in candidates:
                key = normalize_title(candidate.title)
                if not key:
                    continue
                current = unique.get(key)
                if current is None or candidate.sort_key > current.sort_key:
                    unique[key] = candidate
            ordered_candidates = sorted(
                unique.values(),
                key=lambda item: (item.sort_key, item.title),
                reverse=True,
            )

            # Probe bounded batches so stale newest records do not consume all
            # recommendation slots.  Continue to older pages only within the
            # existing ten-page collection; no source URL is rewritten here.
            available_results: list[SearchResult] = []
            batch_size = max(wanted * 4, wanted)
            for start in range(0, len(ordered_candidates), batch_size):
                batch = ordered_candidates[start : start + batch_size]
                available_results.extend(
                    await self._filter_available_results(
                        session,
                        semaphore,
                        (item.result for item in batch),
                    )
                )
                if len(available_results) >= wanted:
                    break
            ordered = tuple(available_results[:wanted])
        now = time.monotonic()
        self._recommendation_cache = {
            key: value
            for key, value in self._recommendation_cache.items()
            if value[0] > now
        }
        self._recommendation_cache[cache_key] = (now + 10 * 60, ordered)
        logger.info(
            "[SHORT_DRAMA] recommendations media=%s selected=%s candidates=%s",
            media_type or "all",
            len(ordered),
            len(candidates),
        )
        return ordered

    async def recommend_titles(self, limit: int = 20) -> tuple[str, ...]:
        """兼容旧调用：只返回最新可播放短剧的剧名。"""
        results = await self.recommend_results(limit)
        return tuple(result.title for result in results)

    async def search_people(
        self,
        query: str,
        limit: int = 10,
        *,
        media_type: str | None = None,
    ) -> tuple[SearchResult, ...]:
        """按演员/导演返回相关作品列表，用于片名搜索无结果后的兜底。"""
        wanted = max(1, min(int(limit), 20))
        normalized_query = normalize_title(query)
        if not normalized_query:
            return ()
        cache_key = f"{media_type or '*'}:{normalized_query}:{wanted}"
        cached = self._people_cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1][:wanted]

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        semaphore = asyncio.Semaphore(self.max_concurrent)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as session:
            source_results = await asyncio.gather(
                *(
                    self._search_source_people(
                        session,
                        semaphore,
                        source,
                        query,
                        media_type=media_type,
                        limit=wanted,
                    )
                    for source in self.sources
                )
            )

        unique: dict[tuple[str, str, str, str], SearchResult] = {}
        for result in sorted(
            (item for source_result in source_results for item in source_result),
            key=_person_result_sort_key,
            reverse=True,
        ):
            unique.setdefault(variant_identity(result), result)
            if len(unique) >= wanted:
                break
        results = tuple(unique.values())
        now = time.monotonic()
        self._people_cache = {
            key: value
            for key, value in self._people_cache.items()
            if value[0] > now
        }
        self._people_cache[cache_key] = (now + 10 * 60, results)
        logger.info(
            "[SHORT_DRAMA] people query=%r media=%s selected=%s",
            query,
            media_type or "all",
            len(results),
        )
        return results

    async def search(
        self,
        query: str,
        *,
        media_type: str | None = None,
        prefer_full: bool = True,
    ) -> SearchResult | None:
        cache_key = (
            f"{media_type or '*'}:{normalize_title(query)}:"
            f"{'full' if prefer_full else 'episodes'}"
        )
        cached = self._cache.get(cache_key)
        if cached and cached[0] > time.monotonic():
            return cached[1]

        timeout = aiohttp.ClientTimeout(
            total=self.timeout_seconds,
            connect=min(5.0, self.timeout_seconds),
        )
        semaphore = asyncio.Semaphore(self.max_concurrent)
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/137.0 Safari/537.36"
            ),
            "Accept": "application/json,text/plain,*/*",
        }
        async with aiohttp.ClientSession(
            timeout=timeout,
            headers=headers,
            trust_env=False,
        ) as session:
            source_results = await asyncio.gather(
                *(
                    self._search_source_variants(
                        session,
                        semaphore,
                        source,
                        query,
                        media_type=media_type,
                    )
                    for source in self.sources
                )
            )
            available = [item for result in source_results for item in result]
            variant_candidates = available
            pool, exact_match = candidate_pool(
                available,
                min_fuzzy_score=self.min_fuzzy_score,
            )
            if not exact_match:
                secondary_terms = fuzzy_search_terms(query)
                if secondary_terms:
                    secondary_results = await asyncio.gather(
                        *(
                            self._search_source_variants(
                                session,
                                semaphore,
                                source,
                                term,
                                match_query=query,
                                media_type=media_type,
                            )
                            for term in secondary_terms
                            for source in self.sources
                        )
                    )
                    combined = self._dedupe_results(
                        available
                        + [
                            item
                            for result in secondary_results
                            for item in result
                        ]
                    )
                    variant_candidates = combined
                    pool, exact_match = candidate_pool(
                        combined,
                        min_fuzzy_score=self.min_fuzzy_score,
                    )
            self._remember_variants(
                f"{media_type or '*'}:{normalize_title(query)}",
                # 普通搜索使用独立中间列表页，保留更多候选供用户按相关性选择。
                self._rank_variants(variant_candidates, 8),
            )
            best = await self._choose_best_result(
                session,
                semaphore,
                pool,
                prefer_full=prefer_full,
            )

        if best:
            logger.info(
                "[SHORT_DRAMA] selected title=%r source=%s match=%s episodes=%s "
                "duration=%.1fmin full=%s",
                best.title,
                best.source,
                "exact" if exact_match else "fuzzy",
                len(best.episodes),
                best.duration_seconds / 60,
                best.is_full_version,
            )
        now = time.monotonic()
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if value[0] > now
        }
        if len(self._cache) >= 100:
            oldest_key = min(self._cache, key=lambda key: self._cache[key][0])
            self._cache.pop(oldest_key, None)
        self._cache[cache_key] = (
            now + self.cache_ttl_seconds,
            best,
        )
        return best

    @staticmethod
    def _dedupe_results(results: Iterable[SearchResult]) -> list[SearchResult]:
        unique: dict[tuple[str, str, str], SearchResult] = {}
        for result in results:
            key = (
                result.source,
                normalize_title(result.title),
                result.episodes[0].url,
            )
            current = unique.get(key)
            if current is None or result.score > current.score:
                unique[key] = result
        return list(unique.values())

    async def _choose_best_result(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        pool: list[SearchResult],
        *,
        prefer_full: bool,
    ) -> SearchResult | None:
        if not pool:
            return None

        # Do not cache a card whose first playlist already returns 404/HTML.
        # This is especially important for newly published recommendation
        # entries: some providers expose the VOD record before the CDN object
        # is ready (or leave the record behind after deleting the object).
        pool = await self._filter_available_results(session, semaphore, pool)
        if not pool:
            return None

        if not prefer_full:
            # 普通搜索优先选择可切换的分集线路。标题匹配分数只用于
            # 在集数相同的候选之间决胜，避免单线路“完整版”因分数略高
            # 抢走真正的多集资源。
            return max(
                pool,
                key=lambda result: (
                    len(result.episodes),
                    result.score,
                    result.duration_seconds,
                ),
            )

        single_streams = [result for result in pool if len(result.episodes) == 1]
        probed = await asyncio.gather(
            *(
                self._probe_result_duration(session, semaphore, result)
                for result in single_streams
            )
        )
        probed_by_key = {
            (result.source, result.episodes[0].url): result for result in probed
        }
        measured_pool = [
            probed_by_key.get(
                (result.source, result.episodes[0].url),
                result,
            )
            for result in pool
        ]
        full_versions = [
            result
            for result in measured_pool
            if len(result.episodes) == 1
            and result.duration_seconds >= self.full_version_min_seconds
        ]
        if full_versions:
            longest = max(
                full_versions,
                key=lambda result: (result.duration_seconds, result.score),
            )
            return replace(longest, is_full_version=True)

        # 没有满足时长阈值的完整版才回退分集；同分时优先集数较完整的源。
        return max(
            measured_pool,
            key=lambda result: (
                result.score,
                len(result.episodes),
                result.duration_seconds,
            ),
        )

    async def _probe_result_duration(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        result: SearchResult,
    ) -> SearchResult:
        seconds = await self._hls_duration(
            session,
            semaphore,
            result.episodes[0].url,
        )
        return replace(result, duration_seconds=seconds)

    async def _probe_hls_available(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        url: str,
    ) -> bool:
        """Check that an advertised HLS URL still returns a playlist."""
        if not self._is_public_http_url(url):
            return False
        try:
            async with semaphore:
                async with session.get(
                    url,
                    allow_redirects=True,
                    ssl=False,
                ) as response:
                    if response.status not in {200, 206}:
                        return False
                    prefix = await response.content.read(4096)
            return b"#EXTM3U" in prefix.upper()
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError):
            return False

    async def _filter_available_results(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        results: Iterable[SearchResult],
    ) -> list[SearchResult]:
        candidates = list(results)
        if not candidates:
            return []
        checks = await asyncio.gather(
            *(
                self._probe_hls_available(
                    session,
                    semaphore,
                    result.episodes[0].url,
                )
                for result in candidates
            )
        )
        available = [
            result
            for result, is_available in zip(candidates, checks)
            if is_available
        ]
        if len(available) != len(candidates):
            logger.info(
                "[SHORT_DRAMA] filtered unavailable HLS results=%s kept=%s",
                len(candidates) - len(available),
                len(available),
            )
        return available

    async def _hls_duration(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        url: str,
        *,
        depth: int = 0,
    ) -> float:
        if depth > 2 or not self._is_public_http_url(url):
            return 0.0
        try:
            async with semaphore:
                async with session.get(url, allow_redirects=True) as response:
                    if response.status not in {200, 206}:
                        return 0.0
                    raw = await response.read()
                    if len(raw) > 5_000_000:
                        return 0.0
            content = raw.decode("utf-8-sig", errors="replace")
            seconds = playlist_duration_seconds(content)
            if seconds > 0:
                return seconds
            variant = master_playlist_variant(content)
            if not variant:
                return 0.0
            return await self._hls_duration(
                session,
                semaphore,
                urljoin(url, variant),
                depth=depth + 1,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
            return 0.0

    @staticmethod
    def _is_public_http_url(url: str) -> bool:
        try:
            parsed = urlsplit(url)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                return False
            if parsed.hostname.lower() in {"localhost", "localhost.localdomain"}:
                return False
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError:
                return True
            return not (
                address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_reserved
                or address.is_multicast
            )
        except ValueError:
            return False

    async def _search_source_variants(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        source: Source,
        query: str,
        *,
        match_query: str | None = None,
        media_type: str | None = None,
    ) -> list[SearchResult]:
        try:
            async with semaphore:
                async with session.get(
                    source.url,
                    params={"ac": "detail", "wd": query},
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        return []
                    raw = await response.read()
                    if len(raw) > 5_000_000:
                        return []
            payload = _decode_json(raw)
            return matching_items(
                match_query or query,
                payload.get("list") or (),
                source.name,
                media_type,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            logger.info(
                "[SHORT_DRAMA] variant source=%s unavailable: %s: %s",
                source.name,
                type(exc).__name__,
                exc,
            )
            return []

    async def _search_source_people(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        source: Source,
        query: str,
        *,
        media_type: str | None = None,
        limit: int = 10,
    ) -> list[SearchResult]:
        try:
            async with semaphore:
                async with session.get(
                    source.url,
                    params={"ac": "detail", "wd": query},
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        return []
                    raw = await response.read()
                    if len(raw) > 5_000_000:
                        return []
            payload = _decode_json(raw)
            return person_matching_items(
                query,
                payload.get("list") or (),
                source.name,
                media_type,
                limit,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            logger.info(
                "[SHORT_DRAMA] people source=%s unavailable: %s: %s",
                source.name,
                type(exc).__name__,
                exc,
            )
            return []

    async def _collection_source_page(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        source: Source,
        league: str,
        year: str | None,
        page: int,
    ) -> tuple[str, list[SearchResult], int]:
        try:
            async with semaphore:
                async with session.get(
                    source.url,
                    params={
                        "ac": "detail",
                        "wd": f"{league} {year}" if year else league,
                        "pg": page,
                    },
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        return source.name, [], 0
                    raw = await response.read()
                    if len(raw) > 5_000_000:
                        return source.name, [], 0
            payload = _decode_json(raw)
            try:
                page_count = max(0, int(payload.get("pagecount") or 0))
            except (TypeError, ValueError):
                page_count = 0
            items: list[SearchResult] = []
            for raw_item in payload.get("list") or ():
                if not isinstance(raw_item, dict):
                    continue
                if not is_sports_replay_item(league, year, raw_item):
                    continue
                result = _result_from_item(raw_item, source.name, EXACT_TITLE_SCORE)
                if result is not None and is_selectable_variant(result):
                    items.append(result)
            return source.name, items, page_count
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            logger.info(
                "[SHORT_DRAMA] collection source=%s page=%s unavailable: %s: %s",
                source.name,
                page,
                type(exc).__name__,
                exc,
            )
            return source.name, [], 0

    async def _recommendation_source_page(
        self,
        session: aiohttp.ClientSession,
        semaphore: asyncio.Semaphore,
        source: Source,
        page: int,
        media_type: str | None,
    ) -> list[Recommendation]:
        try:
            async with semaphore:
                async with session.get(
                    source.url,
                    params={"ac": "detail", "pg": page},
                    allow_redirects=True,
                ) as response:
                    if response.status != 200:
                        logger.info(
                            "[SHORT_DRAMA] recommendation source=%s page=%s status=%s",
                            source.name,
                            page,
                            response.status,
                        )
                        return []
                    raw = await response.read()
                    if len(raw) > 5_000_000:
                        return []
            return recommendation_candidates(
                _decode_json(raw),
                source.name,
                media_type,
            )
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError, TypeError) as exc:
            logger.info(
                "[SHORT_DRAMA] recommendation source=%s unavailable: %s: %s",
                source.name,
                type(exc).__name__,
                exc,
            )
            return []
