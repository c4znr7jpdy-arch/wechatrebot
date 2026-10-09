"""KFC疯狂星期四文案生成器"""
import re
import httpx

_API_URLS = [
    ("https://oiapi.net/api/KFC", "message"),
    ("https://api.pearapi.ai/api/kfc", "text"),
]

# KFC意图匹配模式
_KFC_PATTERNS = [
    r"(?:kfc|KFC|肯德基|疯狂星期四|疯四)",
    r"(?:今天.*星期四|星期四.*文案)",
    r"(?:v我50|V我50|转我50)",
]


def detect_kfc_intent(text: str) -> bool:
    """检测是否包含KFC疯狂星期四意图"""
    for pattern in _KFC_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            return True
    return False


async def fetch_kfc_text() -> str:
    """获取KFC疯狂星期四文案，多接口轮询"""
    async with httpx.AsyncClient(timeout=15.0) as client:
        for url, field in _API_URLS:
            try:
                resp = await client.get(url)
                resp.raise_for_status()
                if not resp.text.strip():
                    continue
                data = resp.json()
                text = data.get(field, "")
                if text:
                    return text
            except Exception:
                continue
    return "V我50！"
