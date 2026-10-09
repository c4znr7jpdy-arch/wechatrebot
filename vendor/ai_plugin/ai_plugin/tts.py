"""
TTS (Text-to-Speech) → silk 语音文件生成模块
流程: 文本 → edge-tts(mp3) → ffmpeg(pcm) → pilk(silk)
发送: silk → CDN上传 → 构造voicemsg XML → send_raw_xml
"""
import asyncio
import hashlib
import os
import subprocess
import time
from pathlib import Path

import edge_tts
import pilk

from nonebot import Bot, logger

_VOICE_DIR = Path(__file__).parent.parent / "data" / "voice"
_VOICE_DIR.mkdir(parents=True, exist_ok=True)

_DEFAULT_VOICE = "zh-CN-XiaoyiNeural"
_SAMPLE_RATE = 24000

VOICE_OPTIONS = {
    "小艺": "zh-CN-XiaoyiNeural",
    "晓晓": "zh-CN-XiaoxiaoNeural",
    "云扬": "zh-CN-YunyangNeural",
    "云希": "zh-CN-YunxiNeural",
    "晓萱": "zh-CN-XiaoxuanNeural",
    "晓墨": "zh-CN-XiaomoNeural",
}

_current_voice = _DEFAULT_VOICE


def set_voice(voice_name: str) -> str:
    global _current_voice
    if voice_name in VOICE_OPTIONS:
        _current_voice = VOICE_OPTIONS[voice_name]
        return voice_name
    for name, vid in VOICE_OPTIONS.items():
        if voice_name.lower() in vid.lower():
            _current_voice = vid
            return name
    return ""


def get_current_voice() -> str:
    for name, vid in VOICE_OPTIONS.items():
        if vid == _current_voice:
            return name
    return _current_voice


async def tts_to_silk(text: str, voice: str | None = None) -> str | None:
    """文本转 silk 语音文件，返回 silk 文件路径，失败返回 None"""
    if not text.strip():
        return None

    voice = voice or _current_voice
    ts = int(time.time() * 1000)
    mp3_path = _VOICE_DIR / f"tts_{ts}.mp3"
    pcm_path = _VOICE_DIR / f"tts_{ts}.pcm"
    silk_path = _VOICE_DIR / f"tts_{ts}.silk"

    try:
        communicate = edge_tts.Communicate(text, voice)
        await communicate.save(str(mp3_path))

        if not mp3_path.exists() or mp3_path.stat().st_size == 0:
            logger.error("[TTS] edge-tts 生成 mp3 失败")
            return None

        _mp3_to_pcm(str(mp3_path), str(pcm_path))

        if not pcm_path.exists() or pcm_path.stat().st_size == 0:
            logger.error("[TTS] ffmpeg 转 pcm 失败")
            return None

        duration_ms = _pcm_to_silk(str(pcm_path), str(silk_path))
        logger.info(f"[TTS] 生成 silk: {silk_path} ({duration_ms}ms)")

        return str(silk_path) if silk_path.exists() else None

    except Exception as e:
        logger.exception(f"[TTS] 转换失败: {e}")
        return None
    finally:
        mp3_path.unlink(missing_ok=True)
        pcm_path.unlink(missing_ok=True)


async def send_voice_via_cdn(bot: Bot, to_wxid: str, silk_path: str) -> dict:
    """通过 CDN 上传 silk 文件并尝试以语音消息发送

    返回: {"ok": bool, "msg": str}
    """
    silk_file = Path(silk_path)
    if not silk_file.exists():
        return {"ok": False, "msg": f"silk 文件不存在: {silk_path}"}

    file_size = silk_file.stat().st_size
    file_md5 = hashlib.md5(silk_file.read_bytes()).hexdigest()
    voice_length = _estimate_silk_duration_ms(silk_path)

    # Step 1: CDN 上传 (尝试不同 file_type)
    cdn_result = None
    for upload_type in (5, 4):
        try:
            cdn_result = await bot.call_api(
                "cdn_upload",
                file_path=str(silk_file.resolve()),
                file_type=upload_type,
            )
            logger.info(f"[TTS CDN] upload(file_type={upload_type}) result: {cdn_result}")
            if cdn_result and isinstance(cdn_result, dict) and cdn_result.get("file_id"):
                break
        except Exception as e:
            logger.warning(f"[TTS CDN] upload(file_type={upload_type}) failed: {e}")

    if not cdn_result or not isinstance(cdn_result, dict):
        return {"ok": False, "msg": f"CDN 上传返回异常: {cdn_result}"}

    file_id = cdn_result.get("file_id", "")
    aes_key = cdn_result.get("aes_key", "")

    if not file_id or not aes_key:
        return {"ok": False, "msg": f"CDN 上传缺少 file_id/aes_key: {cdn_result}"}

    # Step 2: 尝试多种 CDN 发送方式
    cdn_data = {
        "file_id": file_id,
        "aes_key": aes_key,
        "file_md5": file_md5,
        "file_size": file_size,
        "voice_length": voice_length,
    }

    for send_type in (11232, 11234):
        try:
            result = await bot.call_api(
                "cdn_send",
                to_wxid=to_wxid,
                send_type=send_type,
                cdn_data=cdn_data,
            )
            logger.info(f"[TTS CDN] cdn_send type={send_type} result: {result}")
        except Exception as e:
            logger.warning(f"[TTS CDN] cdn_send type={send_type} failed: {e}")

    return {"ok": True, "msg": f"已尝试发送 (file_id={file_id[:20]}...)"}


def _build_voicemsg_xml(file_id: str, aes_key: str, file_size: int,
                        voice_length: int, file_md5: str) -> str:
    """构造微信 voicemsg XML"""
    return (
        f'<msg><voicemsg endflag="1" cancelflag="0" forwardflag="1" '
        f'voiceformat="4" voicelength="{voice_length}" '
        f'length="{file_size}" bufid="0" '
        f'aeskey="{aes_key}" voiceurl="{file_id}" '
        f'clientmsgid="" fromusername="" /></msg>'
    )


def _estimate_silk_duration_ms(silk_path: str) -> int:
    """估算 silk 文件时长(ms)，基于文件大小粗略计算"""
    # silk 编码约 ~2.4KB/s (24000Hz, tencent mode)
    file_size = os.path.getsize(silk_path)
    return max(1000, int(file_size / 2.4))


def _mp3_to_pcm(mp3_path: str, pcm_path: str):
    """mp3 → 单声道 16bit PCM (24000Hz)"""
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", mp3_path,
            "-f", "s16le", "-ac", "1", "-ar", str(_SAMPLE_RATE),
            pcm_path,
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def _pcm_to_silk(pcm_path: str, silk_path: str) -> int:
    """PCM → silk，返回时长(ms)"""
    return pilk.encode(pcm_path, silk_path, pcm_rate=_SAMPLE_RATE, tencent=True)


def cleanup_old_voice_files(max_age_hours: int = 2):
    """清理过期语音文件"""
    now = time.time()
    for f in _VOICE_DIR.glob("tts_*.*"):
        if now - f.stat().st_mtime > max_age_hours * 3600:
            f.unlink(missing_ok=True)
