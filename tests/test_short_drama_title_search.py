"""Title matching accepts close names without confusing unrelated works or sequels."""

import importlib.util
from pathlib import Path

import pytest


_HELPER = Path(__file__).parents[1] / "runtime/jiang_short_drama/title_search.py"
_SPEC = importlib.util.spec_from_file_location("short_drama_title_search_rules", _HELPER)
assert _SPEC is not None and _SPEC.loader is not None
title_search = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(title_search)


@pytest.mark.parametrize("title", ["写给阿嬷的情书", "写给阿嬤的情书", "写给阿嫲的情书"])
def test_matching_normalizes_limited_character_variants_and_quality_noise(title):
    assert title_search.normalize_title(f"《{title}》（高清国语）") == title_search.normalize_title("写给阿嬷的情书")
    assert title_search.title_score("写给阿嬷的情书", title) == title_search.EXACT_TITLE_SCORE


def test_exact_title_sorts_before_typo_substring_or_subtitle_matches():
    query = "写给阿嬷的情书"
    candidates = ["写给给阿嬷的情书", "写给阿嬷的情书之重逢", "给阿嬷的情书", query]
    ordered = sorted(candidates, key=lambda title: title_search.title_score(query, title), reverse=True)
    assert ordered[0] == query
    assert title_search.title_score(query, query) == title_search.EXACT_TITLE_SCORE
    assert all(title_search.title_score(query, title) < title_search.EXACT_TITLE_SCORE for title in candidates[:-1])


@pytest.mark.parametrize(("query", "candidate"), [
    ("给阿嬷的情书", "写给阿嬷的情书"),
    ("写给阿嬷的情书", "给阿嬷的情书"),
    ("写给给阿嬷的情书", "写给阿嬷的情书"),
    ("写给给阿嬷的情书", "给阿嬷的情书"),
    ("写给阿嫲的情书", "写给阿嬷的情书"),
    ("写给阿嫲的情书", "给阿嬷的情书"),
    ("写给阿妈的情书", "写给阿嬷的情书"),
    ("写给阿妈的情书", "给阿嬷的情书"),
    ("写给阿妈的晴书", "写给阿嬷的情书"),
    ("写给阿嬷的情", "写给阿嬷的情书"),
])
def test_long_titles_accept_bounded_insertions_deletions_and_typos(query, candidate):
    assert title_search.title_score(query, candidate) >= title_search.DEFAULT_MIN_FUZZY_SCORE


@pytest.mark.parametrize(("query", "candidate"), [
    ("写给给阿嬷的情书", "情书"),
    ("写给阿嬷的情书", "写给宇宙的歌声"),
    ("情书", "情画"),
    ("阿嬷", "阿妈"),
    ("山海情", "山河情"),
    ("", "写给阿嬷的情书"),
    ("写给阿嬷的情书", ""),
])
def test_fuzzy_matching_rejects_short_generic_or_distant_candidates(query, candidate):
    assert title_search.title_score(query, candidate) < title_search.DEFAULT_MIN_FUZZY_SCORE


@pytest.mark.parametrize(("query", "candidate"), [
    ("流浪地球2", "流浪地球3"),
    ("流浪地球2", "流浪地球"),
    ("长相思第1季", "长相思第2季"),
    ("长相思第1季", "长相思"),
    ("长相思第一季", "长相思第二季"),
    ("七仙女2005", "七仙女2015"),
])
def test_conflicting_sequel_and_year_numbers_never_match(query, candidate):
    assert title_search.title_score(query, candidate) == 0


@pytest.mark.parametrize("candidate_year", ["", "2015", "2006"])
def test_explicit_year_requires_matching_catalog_year_when_title_omits_it(candidate_year):
    assert title_search.title_score("七仙女2005", "七仙女", candidate_year=candidate_year) == 0


def test_explicit_year_accepts_matching_catalog_metadata_below_exact_name():
    score = title_search.title_score("七仙女2005", "七仙女", candidate_year="2005")
    assert title_search.DEFAULT_MIN_FUZZY_SCORE <= score < title_search.EXACT_TITLE_SCORE


def test_existing_typo_plus_long_subtitle_remains_a_useful_match():
    assert title_search.title_score("全球杀机", "全球杀戮：这游戏我能赢") >= title_search.DEFAULT_MIN_FUZZY_SCORE


@pytest.mark.parametrize("query", ["写给给阿嬷的情书", "写给阿嫲的情书", "一部很长很长的影视作品名字"])
def test_fallback_search_terms_are_unique_bounded_and_keep_useful_phrases(query):
    terms = title_search.fuzzy_search_terms(query)
    assert 0 < len(terms) <= 4
    assert len(terms) == len(set(terms))
    assert query not in terms
    assert all(len(title_search.normalize_title(term)) >= 3 for term in terms)


@pytest.mark.parametrize("query", ["写给给阿嬷的情书", "写给阿妈的情书"])
def test_prefix_or_internal_typo_fallback_can_recall_formal_title(query):
    terms = title_search.fuzzy_search_terms(query)
    assert any(term in "给阿嬷的情书" for term in terms)


@pytest.mark.parametrize("query", ["情书", "阿嬷", "山海情", "", "a"])
def test_short_queries_do_not_expand_to_arbitrary_single_characters(query):
    assert title_search.fuzzy_search_terms(query) == ()


def test_latin_fallback_keeps_complete_words_and_original_word_boundaries():
    query = "The Last of Us"
    terms = title_search.fuzzy_search_terms(query)
    assert len(terms) <= 4
    assert terms
    original_words = query.split()
    for term in terms:
        assert term != query
        assert all(word in original_words for word in term.split())
        assert " " in term


def test_existing_known_title_hint_remains_available():
    assert "全球杀戮" in title_search.known_title_hints("全球杀机")
    assert "全球杀戮" in title_search.fuzzy_search_terms("全球杀机")
    assert title_search.known_title_hints("没有登记的作品") == ()
