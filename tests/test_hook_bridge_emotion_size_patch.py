import asyncio
import hashlib
import importlib
import importlib.util
from pathlib import Path
import random
import shutil
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

from PIL import Image
import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def bridge(tmp_path_factory):
    target = tmp_path_factory.mktemp("emotion-bridge")
    source = ROOT / "vendor/hook_41827_astrbot_bridge"
    shutil.copytree(source, target / "hook_41827_astrbot_bridge")
    subprocess.run(["git", "init", "--quiet"], cwd=target, check=True)
    # Exercise the complete bridge patch stack, including new-file delivery.
    for patch in sorted((ROOT / "patches").glob("hook-bridge-*.patch")):
        subprocess.run(["git", "apply", "--ignore-space-change", str(patch)],
                       cwd=target, check=True, capture_output=True)
    package = target / "hook_41827_astrbot_bridge/src/hook_bridge"
    name = "_emotion_test_bridge"
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py",
                                                   submodule_search_locations=[str(package)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    try:
        yield SimpleNamespace(**{part: importlib.import_module(f"{name}.{part}")
                                 for part in ("media", "hook_client", "models", "emotion_media")})
    finally:
        for key in list(sys.modules):
            if key == name or key.startswith(name + "."):
                sys.modules.pop(key)


def test_large_gif_becomes_bounded_sticker_without_modifying_source(bridge, tmp_path):
    source = tmp_path / "large.gif"
    rng = random.Random(7)
    Image.frombytes("RGB", (1254, 1254), rng.randbytes(1254 * 1254 * 3)).save(source)
    original = hashlib.sha256(source.read_bytes()).hexdigest()
    store = bridge.media.MediaStore(tmp_path / "cache", None, 20 * 1024 * 1024)
    result = asyncio.run(store.prepare_emotion(source))
    assert result != source and result.stat().st_size <= bridge.emotion_media.EMOTION_MAX_BYTES
    assert hashlib.sha256(source.read_bytes()).hexdigest() == original
    with Image.open(result) as image:
        assert image.format == "GIF" and max(image.size) <= 240


def test_animation_timing_transparency_and_loop_survive(bridge, tmp_path):
    source = tmp_path / "animated.gif"
    frames = []
    for color in ("red", "blue", "green"):
        frame = Image.new("RGBA", (500, 500), (0, 0, 0, 0))
        frame.paste(color, (150, 150, 350, 350))
        frames.append(frame)
    frames[0].save(source, save_all=True, append_images=frames[1:],
                   duration=[100, 200, 300], loop=2, disposal=2)
    store = bridge.media.MediaStore(tmp_path / "cache", None, 20 * 1024 * 1024)
    result = asyncio.run(store.prepare_emotion(source))
    with Image.open(result) as image:
        assert image.n_frames == 3 and image.info["loop"] == 2
        timings = []
        for index in range(image.n_frames):
            image.seek(index)
            timings.append(image.info["duration"])
            assert image.convert("RGBA").getpixel((0, 0))[3] == 0
        assert timings == [100, 200, 300]


def test_small_gif_unchanged_and_png_converted(bridge, tmp_path):
    store = bridge.media.MediaStore(tmp_path / "cache", None, 1024 * 1024)
    for extension in ("gif", "png"):
        source = tmp_path / f"small.{extension}"
        Image.new("RGB", (80, 80), "red").save(source)
        result = asyncio.run(store.prepare_emotion(source))
        assert (result == source) is (extension == "gif")
        with Image.open(result) as image:
            assert image.format == "GIF"


def test_unfit_sticker_does_not_leave_cache_file(bridge, tmp_path, monkeypatch):
    source = tmp_path / "image.png"
    Image.new("RGB", (500, 500), "red").save(source)
    cache = tmp_path / "cache"
    store = bridge.media.MediaStore(cache, None, 1024 * 1024)
    monkeypatch.setattr(bridge.emotion_media, "EMOTION_MAX_BYTES", 1)
    with pytest.raises(bridge.media.UnsupportedMediaSource):
        asyncio.run(store.prepare_emotion(source))
    assert not list(cache.iterdir())


@pytest.mark.parametrize("ok", [False, True])
def test_hook_file_downgrade_is_not_reported_as_sticker_success(bridge, tmp_path, ok):
    original = bridge.models.SendResult(ok=ok, message_id="123", raw={
        "errCode": 10002, "errMsg": "如果表情包图片大小太大的话系统会把他当作文件发的",
    })
    client = SimpleNamespace(_write=AsyncMock(return_value=original))
    result = asyncio.run(bridge.hook_client.HookClient.send_emotion(client, "target", tmp_path / "x.gif"))
    assert result.ok is False and "sticker delivery failed" in result.error
    assert result.message_id == "123" and result.raw == original.raw
    client._write.assert_awaited_once()
