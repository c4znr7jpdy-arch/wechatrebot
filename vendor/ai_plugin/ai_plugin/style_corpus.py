"""
抖音评论语料爬虫 — 爬取评论存入语料库，用于 RAG 动态风格注入
"""
import re
import sys
from pathlib import Path
from typing import Optional

from nonebot import logger

# 让 crawlers 包能正常导入
_douyin_dir = Path(__file__).parent / "douyin"
if str(_douyin_dir) not in sys.path:
    sys.path.insert(0, str(_douyin_dir))

from .douyin.config import inject_cookies


_crawler = None
_crawler_ready = False


def _ensure_crawler():
    global _crawler, _crawler_ready
    if not _crawler_ready:
        inject_cookies()
        from crawlers.douyin.web.web_crawler import DouyinWebCrawler
        _crawler = DouyinWebCrawler()
        _crawler_ready = True
    return _crawler


def _clean_comment(text: str) -> str:
    """清洗评论文本，去掉无意义内容"""
    if not text:
        return ""
    text = text.strip()
    # 去掉纯表情/纯符号/过短
    if len(text) < 4:
        return ""
    # 去掉纯数字
    if text.isdigit():
        return ""
    # 去掉纯 @ 某人
    if text.startswith("@") and " " not in text and len(text) < 20:
        return ""
    # 去掉纯链接
    if text.startswith("http"):
        return ""
    return text


def _classify_topic(text: str) -> str:
    """简单分类评论话题"""
    text_lower = text.lower()
    if any(k in text_lower for k in ["python", "代码", "编程", "bug", "算法", "java", "go语言", "前端", "后端"]):
        return "技术"
    if any(k in text_lower for k in ["游戏", "原神", "王者", "steam", "lol", "吃鸡", "崩铁", "绝区零"]):
        return "游戏"
    if any(k in text_lower for k in ["手机", "电脑", "显卡", "4090", "5090", "iphone", "小米", "华为"]):
        return "数码"
    if any(k in text_lower for k in ["ai", "gpt", "大模型", "深度学习", "chatgpt"]):
        return "AI"
    return "日常"


async def crawl_comments(aweme_id: str, count: int = 50) -> list[dict]:
    """
    爬取指定抖音视频的评论

    Args:
        aweme_id: 抖音视频ID（纯数字，不是URL）
        count: 爬取评论数量

    Returns:
        [{"content": "评论文本", "topic": "话题", "source": "douyin"}, ...]
    """
    crawler = _ensure_crawler()

    all_comments = []
    cursor = 0
    page_size = min(count, 20)

    while len(all_comments) < count:
        try:
            response = await crawler.fetch_video_comments(
                aweme_id=aweme_id,
                cursor=cursor,
                count=page_size,
            )
        except Exception as e:
            logger.warning(f"[STYLE_CORPUS] 爬取评论失败: {e}")
            break

        comments_data = response.get("comments", [])
        if not comments_data:
            break

        for item in comments_data:
            text = item.get("text", "")
            cleaned = _clean_comment(text)
            if cleaned:
                all_comments.append({
                    "content": cleaned,
                    "topic": _classify_topic(cleaned),
                    "source": "douyin",
                })

        has_more = response.get("has_more", 0)
        cursor = response.get("cursor", 0)
        if not has_more or not cursor:
            break

    logger.info(f"[STYLE_CORPUS] 爬取 {aweme_id} 完成，有效评论 {len(all_comments)}/{count}")
    return all_comments[:count]


async def crawl_from_url(url: str, count: int = 50) -> list[dict]:
    """
    从抖音URL爬取评论（自动提取aweme_id）

    Args:
        url: 抖音视频URL
        count: 爬取数量
    """
    crawler = _ensure_crawler()

    # 用 DouyinWebCrawler 的 AwemeIdFetcher 提取 aweme_id
    try:
        from crawlers.douyin.web.utils import AwemeIdFetcher
        aweme_id = await AwemeIdFetcher.get_aweme_id(url)
    except Exception as e:
        logger.warning(f"[STYLE_CORPUS] 提取 aweme_id 失败: {e}")
        return []

    if not aweme_id:
        logger.warning(f"[STYLE_CORPUS] 无法从 URL 提取 aweme_id: {url}")
        return []

    return await crawl_comments(aweme_id, count)


async def crawl_and_store(embedding_store, aweme_id: str, count: int = 50) -> int:
    """
    爬取评论并存入语料库

    Args:
        embedding_store: EmbeddingStore 实例
        aweme_id: 抖音视频ID
        count: 爬取数量

    Returns:
        存入的评论条数
    """
    comments = await crawl_comments(aweme_id, count)
    if not comments:
        return 0
    stored = await embedding_store.add_style_batch(comments)
    logger.info(f"[STYLE_CORPUS] 存入 {stored} 条语料，总计 {embedding_store.get_style_corpus_count()} 条")
    return stored


async def crawl_from_url_and_store(embedding_store, url: str, count: int = 50) -> int:
    """从URL爬取并存入语料库"""
    comments = await crawl_from_url(url, count)
    if not comments:
        return 0
    stored = await embedding_store.add_style_batch(comments)
    logger.info(f"[STYLE_CORPUS] 存入 {stored} 条语料，总计 {embedding_store.get_style_corpus_count()} 条")
    return stored
