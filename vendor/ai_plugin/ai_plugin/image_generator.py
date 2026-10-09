"""
多后端图片生成模块 — Minimax / GPT-Image
API 流程: submit(同步) → download → 返回本地路径列表
"""
import asyncio
import base64
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from nonebot import logger, require

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from .utils import extract_prompt as _extract_prompt_base, download_async, IMAGE_KEYWORDS

IMAGE_DIR = Path(__file__).parent.parent / "data" / "images"
CLEANUP_HOURS = 2

IMAGE_INTENT_PATTERNS = [
    r"(?:帮我|给我|请|想要|来|做|生成)?(?<![图文])生图(?:$|[\s，,：:])",
    r"生成.*(?:图片|照片|图像|插画|壁纸|头像|[张个只].*图)",
    r"画.*(?:一张|一个|一幅|个)",
    r"帮我.*(?:画|生成|做|制作).*(?:图片|照片|图|图像|插画)",
    r"创作.*(?:图片|照片|图像|插画)",
    r"做.*(?:一张|一个).*(?:图片|照片|图|图像|插画)",
    r"(?:图片|照片|图像|插画).*生成",
    r"文生图",
    r"text.*to.*image",
    r"t2i",
    r"帮我画",
    r"画一[个张幅]",
    r"(?:生成|制作|画).*?头像",
    r"(?:生成|制作|画).*?壁纸",
    r"(?:生成|制作|画)(?:一个|一张|个|[張张][的])?(?:图片|照片|图像|插画)",
    r"(?:给我|来|发|找).{0,2}[张个].{0,10}(?:图片|照片|图像|图)",
    r"(?:给我|来|发|找).{0,6}(?:图片|照片|壁纸|头像)",
]

IMAGE_EDIT_PATTERNS = [
    r"P图",
    r"改图",
    r"修图",
    r"编辑.*(?:图片|照片|图)",
    r"(?:图片|照片|图像).*编辑",
    r"图生图",
    r"以图生图",
    r"image.*to.*image",
    r"i2i",
    r"把.*图.*(?:变|改|换|成|弄|P|修|转|优化)",
    r"把.*(?:P|p)成",                   # 把头发p成红色
    r"(?:P|p)成",                       # 头发p成红色
    r"把.*(?:换|弄)成",                 # 把背景换成蓝色、把头发弄成卷的
    r"(?:美化|优化|增强|滤镜|风格化).*(?:图片|照片|图)",
    r"换脸",
    r"换背景",
    r"(?:抠图|去背景|移除背景)",
    r"变换.*(?:图片|照片|图)",
    r"图片.*(?:变|改|换).*风格",
    r"风格.*(?:转换|迁移|变换)",
]

def detect_image_edit_intent(text: str) -> str | None:
    for pattern in IMAGE_EDIT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            prompt = _extract_prompt(text)
            if prompt:
                return prompt
            return text.strip()
    return None

ASPECT_TO_SIZE = {
    "1:1": "1024x1024",
    "16:9": "1536x1024",
    "9:16": "1024x1536",
    "4:3": "1024x768",
    "3:4": "768x1024",
    "3:2": "1024x683",
    "2:3": "683x1024",
}


def _extract_prompt(text: str) -> str:
    return _extract_prompt_base(text, IMAGE_KEYWORDS, "图片|照片|图像|插画|壁纸|头像|图")


_GIVE_ME_PATTERN = re.compile(
    r"(?:给我|来|发|找).{0,2}[张个幅](.+?)(?:图片|照片|图像|图)$"
)


def detect_image_intent(text: str) -> str | None:
    for pattern in IMAGE_INTENT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            prompt = _extract_prompt(text)
            if prompt and prompt != text.strip():
                return prompt
            m = _GIVE_ME_PATTERN.search(text)
            if m and m.group(1).strip():
                return m.group(1).strip()
            # Strip prefix (给我/来/发/找 + optional quantifier) and suffix (media type)
            cleaned = re.sub(r"^(?:给我|来|发|找)[一]?[张个幅]?", "", text)
            subject = re.sub(r"(?:图片|照片|图像|插画|壁纸|头像|图)$", "", cleaned).strip()
            if subject:
                return subject
            # Subject is the media type itself (e.g. "来个壁纸" -> "壁纸")
            if cleaned.strip():
                return cleaned.strip()
            return text.strip()
    return None


