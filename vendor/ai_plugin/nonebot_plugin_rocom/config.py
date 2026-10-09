"""洛克王国插件配置"""
from pydantic import BaseModel
import os


class RocomConfig(BaseModel):
    """洛克王国插件配置"""

    # API 配置
    api_base_url: str = os.getenv("ROCOM_API_BASE_URL", "https://wegame.shallow.ink")
    wegame_api_key: str = os.getenv("ROCOM_API_KEY", "")

    # 渲染配置
    render_timeout: int = int(os.getenv("ROCOM_RENDER_TIMEOUT", "30000"))

    # 数据目录
    data_dir: str = os.getenv("ROCOM_DATA_DIR", "data/rocom")

    # 管理员 wxid（用于刷新凭证等管理命令）
    admin_wxid: str = os.getenv("ADMIN_WXID", "fengchenhao002")

    # 帮助菜单前缀展示（仅影响图片中的命令前缀显示）
    help_prefix_display: str = os.getenv("ROCOM_HELP_PREFIX", "")

    # 远行商人订阅
    merchant_subscription_enabled: bool = os.getenv("ROCOM_MERCHANT_SUB", "1") == "1"
    merchant_subscription_items: list = None
    merchant_private_subscription_enabled: bool = True

    # 家园订阅
    home_subscription_enabled: bool = os.getenv("ROCOM_HOME_SUB", "1") == "1"
    home_subscription_interval_minutes: int = int(os.getenv("ROCOM_HOME_INTERVAL", "5"))

    def __init__(self, **data):
        super().__init__(**data)
        if self.merchant_subscription_items is None:
            self.merchant_subscription_items = ["国王球", "棱镜球", "炫彩精灵蛋"]
