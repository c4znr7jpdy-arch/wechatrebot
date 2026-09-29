import re
import base64
import urllib.parse
from typing import List, Dict
from dataclasses import dataclass

import httpx
from bs4 import BeautifulSoup
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.api import logger
from astrbot.core import AstrBotConfig
import astrbot.api.message_components as Comp

# ========== 1. 配置映射类（从配置文件读取参数） ==========
@dataclass
class MagnetConfig:
    """磁力搜索配置类"""
    base_url: str          # 站点基础地址
    search_path: str       # 搜索接口路径
    max_results: int       # 最大返回结果数
    request_timeout: int   # 请求超时时间（秒）
    captcha_cookies: Dict[str, str] = None  # 验证Cookie（固定值）

    def __post_init__(self):
        # 初始化固定验证Cookie
        self.captcha_cookies = {
            "sssfwz": "qwsdsddsdsdse"
        }
        # 处理base_url结尾的/（统一格式：不带结尾/）
        if self.base_url.endswith("/"):
            self.base_url = self.base_url.rstrip("/")
        # 处理search_path开头的/（统一格式：带开头/）
        if not self.search_path.startswith("/"):
            self.search_path = f"/{self.search_path}"

# ========== 2. 核心工具类 ==========
class MagnetUtils:
    SORT_KEYWORDS = {
        "相关度": "",
        "大小": "length",
        "文件大小": "length",
        "热门": "hot",
        "热门程度": "hot",
        "时间": "time",
        "最新": "time",
    }

    @staticmethod
    def decrypt_base64(encrypted_str: str) -> str:
        """Base64解密"""
        try:
            encrypted_str = encrypted_str.ljust(len(encrypted_str) + (4 - len(encrypted_str) % 4) % 4, '=')
            decoded = base64.b64decode(encrypted_str).decode('utf-8', errors='ignore')
            return urllib.parse.unquote(decoded)
        except Exception as e:
            logger.warning(f"Base64解密失败：{str(e)}")
            return ""

    @staticmethod
    def get_full_url(base_url: str, relative_url: str) -> str:
        """拼接完整URL"""
        if relative_url.startswith("http"):
            return relative_url
        if relative_url.startswith("./"):
            return f"{base_url}/{relative_url[2:]}"
        if relative_url.startswith("/"):
            return f"{base_url}{relative_url}"
        return f"{base_url}/{relative_url}"
    
    @staticmethod
    def get_sort_param(sort_keyword: str) -> str:
        """
        排序关键词
        """
        # 提高鲁棒性
        sort_keyword = sort_keyword.strip().lower()
        for key, value in MagnetUtils.SORT_KEYWORDS.items():
            if key.lower() == sort_keyword:
                return value
        # 匹配不到返回空
        return ""

    @staticmethod
    def is_sort_keyword(value: str) -> bool:
        normalized = value.strip().lower()
        return any(key.lower() == normalized for key in MagnetUtils.SORT_KEYWORDS)

    @staticmethod
    def decode_embedded_html(raw_html: str) -> str:
        """解码新版站点通过 window.atob 注入的搜索结果或详情 HTML。"""
        match = re.search(
            r'innerHTML\s*=\s*decodeURIComponent\(window\.atob\("([A-Za-z0-9+/=]+)"\)\)',
            raw_html,
        )
        if not match:
            return raw_html
        decoded = MagnetUtils.decrypt_base64(match.group(1))
        return decoded or raw_html

    @staticmethod
    def format_exception(exc: Exception) -> str:
        detail = str(exc).strip()
        return f"{type(exc).__name__}: {detail}" if detail else type(exc).__name__

# ========== 3. 核心搜索服务 ==========
class WhatsLinkService:
    """whatslink.info 预览服务"""

    API_URL = "https://whatslink.info/api/v1/link"

    def __init__(self, timeout: int = 15):
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def get_preview(self, magnet_url: str) -> dict | None:
        """查询磁链预览信息，失败时返回 None"""
        try:
            resp = await self.client.get(self.API_URL, params={"url": magnet_url})
            if resp.status_code != 200:
                logger.warning(f"whatslink API 返回 {resp.status_code}")
                return None
            return resp.json()
        except Exception as e:
            logger.warning(f"whatslink API 请求失败：{e}")
            return None

    async def close(self):
        await self.client.aclose()


