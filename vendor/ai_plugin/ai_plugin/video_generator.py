"""
MiniMax 视频生成模块 — 文生视频 (T2V)
API 文档: https://platform.minimaxi.com/docs/guides/video-generation

流程: submit → poll → download → 返回本地路径
"""
import asyncio
import re
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from nonebot import logger, require

require("nonebot_plugin_apscheduler")
from nonebot_plugin_apscheduler import scheduler  # noqa: E402

from .utils import extract_prompt as _extract_prompt_base, VIDEO_KEYWORDS

VIDEO_DIR = Path(__file__).parent.parent / "data" / "videos"
POLL_INTERVAL = 3  # 轮询间隔秒数
MAX_POLL_TIME = 300  # 最大等待秒数
CLEANUP_HOURS = 2

# 触发视频生成的关键词
VIDEO_INTENT_PATTERNS = [
    r"生成.*视频",
    r"制作.*视频",
    r"帮我.*视频",
    r"创作.*视频",
    r"做个?视频",
    r"拍个?视频",
    r"生成一个?.*(?:视频|短片|MV|动画)",
    r"制作一个?.*(?:视频|短片|MV|动画)",
    r"(?:视频|短片|MV).*生成",
    r"文生视频",
    r"text.*to.*video",
    r"t2v",
]


def _extract_prompt(text: str) -> str:
    return _extract_prompt_base(text, VIDEO_KEYWORDS, "视频|短片|MV|动画")


def detect_video_intent(text: str) -> str | None:
    """检测是否为视频生成请求，返回提取的提示词或 None"""
    for pattern in VIDEO_INTENT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            prompt = _extract_prompt(text)
            if prompt:
                return prompt
    return None


class VideoGenerator:
    """MiniMax 视频生成器"""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-Hailuo-02",
        video_dir: str | None = None,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.video_dir = Path(video_dir) if video_dir else VIDEO_DIR
        self.video_dir.mkdir(parents=True, exist_ok=True)

    async def generate(self, prompt: str, duration: int = 6, resolution: str = "1080P") -> str:
        """生成视频，返回本地文件路径"""
        task_id = await self._submit(prompt, duration, resolution)
        logger.info(f"[VIDEO] 任务已提交: task_id={task_id}")

        file_id = await self._poll(task_id)
        logger.info(f"[VIDEO] 生成完成: file_id={file_id}")

        download_url = await self._get_download_url(file_id)
        logger.info(f"[VIDEO] 下载地址: {download_url[:80]}...")

        local_path = await self._download(download_url)
        logger.info(f"[VIDEO] 已保存到: {local_path}")
        return local_path

    async def _submit(self, prompt: str, duration: int, resolution: str) -> str:
        payload = {
            "model": self.model,
            "prompt": prompt,
            "duration": duration,
            "resolution": resolution,
        }
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{self.base_url}/video_generation",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            if resp.status_code != 200:
                raise RuntimeError(f"MiniMax 视频提交失败: {resp.status_code} {resp.text}")
            data = resp.json()
            base = data.get("base_resp", {})
            if base.get("status_code") != 0:
                raise RuntimeError(f"MiniMax 视频提交错误: {base.get('status_msg', 'unknown')}")
            task_id = data.get("task_id")
            if not task_id:
                raise RuntimeError(f"未获取到 task_id: {data}")
            return task_id

    async def _poll(self, task_id: str) -> str:
        start = time.time()
        while time.time() - start < MAX_POLL_TIME:
            await asyncio.sleep(POLL_INTERVAL)
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.get(
                    f"{self.base_url}/query/video_generation",
                    params={"task_id": task_id},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if resp.status_code != 200:
                    logger.warning(f"[VIDEO] 查询状态失败: {resp.status_code}")
                    continue
                data = resp.json()
                status = data.get("status", "")
                if status == "Success":
                    file_id = data.get("file_id", "")
                    if not file_id:
                        raise RuntimeError("生成成功但未获取到 file_id")
                    return file_id
                elif status in ("Fail", "Failed"):
                    raise RuntimeError(f"视频生成失败: {data}")
                else:
                    logger.info(f"[VIDEO] 状态: {status} (已等 {int(time.time() - start)}s)")
        raise TimeoutError(f"视频生成超时 ({MAX_POLL_TIME}s)")

    async def _get_download_url(self, file_id: str) -> str:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(
                f"{self.base_url}/files/retrieve",
                params={"file_id": file_id},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            if resp.status_code != 200:
                raise RuntimeError(f"获取下载链接失败: {resp.status_code} {resp.text}")
            data = resp.json()
            base = data.get("base_resp", {})
            if base.get("status_code") != 0:
                raise RuntimeError(f"获取下载链接错误: {base.get('status_msg', 'unknown')}")
            url = data.get("file", {}).get("download_url", "")
            if not url:
                raise RuntimeError(f"未获取到 download_url: {data}")
            return url

    async def _download(self, url: str) -> str:
        filename = f"{datetime.now().strftime('%Y%m%d_%H%M%S')}_{hash(url) & 0xFFFF:04x}.mp4"
        filepath = self.video_dir / filename
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                raise RuntimeError(f"下载视频失败: {resp.status_code}")
            filepath.write_bytes(resp.content)
        return str(filepath)

    def cleanup(self):
        """清理超过 CLEANUP_HOURS 小时的视频文件"""
        if not self.video_dir.exists():
            return
        cutoff = datetime.now() - timedelta(hours=CLEANUP_HOURS)
        deleted = 0
        for f in self.video_dir.iterdir():
            if f.is_file() and f.suffix in (".mp4", ".mov", ".avi", ".webm"):
                mtime = datetime.fromtimestamp(f.stat().st_mtime)
                if mtime < cutoff:
                    try:
                        f.unlink()
                        deleted += 1
                    except OSError as e:
                        logger.warning(f"[VIDEO] 删除失败: {f}, {e}")
        if deleted:
            logger.info(f"[VIDEO] 清理了 {deleted} 个过期视频")


# 全局实例，由 __init__.py 初始化
_video_generator: VideoGenerator | None = None


def get_video_generator() -> VideoGenerator | None:
    return _video_generator


def init_video_generator(api_key: str, base_url: str | None = None, model: str | None = None) -> VideoGenerator:
    global _video_generator
    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    if model:
        kwargs["model"] = model
    _video_generator = VideoGenerator(**kwargs)

    # 注册定时清理任务
    _schedule_cleanup()

    return _video_generator


def _schedule_cleanup():
    """每 2 小时执行一次视频清理"""
    job_id = "video_cleanup"

    # 避免重复注册
    if scheduler.get_job(job_id):
        return

    @scheduler.scheduled_job("interval", hours=CLEANUP_HOURS, id=job_id, misfire_grace_time=60)
    def cleanup_job():
        vg = _video_generator
        if vg:
            vg.cleanup()
