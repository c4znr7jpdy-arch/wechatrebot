"""Publish tweet pages through the existing authenticated content service."""

from __future__ import annotations

import asyncio
import base64
import importlib
from html import escape
from io import BytesIO

from PIL import Image


class TweetPageBridge:
    @staticmethod
    def _active_plugin():
        for module_name in (
            "data.plugins.jiang_short_drama.main",
            "jiang_short_drama.main",
        ):
            try:
                module = importlib.import_module(module_name)
            except ImportError:
                continue
            plugin = getattr(module, "_ACTIVE_PLUGIN", None)
            if plugin is not None:
                return plugin
        return None

    @staticmethod
    def _avatar_jpeg(uri: str) -> str:
        raw = base64.b64decode(uri.split(",", 1)[1], validate=True)
        with Image.open(BytesIO(raw)) as image:
            if image.width * image.height > 16_000_000:
                raise ValueError("推主头像像素过大")
            image.thumbnail((240, 240))
            output = BytesIO()
            image.convert("RGB").save(output, "JPEG", quality=88)
        return base64.b64encode(output.getvalue()).decode("ascii")

    async def create_urls(self, snapshot: dict, avatar_cache) -> tuple[str, str]:
        plugin = self._active_plugin()
        store = getattr(plugin, "selection_store", None)
        if store is None or not store.enabled or not hasattr(store, "store_payload"):
            raise ValueError("推文页面服务未配置，请检查内容服务")
        # Reuse the original renderer even though its local listener is disabled.
        server = plugin.player_server
        renderer = importlib.import_module(type(server).__module__)._tweet_page_html
        avatar_source = str(snapshot.get("avatar") or "")
        uri = await avatar_cache.get(avatar_source) if avatar_cache else None
        if avatar_source and not uri:
            raise ValueError("推主头像下载失败，暂时使用文字推送")
        avatar = await asyncio.to_thread(self._avatar_jpeg, uri) if uri else ""
        html = renderer(snapshot)
        if avatar:
            html = html.replace(
                f'src="{escape(avatar_source, quote=True)}"',
                f'src="data:image/jpeg;base64,{avatar}"',
            )
        token = await store.store_payload({
            "v": 1, "kind": "tweet", "html": html, "avatar": avatar,
        })
        if not token:
            raise ValueError("推文页面保存失败，暂时使用文字推送")
        base = store.player_url.removesuffix("/player")
        url = f"{base}/twitter/{token}"
        return url, f"{url}/avatar" if avatar else ""