class MagnetSearchService:
    def __init__(self, config: MagnetConfig):
        self.config = config
        self.client = None  # 全局一个client
        self._init_client()

    def _init_client(self):
        """client初始化"""
        headers = {
            "User-Agent": "Mozilla/5.0 (Linux; U; Android 2.2; en-us; Droid Build/FRG22D) AppleWebKit/533.1 (KHTML, like Gecko) Version/4.0 Mobile Safari/533.1",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Origin": self.config.base_url,
            "Referer": self.config.base_url
        }

        # 复用客户端
        self.client = httpx.AsyncClient(
            headers=headers,
            cookies=self.config.captcha_cookies,
            timeout=self.config.request_timeout,
            follow_redirects=False,
            verify=False
        )

    def _build_search_url(self, keyword: str, sort_param: str) -> str:
        if self.config.search_path.rstrip("/") == "/search":
            encoded_keyword = base64.b64encode(keyword.encode("utf-8")).decode("ascii")
            encoded_keyword = encoded_keyword.rstrip("=")
            new_sort_param = "hits" if sort_param == "hot" else sort_param or "rele"
            query = urllib.parse.urlencode(
                {"word": encoded_keyword, "sort": new_sort_param}
            )
        else:
            params = {"name": keyword}
            if sort_param:
                params["sort"] = sort_param
            query = urllib.parse.urlencode(params)
        return f"{self.config.base_url}{self.config.search_path}?{query}"

    @staticmethod
    def _extract_basic_info(li, title: str) -> tuple[str, str]:
        item_text = li.get_text(" ", strip=True)
        size_match = re.search(
            r"文件大小[：:]\s*([0-9.]+\s*[KMGTPE]?B)",
            item_text,
            re.IGNORECASE,
        )
        create_time_match = re.search(
            r"创建时间[：:]\s*(\d{4}-\d{2}-\d{2})",
            item_text,
        )
        size = size_match.group(1).strip() if size_match else "未知大小"
        create_time = (
            create_time_match.group(1).strip()
            if create_time_match
            else "未知时间"
        )
        return size, create_time

    async def search(self, keyword: str, sort_param: str = "") -> List[dict]:
        """搜索逻辑：返回包含原始数据的 dict 列表"""
        results = []

        try:
            # ========== 构造搜索URL ==========
            search_url = self._build_search_url(keyword, sort_param)
            logger.debug(f"GET请求：{search_url}")
            
            # 发起请求
            response = await self.client.get(search_url)
            response.raise_for_status()
            logger.debug(f"响应状态码：{response.status_code}")
            
            # 提取原始响应
            raw_html = response.text
            decrypted_html = MagnetUtils.decode_embedded_html(raw_html)

            # ========== 提取详情页链接（兼容新版 /information/ 和旧版 xq.php） ==========
            soup = BeautifulSoup(decrypted_html, "lxml")
            result_container = soup.find("ul", id="Search_list_wrapper")
            if not result_container:
                logger.warning(
                    "搜索响应中没有结果容器，站点页面结构可能已变化"
                )
                return []

            detail_links = []
            processed_urls = set()
            # 遍历结果：最多取配置的max_results条
            for li in result_container.find_all("li", recursive=False):
                if li.find("ul", class_="pagination"):
                    continue

                information_a = li.find(
                    "a",
                    href=re.compile(r"/information/"),
                )
                if information_a:
                    relative_url = information_a.get("href", "").strip()
                    if not relative_url or relative_url in processed_urls:
                        continue
                    processed_urls.add(relative_url)
                    title = information_a.get_text("", strip=True) or "搜索结果"
                    size, create_time = self._extract_basic_info(li, title)
                    detail_links.append({
                        "url": MagnetUtils.get_full_url(
                            self.config.base_url,
                            relative_url,
                        ),
                        "method": "GET",
                        "title": title,
                        "size": size,
                        "create_time": create_time,
                    })
                    if len(detail_links) >= self.config.max_results:
                        break
                    continue

                form_tag = li.find("form", action=re.compile(r"xq\.php"))
                if not form_tag:
                    continue
                key_input = form_tag.find("input", attrs={"name": "key"})
                if not key_input:
                    continue
                key = key_input.get("value", "").strip()
                if not key:
                    continue

                full_url = MagnetUtils.get_full_url(self.config.base_url, "/xq.php")
                
                # key去重
                if key in processed_urls:
                    continue
                processed_urls.add(key)

                # 提取基础信息
                title_a = form_tag.find("a")
                title = (
                    title_a.get_text("", strip=True)
                    if title_a
                    else f"搜索结果{len(detail_links) + 1}"
                )
                size, create_time = self._extract_basic_info(li, title)

                detail_links.append({
                    "url": full_url,
                    "method": "POST",
                    "key": key,
                    "title": title,
                    "size": size,
                    "create_time": create_time
                })
                if len(detail_links) >= self.config.max_results:
                    break

            if not detail_links:
                return []

            # ========== 解析详情页 ==========
            for link in detail_links:
                try:
                    if link["method"] == "POST":
                        detail_resp = await self.client.post(
                            link["url"],
                            data={"key": link["key"]},
                        )
                    else:
                        detail_resp = await self.client.get(link["url"])
                    detail_resp.raise_for_status()
                    detail_html = MagnetUtils.decode_embedded_html(detail_resp.text)

                    # 提取磁力链接
                    detail_soup = BeautifulSoup(detail_html, "lxml")
                    magnet_link = None
                    magnet_a = detail_soup.find("a", href=re.compile(r"magnet:\?xt=urn:btih:"))
                    if magnet_a:
                        magnet_link = magnet_a.get("href").strip()
                    if not magnet_link:
                        magnet_match = re.search(r"magnet:\?xt=urn:btih:[a-fA-F0-9]{40,}[^\"']*", detail_html)
                        if magnet_match:
                            magnet_link = magnet_match.group().strip()

                    results.append({
                        "title": link["title"],
                        "magnet_link": magnet_link,
                        "size": link["size"],
                        "create_time": link["create_time"],
                    })
                except Exception as e:
                    error_text = MagnetUtils.format_exception(e)
                    logger.warning(
                        f"详情页解析失败：{link['url']} ({error_text})"
                    )
                    results.append({
                        "title": link["title"],
                        "magnet_link": None,
                        "size": link["size"],
                        "create_time": "",
                        "error": error_text[:80],
                    })

        except Exception as e:
            error_text = MagnetUtils.format_exception(e)
            logger.exception(f"搜索异常：{error_text}")
            if isinstance(e, httpx.TimeoutException):
                user_error = "磁力搜索站点响应超时，请稍后再试"
            elif isinstance(e, httpx.RequestError):
                user_error = "磁力搜索站点连接失败，请稍后再试或更新站点地址"
            elif isinstance(e, httpx.HTTPStatusError):
                user_error = (
                    f"磁力搜索站点返回异常状态码 "
                    f"{e.response.status_code}，请稍后再试"
                )
            else:
                user_error = f"磁力搜索失败：{error_text[:80]}"
            results = [{"error": user_error}]

        return results

