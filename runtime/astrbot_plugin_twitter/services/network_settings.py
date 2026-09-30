"""微信管理页的持久化网络设置与有界健康检查。"""

import asyncio
import copy
from dataclasses import replace
from urllib.parse import urlsplit

from astrbot.api import logger

MODES = {"文字": "text", "卡片": "card", "截图": "screenshot",
         "text": "text", "card": "card", "screenshot": "screenshot"}


def network_url(value, *, proxy=False, optional=False):
    if not isinstance(value, str):
        raise ValueError("网络地址必须是文本")
    value = value.strip().rstrip("/")
    if not value and optional:
        return value
    try:
        parts = urlsplit(value)
        protocols = {"http", "https", "socks5"} if proxy else {"http", "https"}
        if (parts.scheme not in protocols or not parts.hostname or parts.query or parts.fragment
                or any(ord(c) < 33 for c in value)
                or (proxy and (not parts.port or parts.path))
                or (not proxy and (parts.username is not None or parts.password is not None))):
            raise ValueError()
        _ = parts.port
    except ValueError:
        raise ValueError("代理需填写协议、主机和端口；数据源需填写有效的 HTTP(S) 地址") from None
    return value


class NetworkSettings:
    def __init__(self, plugin):
        self.plugin = plugin
        self.checking = False
        self.error = ""
        self.timeout = 20.0

    def settings(self):
        p = self.plugin
        return {
            "data_provider": p.data_provider,
            "fxtwitter_api_base": p.fxtwitter_api_base,
            "nitter_url": p.custom_nitter_url,
            "proxy": p.proxy or "",
            "delivery_mode": p.text_render_mode,
            "screenshot_theme": p.screenshot_theme,
            "pre_download_media": p.pre_download_media,
            "translate_enabled": p.translate_enabled,
            "translate_target_lang": p.translate_target_lang,
            "translate_provider_id": p.translate_provider_id,
            "translate_timeout_seconds": p.translate_timeout_seconds,
        }

    def status(self):
        p = self.plugin
        route = "直连（未配置代理）"
        if p.proxy:
            try:
                parts = urlsplit(p.proxy)
                host = f"[{parts.hostname}]" if ":" in (parts.hostname or "") else parts.hostname
                route = f"代理 {parts.scheme}://{host}:{parts.port}"
            except ValueError:
                route = "已配置代理（地址无效）"
        return {"provider": {"name": p.data_provider, "ready": p._provider_ready,
                             "checking": self.checking, "error": self.error,
                             "route": route},
                "polling": {"running": p._running}}

    async def check(self):
        p = self.plugin
        self.checking, self.error = True, ""
        try:
            request = (p.twitter_api.check_fxtwitter_available()
                       if p.data_provider == "fxtwitter"
                       else p.twitter_api.check_website_available(p.website_list))
            ready = bool(await asyncio.wait_for(request, timeout=self.timeout))
            if not ready:
                self.error = "数据源检查失败，请检查代理出口或数据源地址"
            p._provider_ready = ready
            return ready
        except TimeoutError:
            self.error = f"数据源检查超时（{self.timeout:g} 秒），请检查代理出口或数据源地址"
            p._provider_ready = False
            return False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # 不把可能包含代理凭据的异常文本传到页面。
            self.error = f"数据源连接失败（{type(exc).__name__}），请检查代理和地址"
            p._provider_ready = False
            return False
        finally:
            self.checking = False

    async def save(self, payload):
        p = self.plugin
        provider = payload.get("data_provider")
        if provider not in {"nitter", "fxtwitter"}:
            raise ValueError("请选择 FxTwitter API 或 Nitter 镜像")
        fx = network_url(payload.get("fxtwitter_api_base", "https://api.fxtwitter.com"))
        nitter = network_url(payload.get("nitter_url", ""), optional=True)
        proxy = network_url(payload.get("proxy", ""), proxy=True, optional=True)
        mode = payload.get("delivery_mode", "text")
        theme = payload.get("screenshot_theme", "dark")
        media = payload.get("pre_download_media", False)
        if mode not in {"text", "card", "screenshot"} or theme not in {"dark", "light"} or type(media) is not bool:
            raise ValueError("推送形式、截图主题或媒体设置无效")
        translation = {key: payload.get(key, value) for key, value in self.settings().items()
                       if key.startswith("translate_")}
        language = translation["translate_target_lang"]
        model = translation["translate_provider_id"]
        budget = translation["translate_timeout_seconds"]
        if (type(translation["translate_enabled"]) is not bool
                or not isinstance(language, str) or not language.strip() or len(language) > 80
                or not isinstance(model, str) or len(model) > 200
                or type(budget) is not int or not 5 <= budget <= 120):
            raise ValueError("翻译设置无效：请填写目标语言，超时应为 5–120 秒")
        translation["translate_target_lang"] = language.strip()
        translation["translate_provider_id"] = model.strip()
        if model.strip() and not p.context.get_provider_by_id(model.strip()):
            raise ValueError("指定的翻译模型不可用，请检查模型 ID 或留空跟随会话")

        async with p._config_lock:
            previous = copy.deepcopy(dict(p.config))
            basic = dict(p.config.get("basic") or {})
            basic.update(twitter_data_provider=provider, twitter_fxtwitter_api_base=fx,
                         twitter_nitter_url=nitter, twitter_proxy=proxy,
                         twitter_pre_download_media=media)
            message = dict(p.config.get("message_format") or {})
            message.update(twitter_text_render_mode=mode, twitter_screenshot_theme=theme,
                           twitter_use_node=False, twitter_collective_forward=False)
            translate_config = dict(p.config.get("translation") or {})
            translate_config.update({f"twitter_{key}": value for key, value in translation.items()})
            p.config.update(basic=basic, message_format=message, translation=translate_config)
            try:
                result = p.config.save_config()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                p.config.clear()
                p.config.update(previous)
                raise

            if p._poll_task and not p._poll_task.done():
                p._poll_task.cancel()
                try:
                    await p._poll_task
                except asyncio.CancelledError:
                    pass
                except Exception as exc:
                    logger.warning(f"停止旧 Twitter 检查任务失败（{type(exc).__name__}）")
            try:
                await asyncio.wait_for(p.twitter_api.close(), timeout=5)
            except Exception as exc:
                # close 已先解绑旧 client；关闭连接失败不能中断配置应用。
                logger.warning(f"关闭旧 Twitter 连接失败（{type(exc).__name__}）")
            p.data_provider, p.proxy, p.fxtwitter_api_base = provider, proxy or None, fx
            p.custom_nitter_url, p.text_render_mode, p.screenshot_theme = nitter, mode, theme
            p.pre_download_media = media
            for key, value in translation.items():
                setattr(p, key, value)
            p.website_list = ([nitter] if nitter else []) + p._builtin_websites
            api = p.twitter_api
            api.provider, api.proxy, api.fxtwitter_api_base = provider, p.proxy, fx
            api.nitter_url, api.provider_ready = "", False
            if hasattr(api, "_status_cache"):
                api._status_cache.clear()
            p._provider_ready = False
            p.message_service.settings = replace(p.message_service.settings, proxy=p.proxy,
                text_render_mode=mode, screenshot_theme=theme, pre_download_media=media,
                **translation)
            p.polling_service.settings = replace(p.polling_service.settings, data_provider=provider,
                custom_nitter_url=nitter, website_list=tuple(p.website_list))
            self.error, self.checking = "", True
            if p._running:
                p._poll_task = asyncio.create_task(p._poll_tweets())
            else:
                self.checking = False
                self.error = "设置已保存，将在插件启动时检查"
            logger.info("Twitter 网络与推送设置已保存")