class MiniMaxImageGenerator:
    """MiniMax image-01 后端"""

    def __init__(self, api_key: str, base_url: str = "https://api.minimaxi.com/v1",
                 model: str = "image-01", image_dir: Path | None = None):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.image_dir = image_dir or IMAGE_DIR
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self, prompt: str, n: int = 1, aspect_ratio: str = "1:1",
                       prompt_optimizer: bool = True) -> list[str]:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": n,
            "aspect_ratio": aspect_ratio,
            "response_format": "url",
            "prompt_optimizer": prompt_optimizer,
        }
        async with httpx.AsyncClient(timeout=120.0) as client:
            request_started = time.monotonic()
            resp = await client.post(
                f"{self.base_url}/image_generation",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            logger.info(
                f"[IMAGE] MiniMax request finished in {time.monotonic() - request_started:.1f}s, "
                f"model={self.model}, status={resp.status_code}"
            )
            if resp.status_code != 200:
                raise RuntimeError(f"MiniMax 图片生成失败: {resp.status_code} {resp.text}")
            data = resp.json()
            base = data.get("base_resp", {})
            if base.get("status_code") != 0:
                raise RuntimeError(f"MiniMax 图片生成错误: {base.get('status_msg', 'unknown')}")
            urls = data.get("data", {}).get("image_urls", [])
            if not urls:
                raise RuntimeError(f"未获取到图片 URL: {data}")

        paths = []
        download_started = time.monotonic()
        for i, url in enumerate(urls):
            if url.startswith("data:"):
                paths.append(_save_data_uri(url, self.image_dir, i))
            else:
                paths.append(await download_async(url, self.image_dir, i))
        logger.info(
            f"[IMAGE] MiniMax image download/save finished in {time.monotonic() - download_started:.1f}s"
        )
        return paths


def _openai_v1_endpoint(base_url: str, path: str) -> str:
    """拼接 OpenAI 兼容端点，兼容 base URL 是否已包含 /v1。"""
    base = base_url.rstrip("/")
    versioned_base = base if base.endswith("/v1") else f"{base}/v1"
    return f"{versioned_base}/{path.lstrip('/')}"


class GPTImageGenerator:
    """GPT-Image (OpenAI 兼容) 后端"""

    def __init__(self, api_key: str, base_url: str = "https://api.openai.com/v1",
                 model: str = "gpt-image-2", image_dir: Path | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.image_dir = image_dir or IMAGE_DIR
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self, prompt: str, n: int = 1, aspect_ratio: str = "1:1",
                       prompt_optimizer: bool = True) -> list[str]:
        size = ASPECT_TO_SIZE.get(aspect_ratio, "1024x1024")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "n": n,
            "size": size,
            "response_format": "url",
        }
        # 生图响应耗时较长，继承系统代理容易被本机代理的空闲超时中断。
        # GPT 图像 API 固定直连；其他下载和后端仍沿用各自的网络配置。
        async with httpx.AsyncClient(
            timeout=300.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            request_started = time.monotonic()
            resp = await client.post(
                _openai_v1_endpoint(self.base_url, "images/generations"),
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            logger.info(
                f"[IMAGE] GPT-compatible request finished in {time.monotonic() - request_started:.1f}s, "
                f"model={self.model}, status={resp.status_code}, base={self.base_url}"
            )
            if resp.status_code != 200:
                raise RuntimeError(f"GPT-Image 生成失败: {resp.status_code} {resp.text}")
            data = resp.json()
            items = data.get("data", [])
            # 调试：记录 API 返回结构
            for i, item in enumerate(items):
                keys = list(item.keys())
                if "url" in item:
                    logger.info(f"[IMAGE] GPT API item[{i}] keys={keys}, url长度={len(item['url'])}, 前缀={item['url'][:30]}")
                elif "b64_json" in item:
                    logger.info(f"[IMAGE] GPT API item[{i}] keys={keys}, b64长度={len(item['b64_json'])}")
                else:
                    logger.info(f"[IMAGE] GPT API item[{i}] keys={keys}")

        paths = []
        download_started = time.monotonic()
        for i, item in enumerate(items):
            # 处理 b64_json 响应（API 返回 base64 编码的图片数据）
            if "b64_json" in item:
                paths.append(_save_b64_image(item["b64_json"], self.image_dir, i))
            elif "url" in item:
                url = item["url"]
                # 处理 data URI（data:image/...;base64,...）
                if url.startswith("data:"):
                    paths.append(_save_data_uri(url, self.image_dir, i))
                else:
                    paths.append(await download_async(url, self.image_dir, i))
        logger.info(
            f"[IMAGE] GPT-compatible image download/save finished in {time.monotonic() - download_started:.1f}s"
        )

        if not paths:
            raise RuntimeError(f"未获取到图片 URL: {data}")
        return paths


class GRSAIImageGenerator:
    """GRS draw completions backend for gpt-image-2."""

    def __init__(self, api_key: str, base_url: str = "https://grsai.dakka.com.cn",
                 model: str = "gpt-image-2", quality: str = "auto",
                 image_dir: Path | None = None):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.quality = quality
        self.image_dir = image_dir or IMAGE_DIR
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self, prompt: str, n: int = 1, aspect_ratio: str = "1:1",
                       prompt_optimizer: bool = True) -> list[str]:
        return await self.generate_from_urls(prompt, [], n=n, aspect_ratio=aspect_ratio)

    async def generate_from_urls(self, prompt: str, urls: list[str] | None = None,
                                 n: int = 1, aspect_ratio: str = "1:1") -> list[str]:
        size = ASPECT_TO_SIZE.get(aspect_ratio, "1024x1024")
        payload = {
            "model": self.model,
            "prompt": prompt,
            "aspectRatio": size,
            "quality": self.quality,
            "webHook": "-1",
            "shutProgress": True,
        }
        if urls:
            payload["urls"] = urls

        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            request_started = time.monotonic()
            resp = await client.post(
                f"{self.base_url}/v1/draw/completions",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json=payload,
            )
            logger.info(
                f"[IMAGE] GRS draw request finished in {time.monotonic() - request_started:.1f}s, "
                f"model={self.model}, status={resp.status_code}, base={self.base_url}"
            )
            if resp.status_code != 200:
                raise RuntimeError(f"GPT2 图片任务提交失败: {resp.status_code} {resp.text}")
            submit_data = resp.json()
            self._raise_api_error(submit_data, "GPT2 图片任务提交失败")
            task_id = self._extract_task_id(submit_data)
            if not task_id:
                result = self._extract_result(submit_data)
                if result and self._result_urls(result):
                    return await self._save_result_images(result)
                raise RuntimeError(f"GPT2 未返回任务 ID: {submit_data}")
            result = await self._poll_result(client, task_id)

        return await self._save_result_images(result)

    async def _poll_result(self, client: httpx.AsyncClient, task_id: str) -> dict:
        deadline = time.monotonic() + 300
        last_data = None
        while time.monotonic() < deadline:
            resp = await client.post(
                f"{self.base_url}/v1/draw/result",
                headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                json={"id": task_id},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"GPT2 查询结果失败: {resp.status_code} {resp.text}")
            last_data = resp.json()
            self._raise_api_error(last_data, "GPT2 查询结果失败")
            result = self._extract_result(last_data) or {}
            status = str(result.get("status", "")).lower()
            progress = result.get("progress")
            if status == "succeeded" or self._result_urls(result):
                logger.info(f"[IMAGE] GRS draw succeeded, id={task_id}, progress={progress}")
                return result
            if status == "failed":
                reason = result.get("failure_reason") or result.get("error") or last_data
                raise RuntimeError(f"GPT2 图片生成失败: {reason}")
            logger.info(f"[IMAGE] GRS draw running, id={task_id}, status={status}, progress={progress}")
            await asyncio.sleep(3)
        raise RuntimeError(f"GPT2 图片生成超时: id={task_id}, last={last_data}")

    async def _save_result_images(self, result: dict) -> list[str]:
        urls = self._result_urls(result)
        if not urls:
            raise RuntimeError(f"GPT2 未获取到图片 URL: {result}")
        paths = []
        download_started = time.monotonic()
        for i, url in enumerate(urls):
            if url.startswith("data:"):
                paths.append(_save_data_uri(url, self.image_dir, i))
            else:
                paths.append(await download_async(url, self.image_dir, i))
        logger.info(
            f"[IMAGE] GRS image download/save finished in {time.monotonic() - download_started:.1f}s"
        )
        return paths

    @staticmethod
    def _extract_task_id(data: dict) -> str | None:
        inner = data.get("data") if isinstance(data, dict) else None
        if isinstance(inner, dict) and inner.get("id"):
            return str(inner["id"])
        if isinstance(data, dict) and data.get("id"):
            return str(data["id"])
        return None

    @staticmethod
    def _extract_result(data: dict) -> dict | None:
        if not isinstance(data, dict):
            return None
        inner = data.get("data")
        if isinstance(inner, dict) and any(k in inner for k in ("status", "results", "url", "progress")):
            return inner
        if any(k in data for k in ("status", "results", "url", "progress")):
            return data
        return None

    @staticmethod
    def _result_urls(result: dict) -> list[str]:
        urls = []
        for item in result.get("results") or []:
            if isinstance(item, dict) and item.get("url"):
                urls.append(str(item["url"]))
        if not urls and result.get("url"):
            urls.append(str(result["url"]))
        return urls

    @staticmethod
    def _raise_api_error(data: dict, prefix: str) -> None:
        if not isinstance(data, dict):
            return
        code = data.get("code")
        if code not in (None, 0):
            msg = data.get("msg") or data.get("message") or data
            raise RuntimeError(f"{prefix}: code={code}, msg={msg}")


def _save_b64_image(b64_data: str, image_dir: Path, index: int = 0) -> str:
    """将 base64 编码的图片数据保存为本地文件"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = ".png"
    if b64_data.startswith("/9j/"):
        ext = ".jpg"
    elif b64_data.startswith("UklGR"):
        ext = ".webp"
    filename = f"{ts}_b64_{index}{ext}"
    filepath = image_dir / filename
    filepath.write_bytes(base64.b64decode(b64_data))
    return str(filepath)


def _save_data_uri(data_uri: str, image_dir: Path, index: int = 0) -> str:
    """将 data URI（data:image/...;base64,...）保存为本地文件"""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    ext = ".png"
    if "jpeg" in data_uri or "jpg" in data_uri:
        ext = ".jpg"
    elif "webp" in data_uri:
        ext = ".webp"
    elif "gif" in data_uri:
        ext = ".gif"
    b64_data = data_uri.split(",", 1)[1] if "," in data_uri else data_uri
    filename = f"{ts}_datauri_{index}{ext}"
    filepath = image_dir / filename
    filepath.write_bytes(base64.b64decode(b64_data))
    return str(filepath)


# ---- 全局状态 ----

_generators: dict[str, MiniMaxImageGenerator | GPTImageGenerator | GRSAIImageGenerator] = {}
_active_model: str = "gpt1"


def init_image_generators(
    minimax_api_key: str,
    minimax_base_url: str = "https://api.minimaxi.com/v1",
    minimax_model: str = "image-01",
    gpt_api_key: str = "",
    gpt_base_url: str = "https://api.openai.com/v1",
    gpt_model: str = "gpt-image-2",
    gpt2_api_key: str = "",
    gpt2_base_url: str = "https://grsai.dakka.com.cn",
    gpt2_model: str = "gpt-image-2",
    gpt2_quality: str = "auto",
) -> None:
    global _generators, _active_model
    _generators = {}
    if minimax_api_key:
        _generators["minimax"] = MiniMaxImageGenerator(
            api_key=minimax_api_key, base_url=minimax_base_url, model=minimax_model
        )
        logger.info("[IMAGE] MiniMax 后端已就绪")
    if gpt_api_key:
        _generators["gpt1"] = GPTImageGenerator(
            api_key=gpt_api_key, base_url=gpt_base_url, model=gpt_model
        )
        logger.info("[IMAGE] GPT OpenAI-compatible 后端已就绪")
    if gpt2_api_key:
        _generators["gpt2"] = GRSAIImageGenerator(
            api_key=gpt2_api_key, base_url=gpt2_base_url, model=gpt2_model, quality=gpt2_quality
        )
        logger.info("[IMAGE] GPT2 GRS 后端已就绪")
    if _active_model not in _generators and _generators:
        for preferred in ("gpt1", "minimax", "gpt2"):
            if preferred in _generators:
                _active_model = preferred
                break
    _schedule_cleanup()


def get_image_generator(name: str | None = None):
    """返回指定图片生成器；未指定时返回当前激活的生成器。"""
    return _generators.get(name or _active_model)


def get_current_image_model() -> str:
    return _active_model


def switch_image_model(name: str) -> str:
    """切换图片模型，返回状态消息"""
    global _active_model
    name = name.lower().strip()
    if name in ("mm", "minimax"):
        name = "minimax"
    elif name in ("gpt", "gpt1", "gpt-image-2", "gpt-image", "gptimage"):
        name = "gpt1"
    elif name in ("gpt2", "grs", "grsai", "oldgpt"):
        name = "gpt2"
    if name not in _generators:
        available = ", ".join(_generators.keys()) or "(无)"
        return f"图片模型 {name} 不可用，可用: {available}，当前: {_active_model}"
    _active_model = name
    logger.info(f"[IMAGE] 切换到 {name}")
    return f"图片模型已切换到 {name}"


def _schedule_cleanup():
    job_id = "image_cleanup"
    if scheduler.get_job(job_id):
        return

    @scheduler.scheduled_job("interval", hours=CLEANUP_HOURS, id=job_id, misfire_grace_time=60)
    def cleanup_job():
        _cleanup_all()


def _cleanup_all():
    seen = set()
    for gen in _generators.values():
        d = str(gen.image_dir)
        if d in seen:
            continue
        seen.add(d)
        _cleanup_dir(gen.image_dir)


def _cleanup_dir(image_dir: Path):
    if not image_dir.exists():
        return
    cutoff = datetime.now() - timedelta(hours=CLEANUP_HOURS)
    deleted = 0
    for f in image_dir.iterdir():
        if f.is_file() and f.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
            mtime = datetime.fromtimestamp(f.stat().st_mtime)
            if mtime < cutoff:
                try:
                    f.unlink()
                    deleted += 1
                except OSError as e:
                    logger.warning(f"[IMAGE] 删除失败: {f}, {e}")
    if deleted:
        logger.info(f"[IMAGE] 清理了 {deleted} 个过期图片")
