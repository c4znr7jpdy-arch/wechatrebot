"""
Cookie 注入 — 从环境变量读取，写入 crawlers 的 YAML 配置文件
"""
import os
import yaml
from pathlib import Path
from nonebot import logger

_CRAWLERS_DIR = Path(__file__).parent / "crawlers"


def inject_cookies():
    douyin_cookie = os.getenv("DOUYIN_COOKIE", "")
    tiktok_cookie = os.getenv("TIKTOK_COOKIE", "")

    if douyin_cookie:
        _patch_yaml(
            _CRAWLERS_DIR / "douyin" / "web" / "config.yaml",
            ["TokenManager", "douyin", "headers", "Cookie"],
            douyin_cookie,
        )
        logger.info("[DOUYIN] Cookie 已注入")

    if tiktok_cookie:
        _patch_yaml(
            _CRAWLERS_DIR / "tiktok" / "web" / "config.yaml",
            ["TokenManager", "tiktok", "headers", "Cookie"],
            tiktok_cookie,
        )
        logger.info("[DOUYIN] TikTok Cookie 已注入")

    bilibili_cookie = os.getenv("BILIBILI_COOKIE", "")
    if bilibili_cookie:
        _patch_yaml(
            _CRAWLERS_DIR / "bilibili" / "web" / "config.yaml",
            ["TokenManager", "bilibili", "headers", "cookie"],
            bilibili_cookie,
        )
        logger.info("[DOUYIN] Bilibili Cookie 已注入")


def _patch_yaml(path: Path, keys: list, value: str):
    """修改 YAML 文件中的嵌套 key"""
    if not path.exists():
        logger.warning(f"[DOUYIN] 配置文件不存在: {path}")
        return
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    # Navigate nested dict
    d = data
    for k in keys[:-1]:
        d = d.get(k, {})
    d[keys[-1]] = value
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(data, f, allow_unicode=True, default_flow_style=False)
