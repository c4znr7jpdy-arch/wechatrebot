"""
图生图 / 图片编辑模块 — 支持 GPT image edit 和 MiniMax i2i
"""
import base64
from pathlib import Path

import httpx
from nonebot import logger

from .utils import download_async
from .image_generator import (
    GRSAIImageGenerator,
    _openai_v1_endpoint,
    _save_b64_image,
    _save_data_uri,
)

IMAGE_DIR = Path(__file__).parent.parent / "data" / "edited_images"


class ImageEditor:
    """图生图后端（GPT image edit + MiniMax i2i）"""

    def __init__(self, gpt_api_key: str = "", gpt_base_url: str = "https://api.openai.com/v1",
                 gpt_model: str = "gpt-image-2",
                 gpt2_api_key: str = "", gpt2_base_url: str = "https://grsai.dakka.com.cn",
                 gpt2_model: str = "gpt-image-2", gpt2_quality: str = "auto",
                 minimax_api_key: str = "", minimax_base_url: str = "https://api.minimaxi.com/v1",
                 minimax_model: str = "image-01"):
        self.gpt_api_key = gpt_api_key
        self.gpt_base_url = gpt_base_url.rstrip("/")
        self.gpt_model = gpt_model
        self.gpt2_generator = (
            GRSAIImageGenerator(
                api_key=gpt2_api_key,
                base_url=gpt2_base_url,
                model=gpt2_model,
                quality=gpt2_quality,
                image_dir=IMAGE_DIR,
            )
            if gpt2_api_key
            else None
        )
        self.minimax_api_key = minimax_api_key
        self.minimax_base_url = minimax_base_url.rstrip("/")
        self.minimax_model = minimax_model
        self.image_dir = IMAGE_DIR
        self.image_dir.mkdir(parents=True, exist_ok=True)

    async def edit(self, image_path: str, prompt: str, model: str = "gpt1",
                   n: int = 1, aspect_ratio: str = "1:1") -> list[str]:
        model = (model or "gpt1").lower().strip()
        if model == "gpt":
            model = "gpt1"
        if model == "gpt2" and self.gpt2_generator:
            return await self._edit_gpt2(image_path, prompt, n, aspect_ratio)
        if model == "gpt1" and self.gpt_api_key:
            return await self._edit_gpt(image_path, prompt, n, aspect_ratio)
        if model == "minimax" and self.minimax_api_key:
            try:
                return await self._edit_minimax(image_path, prompt, n, aspect_ratio)
            except RuntimeError as e:
                # 内容审核拒绝时自动 fallback 到 GPT
                if "sensitive" in str(e).lower():
                    if self.gpt2_generator:
                        logger.warning(f"[IMAGE EDITOR] MiniMax 内容审核拒绝，回退 GPT2: {e}")
                        return await self._edit_gpt2(image_path, prompt, n, aspect_ratio)
                    if self.gpt_api_key:
                        logger.warning(f"[IMAGE EDITOR] MiniMax 内容审核拒绝，回退 GPT: {e}")
                        return await self._edit_gpt(image_path, prompt, n, aspect_ratio)
                raise
        available = []
        if self.gpt2_generator:
            available.append("gpt2")
        if self.gpt_api_key:
            available.append("gpt1")
        if self.minimax_api_key:
            available.append("minimax")
        raise RuntimeError(f"图生图模型 {model} 不可用，可用: {', '.join(available) or '(无)'}")

    async def _edit_gpt2(self, image_path: str, prompt: str, n: int = 1,
                         aspect_ratio: str = "1:1") -> list[str]:
        if not self.gpt2_generator:
            raise RuntimeError("GPT2 图生图后端未配置 API Key")
        image_uri = _encode_image_data_uri(image_path)
        return await self.gpt2_generator.generate_from_urls(
            prompt=prompt,
            urls=[image_uri],
            n=n,
            aspect_ratio=aspect_ratio,
        )

    async def _edit_gpt(self, image_path: str, prompt: str, n: int = 1,
                        aspect_ratio: str = "1:1") -> list[str]:
        size_map = {"1:1": "1024x1024", "16:9": "1536x1024", "9:16": "1024x1536",
                    "4:3": "1024x768", "3:4": "768x1024", "3:2": "1024x683", "2:3": "683x1024"}
        size = size_map.get(aspect_ratio, "1024x1024")
        url = _openai_v1_endpoint(self.gpt_base_url, "images/edits")

        # 与文生图保持一致，避免长请求经过系统代理后被提前断开。
        async with httpx.AsyncClient(
            timeout=300.0,
            follow_redirects=True,
            trust_env=False,
        ) as client:
            with open(image_path, "rb") as f:
                files = {
                    "image": (Path(image_path).name, f, "image/png"),
                }
                data = {
                    "model": self.gpt_model,
                    "prompt": prompt,
                    "n": str(n),
                    "size": size,
                    "response_format": "url",
                }
                resp = await client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.gpt_api_key}"},
                    files=files,
                    data=data,
                )
            if resp.status_code != 200:
                raise RuntimeError(f"GPT 图片编辑失败: {resp.status_code} {resp.text}")
            result = resp.json()
            items = result.get("data", [])

        paths = []
        for i, item in enumerate(items):
            if "b64_json" in item:
                paths.append(_save_b64_image(item["b64_json"], self.image_dir, i))
            elif "url" in item:
                url = item["url"]
                if url.startswith("data:"):
                    paths.append(_save_data_uri(url, self.image_dir, i))
                else:
                    paths.append(await download_async(url, self.image_dir, i))

        if not paths:
            raise RuntimeError(f"未获取到编辑后图片 URL: {result}")
        return paths

    async def _edit_minimax(self, image_path: str, prompt: str, n: int = 1,
                            aspect_ratio: str = "1:1") -> list[str]:
        image_uri = _encode_image_data_uri(image_path)
        payload = {
            "model": self.minimax_model,
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "subject_reference": [
                {
                    "type": "character",
                    "image_file": image_uri,
                }
            ],
            "n": n,
            "response_format": "url",
        }
        url = f"{self.minimax_base_url}/image_generation"
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.minimax_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"MiniMax 图生图失败: {resp.status_code} {resp.text}")
            data = resp.json()
            base_resp = data.get("base_resp", {})
            if base_resp.get("status_code") != 0:
                raise RuntimeError(f"MiniMax 图生图错误: {base_resp.get('status_msg', 'unknown')}")
            urls = data.get("data", {}).get("image_urls", [])
            if not urls:
                raise RuntimeError(f"未获取到编辑后图片 URL: {data}")

        paths = []
        for i, img_url in enumerate(urls):
            paths.append(await download_async(img_url, self.image_dir, i))
        return paths


