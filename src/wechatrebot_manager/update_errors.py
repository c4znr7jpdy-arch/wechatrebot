from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class UpdateErrorDefinition:
    code: str
    title: str
    message: str


UPDATE_ERRORS = {
    "UPD-1001": UpdateErrorDefinition("UPD-1001", "检查更新失败", "暂时无法获取更新信息，请稍后重试。"),
    "UPD-1002": UpdateErrorDefinition("UPD-1002", "下载失败", "更新文件下载失败，请检查网络后重试。"),
    "UPD-1003": UpdateErrorDefinition("UPD-1003", "文件校验失败", "更新文件校验失败，请重新下载。"),
    "UPD-1004": UpdateErrorDefinition("UPD-1004", "磁盘空间不足", "请释放磁盘空间后重试。"),
    "UPD-1005": UpdateErrorDefinition("UPD-1005", "无法安全退出", "请先关闭微信，再重试更新。"),
    "UPD-1006": UpdateErrorDefinition("UPD-1006", "微信连接无法停止", "微信连接未能安全停止，本次更新已取消。"),
    "UPD-1007": UpdateErrorDefinition("UPD-1007", "更新文件替换失败", "更新未能完成，正在恢复原版本。"),
    "UPD-1008": UpdateErrorDefinition("UPD-1008", "权限不足", "当前安装目录无法写入，请检查目录权限。"),
    "UPD-1009": UpdateErrorDefinition("UPD-1009", "新版本启动失败", "新版本未能正常启动，正在恢复原版本。"),
    "UPD-1010": UpdateErrorDefinition("UPD-1010", "自动恢复失败", "自动恢复未能完成，请联系技术支持。"),
    "UPD-1011": UpdateErrorDefinition("UPD-1011", "回复引擎无法停止", "回复引擎未能安全停止，本次更新已取消。"),
    "UPD-1012": UpdateErrorDefinition("UPD-1012", "更新包解压失败", "更新包无法展开，请重新下载。"),
    "UPD-1013": UpdateErrorDefinition("UPD-1013", "更新配置错误", "更新信息格式异常，请联系技术支持。"),
    "UPD-1014": UpdateErrorDefinition("UPD-1014", "等待微信退出超时", "微信仍在运行，请退出微信后重试。"),
    "UPD-1015": UpdateErrorDefinition("UPD-1015", "更新目录不可写", "更新目录无法写入，请检查磁盘和目录权限。"),
    "UPD-1016": UpdateErrorDefinition("UPD-1016", "需要完成更新", "当前版本已停止支持，请完成更新后再启动机器人。"),
}


class UpdateFailure(RuntimeError):
    def __init__(
        self,
        code: str,
        detail: str = "",
        *,
        public_message: str = "",
    ) -> None:
        self.definition = UPDATE_ERRORS.get(code, UPDATE_ERRORS["UPD-1007"])
        self.detail = detail
        self.public_message = public_message
        super().__init__(self.definition.message)

    def to_public_dict(self) -> dict[str, str]:
        return {
            "code": self.definition.code,
            "title": self.definition.title,
            "message": self.public_message or self.definition.message,
        }


__all__ = ["UPDATE_ERRORS", "UpdateErrorDefinition", "UpdateFailure"]
