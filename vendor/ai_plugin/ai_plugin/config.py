from pydantic import BaseModel
from pathlib import Path
import os

class Config(BaseModel):
    """AI Plugin 配置"""
    # 路径配置
    lib_path: Path = Path(__file__).parent.parent.parent / "lib"

    # API 配置
    minimax_api_key: str = os.getenv("MINIMAX_API_KEY", "")
    minimax_api_base: str = os.getenv("MINIMAX_API_BASE_URL", "https://api.minimaxi.com/v1")
    minimax_api_model: str = os.getenv("MINIMAX_API_MODEL", "MiniMax-M2.7-highspeed")

    fallback_api_key: str = os.getenv("FALLBACK_API_KEY", "")
    fallback_api_base: str = os.getenv("FALLBACK_API_BASE_URL", "https://freeapi.dgbmc.top/v1")
    fallback_api_model: str = os.getenv("FALLBACK_API_MODEL", "grok-4.20-0309")

    # DeepSeek
    deepseek_api_key: str = os.getenv("DS_API_KEY", "")
    deepseek_api_base: str = os.getenv("DS_API_BASE_URL", "https://api.deepseek.com/v1")
    deepseek_api_model: str = os.getenv("DS_API_MODEL", "deepseek-v4-flash")

    # 管理员（支持逗号分隔多个 wxid）
    admin_wxid: str = os.getenv("ADMIN_WXID", "fengchenhao002")

    def is_admin(self, wxid: str) -> bool:
        return wxid in [x.strip() for x in self.admin_wxid.split(",")]

    # 运行时配置
    self_user_id: str = os.getenv("BOT_WXID", "")  # 启动后由 meta 事件动态设置

    # 数据路径
    data_dir: str = "data"

    # 功能开关
    enable_stealth_mode: bool = True  # 群聊潜伏模式
    enable_private_reply: bool = True  # 私聊回复
