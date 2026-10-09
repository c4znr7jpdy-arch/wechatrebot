"""运行时状态 — 在收到 main.py 登录事件后动态设置"""

bot_wxid: str = ""
bot_nickname: str = ""

def set_identity(wxid: str, nickname: str = ""):
    global bot_wxid, bot_nickname
    bot_wxid = wxid
    bot_nickname = nickname