# ========== 4. 工具函数 ==========
def _format_size(size_bytes) -> str:
    """将字节数格式化为可读大小"""
    if not size_bytes or size_bytes <= 0:
        return "未知"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} PB"

# ========== 5. 插件主类 ==========
@register(
    "astrbot_plugin_BitTorrent",
    "NightDust981989",
    "BitTorrent磁力搜索",
    "1.3.0",
    "https://github.com/NightDust981989/astrbot_plugin_BitTorrent"
)
class MagnetSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config        
        # ========== 从插件配置文件读取参数 ==========
        magnet_config_dict = self.config.get("magnet_search", {})

        base_url = magnet_config_dict.get("base_url", "https://clg02.cc")
        search_path = magnet_config_dict.get("search_path", "/search")
        max_results = int(magnet_config_dict.get("max_results", 3))
        request_timeout = int(magnet_config_dict.get("request_timeout", 15))
        self.enable_preview = magnet_config_dict.get("enable_preview", True)

        # 初始化配置类
        self.magnet_config = MagnetConfig(
            base_url=base_url,
            search_path=search_path,
            max_results=max_results,
            request_timeout=request_timeout
        )
        self.search_service = MagnetSearchService(self.magnet_config)
        self.whatslink_service = WhatsLinkService(timeout=request_timeout)
        logger.info(f"磁力搜索插件初始化完成，使用站点：{base_url}{search_path}")

    async def terminate(self):
        await self.search_service.client.aclose()
        await self.whatslink_service.close()

    @staticmethod
    def _format_result(idx: int, res: dict) -> str:
        """格式化单条搜索结果"""
        if "error" in res and not res.get("magnet_link"):
            title = res.get("title", "未知")
            size = res.get("size", "未知")
            return f"‎\n===== 结果 {idx} =====\n‎标题：{title}\n解析失败：{res['error']}\n文件大小：{size}"

        return (
            f"‎\n===== 结果 {idx} =====\n‎"
            f"标题：{res['title']}\n"
            f"磁力链接：{res['magnet_link'] or '未提取到'}\n"
            f"文件大小：{res['size']}\n"
            f"收录时间：{res['create_time']}"
        )

    @filter.command("bt")
    async def magnet_search_handler(self, event: AstrMessageEvent):
        """
        磁力搜索指令
        使用方式：bt （排序方式） [关键词]
        示例：bt 安达与岛村 / bt 热门 安达与岛村
        """
        # 命令由本插件独占处理，避免失败后继续落入普通 AI 对话链。
        event.stop_event()
        message = event.message_str.strip().lstrip("/\\").strip()
        args = message.split()

        chain = []

        if len(args) < 2 or args[0] != "bt":
            chain.append(Comp.Plain("用法：/bt （排序方式） [关键词]\n示例：/bt 热门 安达与岛村"))
            yield event.chain_result(chain)
            return

        # 解析排序参数和关键词
        sort_keyword = ""
        if len(args) > 2 and MagnetUtils.is_sort_keyword(args[1]):
            sort_keyword = args[1]
            keyword = " ".join(args[2:])
        else:
            keyword = " ".join(args[1:])

        sort_param = MagnetUtils.get_sort_param(sort_keyword)
        results = await self.search_service.search(keyword, sort_param)

        if not results:
            chain.append(Comp.Plain("未找到相关磁力链接，网站失效或网络问题"))
            yield event.chain_result(chain)
            return
        elif len(results) == 1 and results[0].get("error") and not results[0].get("title"):
            # 搜索整体失败
            chain.append(Comp.Plain(results[0]["error"]))
            yield event.chain_result(chain)
            return
        else:
            # 第一次发送：所有基础结果 + 预览文本
            text_chain = [Comp.Plain(f"共找到 {len(results)} 条有效结果：")]
            preview_nodes = []
            for idx, res in enumerate(results, 1):
                text_chain.append(Comp.Plain(self._format_result(idx, res)))
                preview = None
                if self.enable_preview and res.get("magnet_link"):
                    preview = await self.whatslink_service.get_preview(res["magnet_link"])
                if preview:
                    text_chain.append(Comp.Plain(
                        f"‎\n--- 预览 ---\n‎"
                        f"类型：{preview.get('file_type') or '未知'}\n"
                        f"文件数：{preview.get('count') or '未知'}"
                    ))
                    # 收集截图到 Node
                    if preview.get("screenshots"):
                        screenshot_url = preview["screenshots"][0].get("screenshot")
                        if screenshot_url:
                            node_content = [Comp.Plain(f"结果{idx}预览图"), Comp.Image.fromURL(screenshot_url)]
                        else:
                            node_content = [Comp.Plain(f"结果{idx}无预览图")]
                    else:
                        node_content = [Comp.Plain(f"结果{idx}无预览图")]
                else:
                    node_content = [Comp.Plain(f"结果{idx}无预览图")]
                preview_nodes.append(Comp.Node(
                    content=node_content,
                    name=event.get_sender_name(),
                    uin=str(event.get_sender_id()),
                ))
            yield event.chain_result(text_chain)

            # 第二次发送：预览图合并转发
            if self.enable_preview and preview_nodes:
                yield event.chain_result([Comp.Nodes(nodes=preview_nodes)])

    @filter.command("btp")
    async def magnet_preview_handler(self, event: AstrMessageEvent):
        """
        磁链预览指令
        使用方式：btp [磁力链接]
        示例：btp magnet:?xt=urn:btih:xxxx
        """
        event.stop_event()
        message = event.message_str.strip().lstrip("/\\").strip()
        args = message.split(maxsplit=1)

        chain = []

        if len(args) < 2:
            chain.append(Comp.Plain("用法：/btp [磁力链接]\n示例：/btp magnet:?xt=urn:btih:xxxx"))
            yield event.chain_result(chain)
            return

        magnet_url = args[1].strip()
        if not magnet_url.startswith("magnet:"):
            chain.append(Comp.Plain("请输入有效的磁力链接（以 magnet: 开头）"))
            yield event.chain_result(chain)
            return

        preview = await self.whatslink_service.get_preview(magnet_url)
        if not preview or not preview.get("name"):
            chain.append(Comp.Plain("未查询到预览信息，链接可能无效或 API 暂不可用"))
            yield event.chain_result(chain)
            return

        text = (
            f"文件名：{preview.get('name', '未知')}\n‎"
            f"类型：{preview.get('file_type') or '未知'}\n"
            f"总大小：{_format_size(preview.get('size', 0))}\n"
            f"文件数：{preview.get('count') or '未知'}"
        )
        chain.append(Comp.Plain(text))

        if preview.get("screenshots"):
            screenshot_url = preview["screenshots"][0].get("screenshot")
            if screenshot_url:
                chain.append(Comp.Image.fromURL(screenshot_url))

        yield event.chain_result(chain)
