"""
公共工具函数 — 消除各模块间的重复代码
"""
import hashlib
import re
from datetime import datetime
from pathlib import Path

import httpx


def clean_at(text: str) -> str:
    """去掉消息开头的 @xxx 前缀"""
    text = text.strip()
    if text.startswith("@"):
        space = text.find(" ")
        if space < 0:
            space = text.find(" ")
        if space > 0:
            return text[space + 1:].strip()
        return ""
    return text


IMAGE_KEYWORDS = [
    "生成一张图片生图", "生成一张图片", "生成一个图片", "生成一幅图片",
    "生成图片", "生成照片", "生成图像", "生成插画", "生成壁纸", "生成头像",
    "生图",
    "画一张", "画一个", "画一幅", "画个",
    "生成一张", "生成一个", "生成",
    "制作图片", "制作一张", "制作",
    "画",
]

VIDEO_KEYWORDS = [
    "生成视频", "制作视频", "做个视频", "生成一个", "生成",
]


def extract_prompt(text: str, keywords: list[str], media_pattern: str) -> str:
    """从用户消息中提取生成用的 prompt。

    Args:
        text: 用户原始消息
        keywords: 按优先级排列的指令关键词
        media_pattern: 匹配媒体类型的正则片段，如 "图片|照片|图像" 或 "视频|短片"
    """
    text = clean_at(text)

    for kw in keywords:
        idx = text.find(kw)
        if idx >= 0:
            after = text[idx + len(kw):].strip()
            after = re.sub(r"^[\s：:，,]+", "", after)
            if after:
                return after
            break

    cleaned = re.sub(
        rf"^(帮我|请帮我|麻烦|请|帮我制作|生成|制作|画).*?(?:{media_pattern})[：:，,\s]*",
        "", text,
    ).strip()
    if cleaned and len(cleaned) >= 3:
        return cleaned

    return text.strip()


async def download_async(url: str, save_dir: Path, index: int = 0) -> str:
    """下载文件到本地，返回路径。文件名含时间戳和 URL hash。"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    url_hash = hashlib.md5(url.encode()).hexdigest()[:6]
    ext = ".png"
    if ".jpg" in url or ".jpeg" in url:
        ext = ".jpg"
    elif ".webp" in url:
        ext = ".webp"
    filename = f"{ts}_{url_hash}_{index}{ext}"
    filepath = save_dir / filename
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        resp = await client.get(url)
        if resp.status_code != 200:
            raise RuntimeError(f"下载失败: {resp.status_code}")
        filepath.write_bytes(resp.content)
    return str(filepath)