def _encode_image_data_uri(image_path: str) -> str:
    """将本地图片编码为 data URI"""
    ext = Path(image_path).suffix.lower()
    mime_map = {".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}
    mime = mime_map.get(ext, "image/png")
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


def _encode_image_b64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


_editor: ImageEditor | None = None
_active_editor_model: str = "gpt1"


def init_image_editor(gpt_api_key: str = "", gpt_base_url: str = "https://api.openai.com/v1",
                      gpt_model: str = "gpt-image-2",
                      gpt2_api_key: str = "", gpt2_base_url: str = "https://grsai.dakka.com.cn",
                      gpt2_model: str = "gpt-image-2", gpt2_quality: str = "auto",
                      minimax_api_key: str = "", minimax_base_url: str = "https://api.minimaxi.com/v1",
                      minimax_model: str = "image-01") -> None:
    global _editor, _active_editor_model
    _editor = ImageEditor(
        gpt_api_key=gpt_api_key, gpt_base_url=gpt_base_url, gpt_model=gpt_model,
        gpt2_api_key=gpt2_api_key, gpt2_base_url=gpt2_base_url,
        gpt2_model=gpt2_model, gpt2_quality=gpt2_quality,
        minimax_api_key=minimax_api_key, minimax_base_url=minimax_base_url,
        minimax_model=minimax_model,
    )
    available = {
        "gpt1": bool(_editor.gpt_api_key),
        "minimax": bool(_editor.minimax_api_key),
        "gpt2": bool(_editor.gpt2_generator),
    }
    if not available.get(_active_editor_model):
        for preferred in ("gpt1", "minimax", "gpt2"):
            if available[preferred]:
                _active_editor_model = preferred
                break
    logger.info("[IMAGE EDITOR] 图生图编辑器已就绪")


def get_image_editor() -> ImageEditor | None:
    return _editor


def get_current_editor_model() -> str:
    return _active_editor_model


def switch_editor_model(name: str) -> str:
    global _active_editor_model, _editor
    name = name.lower().strip()
    if name in ("mm", "minimax"):
        name = "minimax"
    elif name in ("gpt", "gpt1", "gpt-image-2", "gpt-image", "gptimage"):
        name = "gpt1"
    elif name in ("gpt2", "grs", "grsai", "oldgpt"):
        name = "gpt2"
    else:
        return f"图生图模型名 {name} 不认识，可用: gpt2, gpt1, minimax"
    if _editor is None:
        return "图生图编辑器未初始化"
    if name == "gpt2" and not _editor.gpt2_generator:
        return "GPT2 图生图后端未配置 API Key"
    if name == "gpt1" and not _editor.gpt_api_key:
        return "GPT1 图生图后端未配置 API Key"
    if name == "minimax" and not _editor.minimax_api_key:
        return "MiniMax 图生图后端未配置 API Key"
    _active_editor_model = name
    logger.info(f"[IMAGE EDITOR] 切换到 {name}")
    return f"图生图模型已切换到 {name}"
