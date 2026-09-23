"""Customer help and curated common fields; never contains customer values."""
from __future__ import annotations

import copy
from typing import Any


# Paths use the delivered upstream schema. Omitted settings remain in Advanced.
_GUIDES = {
    "astrbot_plugin_angel_heart": ("协调群聊接话和多人上下文", "先到群聊&私聊管理选择群聊和回复方式；需要调整上下文长度时再打开专属配置页。", "它不会建立独立长期记忆库；会话上下文仍受回复引擎的上下文范围限制。", ()),
    "astrbot_plugin_bittorrent": ("按关键词查找资源索引", "先填写可用的资源索引站地址，再调整返回数量。", "只检索有权访问和使用的内容；索引站需要可访问。", ("magnet_search.base_url", "magnet_search.max_results", "magnet_search.enable_preview")),
    "astrbot_plugin_livingmemory": ("记住重要对话并在需要时回忆", "先在模型设置中选择 Embedding 文本向量模型和聊天模型，再保持智能召回。", "需要单独配置 Embedding 模型；Grok 等聊天模型不能替代。明确回忆会检索，普通问候默认跳过。", ("provider_settings.embedding_provider_id", "provider_settings.llm_provider_id", "recall_engine.auto_recall_mode", "recall_engine.top_k", "reflection_engine.summary_trigger_rounds")),
    "astrbot_plugin_mimo_tts_clone": ("把文字读成指定声音", "打开专属配置页填写语音服务密钥，管理授权音色、使用范围和语音参数；保存后查看页面反馈并在测试聊天中验证。", "需要 MiMo 语音服务和已授权的参考音频；自动语音会产生服务用量。", ()),
    "astrbot_plugin_music": ("在聊天中点歌", "群聊直接发送“点歌 七里香”，无需点名机器人。", "点歌属于对当前消息的回复，不受主动推送开关影响。音乐能否播放取决于平台和歌曲授权。", ()),
    "astrbot_plugin_parser": ("解析聊天中的抖音等分享链接", "启用后直接发送分享链接，使用内置推荐配置即可。", "部分平台可能受登录状态或网络环境影响；普通使用无需填写参数。", ()),
    "astrbot_plugin_twitter": ("订阅 X/Twitter 推主并推送新推文", "先选择 FxTwitter 或 Nitter 数据源并配置网络访问，再在群聊或私聊中使用“/推特关注 用户名”建立订阅；需要管理群级订阅时打开专属订阅管理页。", "推送依赖可用的数据源和网络代理；这是按会话保存的订阅，不会自动把所有推主消息发送到全部聊天。", ("basic.twitter_data_provider", "basic.twitter_fxtwitter_api_base", "basic.twitter_proxy", "basic.twitter_poll_interval", "basic.twitter_poll_max_tweets_per_user", "content_filter.twitter_link_recognition_enabled")),
    "astrbot_plugin_patch_at_wxid": ("识别微信群里的点名", "保持启用即可，无需填写参数。", "这是微信兼容功能；关闭后可能影响群聊点名识别。", ()),
    "astrbot_plugin_proactive_chat": ("按指定范围主动聊天", "只填写允许主动联系的会话，再打开对应的群聊或私聊开关。", "主动消息会发送给真实联系人；先在测试会话试用，避免打扰。", ("friend_settings.enable", "friend_settings.session_list", "group_settings.enable", "group_settings.session_list", "group_settings.group_idle_trigger_minutes")),
    "astrbot_plugin_qq_group_daily_analysis": ("把一天群聊整理成日报", "从管理器的群聊列表选择自动日报群，不需要在群里发送命令。", "使用当前机器人账号成功接收后保存在本机的群消息；安装前或连接离线期间的消息无法补拉。生成报告还会调用回复模型。", ()),
    "astrbot_plugin_twitter": ("订阅 X（Twitter）账号并推送新推文", "先选择 FxTwitter 数据源并确认网络可访问，再打开专属订阅页为群聊或私聊添加推主和推送开关。", "需要可访问的 Twitter/FxTwitter 数据源；开启翻译时还需要可用的模型服务。订阅按会话隔离，不会自动向所有聊天发送。", ("basic.twitter_data_provider", "basic.twitter_fxtwitter_api_base", "basic.twitter_proxy", "basic.twitter_poll_interval", "basic.twitter_poll_max_tweets_per_user", "content_filter.twitter_link_recognition_enabled", "translation.twitter_translate_enabled", "translation.twitter_translate_target_lang", "translation.twitter_translate_provider_id")),
    "astrbot_plugin_self_iterative_core": ("限制自动优化的使用范围", "先开启白名单，只添加可信任的管理员。", "进阶维护能力可能修改功能代码；普通用户不建议关闭权限限制。", ("enable_whitelist", "whitelist_users", "verbose_steps")),
    "astrbot_plugin_self_learning": ("让回复带有稳定的情绪和关系感", "默认使用精简情感模式；需要模仿群聊风格时再单独开启学习并选择范围。", "情感功能不等于长期记忆。开启消息学习会保存群聊样本，必须先明确范围和征得群成员同意。", ("Affection_System_Settings.enable_affection_system", "Mood_System_Settings.enable_daily_mood", "Social_Context_Settings.enable_social_context_injection", "Runtime_Internal_Settings.llm_hook_profile", "Self_Learning_Basic.enable_message_capture")),
    "jiang_capability_router": ("让自然语言找到合适的功能", "一般保持当前设置即可；可调整“再来一个”这类续接请求的有效时间。", "具体能力需要对应功能已启用且完成配置。", ("recent_action_ttl_seconds", "inject_capability_prompt", "block_unknown_slash_llm")),
    "jiang_commands": ("查询天气、新闻等信息", "群里可以询问天气、新闻、油价或 Epic 免费游戏；具体指令见下方列表。安排天气推送时，再到定时任务创建天气内容并选择群聊。", "所选群聊须在群聊&私聊管理允许“常用指令”和“接收已配置的定时任务”；两个开关不能互相替代。", ()),
    "jiang_commands.setu": ("发送一张随机图片", "在聊天中发送“图来”；需要自动撤回时打开开关并填写等待秒数。", "在群聊管理中独立勾选“图来”；“常用指令”不会代替此开关。自动撤回依赖微信返回可撤回的消息标识。", ("setu_recall_enabled", "setu_recall_seconds")),
    "jiang_group_notice": ("提示入群、退群和群昵称变化", "分别选择要提示的成员事件，并按场景修改加入、退出和群昵称变化文案。", "这不是微信群公告编辑器；仅在机器人收到成员变动事件时发送，不会补发历史通知。", ("notify_join", "notify_leave", "notify_profile_change", "welcome_title", "welcome_tail", "leave_title", "leave_body", "profile_change_title", "profile_change_body")),
    "jiang_image": ("用文字或参考图生成图片", "先到“设置 → 模型服务”添加图片模型，再在这里选择主模型；确有需要时可启用一个备用模型。服务地址、模型名称和密钥只在模型服务中管理。", "需要支持生图的图片模型服务；聊天模型不能自动替代生图模型。", ("primary_model", "secondary_enabled", "secondary_model", "use_system_proxy", "local_proxy_port")),
    "jiang_mimo_emoji_voice": ("让表情回复带上语音", "先配置语音克隆和表情管理，再启用表情语音。", "依赖语音服务和可用的参考声音；语音生成可能产生费用。", ("enabled", "min_text_chars", "max_text_chars", "use_style_director")),
    "jiang_mystool": ("使用米游社签到", "在私聊中按提示绑定米游社账号，再启用需要的签到任务。", "需要你自己的米游社账号授权；不要在群聊公开验证码或登录凭据。", ()),
    "jiang_repeater": ("参与群里的复读互动", "启用后，在群聊出现连续相同消息时进行互动。", "无需额外配置；不想让机器人参与时可关闭。", ()),
    "jiang_rocom": ("使用洛克王国游戏助手", "按插件提示通过二维码或已有凭据绑定游戏账号，再使用角色、阵容、交换或家园能力。", "这是特定游戏工具，不是运势功能；账号凭据不要发到群里。", ()),
    "jiang_schedule": ("安排内容推送和签到", "在定时任务选择内容、聊天和时间；天气等内容还需在群聊&私聊管理允许对应功能，再打开该聊天的定时接收开关。旧版计划留在兼容区。", "功能中心启用不等于向全部群发送；没有用户创建并启用的任务时不会推送。旧式群聊写操作仍要求可靠管理员身份。", ()),
    "jiang_short_drama": ("搜索内容并安排推送", "无需专属配置页；在定时任务中选择影视推荐、分类、数量和发送聊天。播放卡片统一打开托管中间页，无需在本机启动服务。", "仅访问有权使用的内容；播放依赖内容源与托管播放页可用。", ()),
    "jiang_video_understanding": ("总结微信短视频", "发送视频后使用 /总结视频；先保留当前抽帧数量。", "需要能理解图片的模型；音频不可用时会退回画面分析。", ("max_duration_seconds", "max_file_size_mb", "frame_count", "vision_provider_id", "audio_provider_id", "semantic_fallback_provider_id")),
    "meme_manager": ("管理聊天使用的表情包", "打开专属配置页导入表情包，再到群聊&私聊管理为需要的群勾选“表情管理”。人格只需正常填写，系统会在允许表情的群聊中自动加入隐藏的情绪标签规则。", "只导入有权使用的表情；云端同步需要相应图床或存储账号。", ("storage.provider", "sync.status_cache_ttl_seconds")),
}


# Curated from the plugin's HELP_DATA. Keep this customer-facing list stable
# even when the plugin source contains internal or administrator-only helpers.
_COMMANDS = {
    "jiang_commands.setu": (
        {
            "title": "随机图片",
            "items": (("图来", "发送一张随机图片，并按设置决定是否自动撤回"),),
        },
    ),
    "jiang_commands": (
        {
            "title": "常用查询",
            "items": (
                ("/新闻", "今日热点新闻"),
                ("/城市天气", "例如：/绵阳天气、/青岛天气902"),
                ("/油价省份", "例如：/油价四川"),
                ("/epic", "Epic 喜加一"),
                ("/kfc", "疯狂星期四文案"),
                ("/理想", "发送 20 秒《理想进行曲》语音条"),
            ),
        },
        {
            "title": "AI 互动",
            "items": (
                ("@机器人 帮我画 xxx", "文生图"),
                ("@机器人 P图/改图/图生图", "编辑图片（需引用图片）"),
            ),
        },
        {
            "title": "趣味与游戏",
            "items": (
                ("/占卜", "抽四张塔罗牌占卜"),
                ("/塔罗牌", "抽一张塔罗牌"),
                ("/洛克", "查看洛克王国全部指令"),
                ("/洛克档案 /战绩 /背包", "查询角色数据"),
                ("/洛克阵容 /查蛋 /配种", "阵容与宠物"),
                ("/洛克交换大厅 /远行商人", "交易与商店"),
                ("/订阅远行商人 /家园菜园", "订阅通知"),
            ),
        },
        {
            "title": "其他",
            "items": (
                ("发送链接", "抖音/TikTok/B站链接自动解析"),
                ("/米游社帮助", "查看米游社相关功能"),
            ),
        },
    ),
    "jiang_mystool": (
        {
            "title": "登录与任务",
            "items": (
                ("/米游社帮助", "查看完整命令说明"),
                ("/登录", "私聊扫码绑定米游社账号"),
                ("/登录状态", "查看扫码处理进度"),
                ("/签到", "执行游戏签到"),
                ("/任务", "执行米游币任务"),
            ),
        },
        {
            "title": "账户与游戏数据",
            "items": (
                ("/地址 [账户ID]", "查看收货地址"),
                ("/商品 [游戏]", "查看米游社商品"),
                ("/兑换 +/- 商品ID", "管理兑换计划"),
                ("/原神便签", "查看原神便签"),
                ("/星穹铁道便签", "查看星铁便签"),
            ),
        },
        {
            "title": "账号设置",
            "items": (
                ("/设置", "查看米游社设置用法"),
                ("/账号设置 [账户ID] …", "设置平台、签到和米游币任务"),
                ("/通知设置 开启/关闭", "开关任务通知"),
                ("/导出Cookies", "仅私聊导出账号凭据"),
            ),
        },
    ),
    "jiang_rocom": (
        {
            "title": "登录与绑定",
            "items": (
                ("/洛克", "查看完整命令说明"),
                ("/洛克QQ登录", "扫码绑定 QQ 账号"),
                ("/洛克微信登录", "扫码绑定微信账号"),
                ("/洛克导入 <ID> <Ticket>", "通过凭证导入账号"),
                ("/洛克绑定列表", "查看已绑定账号"),
                ("/洛克切换 <序号>", "切换主账号"),
                ("/洛克解绑 <序号>", "解绑账号"),
                ("/洛克刷新", "刷新当前凭证"),
                ("/洛克刷新所有凭证", "管理员刷新全部凭证"),
                ("/洛克删除无效绑定", "管理员清理无效绑定"),
            ),
        },
        {
            "title": "角色与查询",
            "items": (
                ("/洛克档案", "查看角色档案"),
                ("/洛克战绩 [页码]", "查看战绩"),
                ("/洛克背包 [页码]", "查看精灵收集"),
                ("/洛克阵容", "查看当前阵容"),
                ("/查看阵容 <序号>", "查看阵容详情"),
                ("/洛克交换大厅 [页码]", "查看交换信息"),
                ("/远行商人", "查看远行商人"),
                ("/洛克玩家 <UID>", "查询玩家"),
                ("/洛克家园 [UID]", "查询家园"),
                ("/洛克商店 [商店ID]", "查询游戏商店"),
                ("/洛克好友关系 <id1,id2>", "查询好友关系"),
                ("/洛克学生", "查询学生信息"),
            ),
        },
        {
            "title": "宠物与百科",
            "items": (
                ("/洛克查蛋 [关键词]", "查询宠物蛋"),
                ("/洛克配种 <精灵A> <精灵B>", "查询配种结果"),
                ("/洛克wiki [名称]", "查询精灵 Wiki"),
                ("/洛克技能 [名称]", "查询技能 Wiki"),
            ),
        },
        {
            "title": "订阅通知",
            "items": (
                ("/订阅远行商人", "订阅远行商人通知"),
                ("/取消订阅远行商人", "取消远行商人通知"),
                ("/订阅家园菜园 [UID]", "订阅家园菜园通知"),
                ("/订阅家园灵感 [UID]", "订阅家园灵感通知"),
                ("/取消订阅家园", "取消家园订阅"),
            ),
        },
    ),
}


# audience, readiness, recommendation.  This is product guidance only: it never
# toggles, deletes, or rewrites a customer's existing plugin configuration.
_ASSESSMENTS = {
    "astrbot_plugin_angel_heart": ("群聊常用", "可用但已纠正名称", "保留。适合多人群聊的接话与上下文协调，但不应宣传为长期记忆。"),
    "astrbot_plugin_bittorrent": ("群聊常用", "默认开启", "可直接使用 /bt 加关键词检索；结果来自外部索引，请确认内容授权并遵守当地规则。"),
    "astrbot_plugin_livingmemory": ("对话基础", "需要 Embedding 模型", "保留为唯一主长期记忆；普通寒暄跳过，明确回忆请求必须尝试检索。"),
    "astrbot_plugin_mimo_tts_clone": ("进阶选配", "需要外部服务", "只有确实需要授权音色和语音回复时使用；默认文字回复更省成本。"),
    "astrbot_plugin_music": ("群聊常用", "可直接试用", "保留为低门槛娱乐能力；播放结果仍取决于在线音源与授权。"),
    "astrbot_plugin_parser": ("群聊常用", "可直接试用", "建议保留。群内分享链接频繁时价值高，并建议默认要求点名机器人。"),
    "astrbot_plugin_twitter": ("兴趣选配", "需要数据源和网络访问", "只为明确需要的群或好友建立订阅；先用 FxTwitter 测试网络，再逐个订阅推主，避免不必要的轮询请求。"),
    "astrbot_plugin_patch_at_wxid": ("底层支持", "无需用户配置", "必须保留并在普通功能列表弱化展示；它负责微信群点名兼容。"),
    "astrbot_plugin_proactive_chat": ("进阶选配", "会主动发送消息", "全局启用不等于向所有群发言；默认群主动聊天关闭，只为明确选择的群开启，并先在测试群验证频率。"),
    "astrbot_plugin_qq_group_daily_analysis": ("群管理常用", "已适配本机消息记录", "建议保留并按需启用；只分析当前机器人账号成功接收后的本机群消息，首次使用需先积累记录。"),
    "astrbot_plugin_twitter": ("群聊常用", "需要外部数据源", "建议按需启用。先在专属订阅页只添加一个测试会话和推主，再逐步扩大推送范围；遵守 X 内容访问和转载规则。"),
    "astrbot_plugin_self_iterative_core": ("不建议默认启用", "实验性高风险", "不属于群主管理刚需；可写入并重载功能代码，应从小白默认界面和新安装默认项移除。"),
    "astrbot_plugin_self_learning": ("人格与情感基础", "默认隐私模式", "保留好感度、每日情绪和精简上下文；原始消息抓取、自动学习和人格覆盖默认关闭。"),
    "jiang_capability_router": ("底层支持", "无需用户配置", "建议保留并弱化展示；它负责把自然语言请求交给合适功能。"),
    "jiang_commands": ("群聊常用", "已封装，可直接使用", "建议保留。普通用户直接使用常用查询，不再配置内部服务编号和密钥。"),
    "jiang_commands.setu": ("群聊常用", "可配置撤回", "按需使用；默认 20 秒后撤回，也可以关闭自动撤回。"),
    "jiang_group_notice": ("群管理常用", "收到成员事件即可用", "建议保留，但名称必须说明是成员变动通知，不是群公告发布。"),
    "jiang_image": ("场景选配", "需要生图服务", "群里确有生图需求时保留；聊天模型不能自动替代生图模型。"),
    "jiang_mimo_emoji_voice": ("进阶选配", "依赖语音克隆和表情管理", "仅在两个依赖都可用时开启，避免失败链路和额外费用。"),
    "jiang_mystool": ("兴趣选配", "需要用户私聊绑定", "用于米游社账号签到；绑定凭据必须留在私聊。"),
    "jiang_repeater": ("群聊常用", "可直接试用", "作为基础互动能力按需开启；工作群可关闭以免打扰。"),
    "jiang_rocom": ("兴趣选配", "需要游戏账号绑定", "仅洛克王国用户群有必要；应使用准确名称并把绑定放在私聊。"),
    "jiang_schedule": ("群管理常用", "权限已加固", "建议保留；写操作已按当前会话和可靠管理员身份隔离，共享账号签到仅限机器人管理员。"),
    "jiang_short_drama": ("场景选配", "需要内容源", "仅内容分享群有必要；先确认内容来源、授权和播放器可用。"),
    "jiang_video_understanding": ("群聊常用", "需要视觉模型", "群内短视频较多时建议保留；未配置视觉模型时不要承诺完整理解。"),
    "meme_manager": ("群聊常用", "可本地使用", "建议保留。小白只需管理表情包和会话绑定，云同步保持选配。"),
}

_HIDE_ALL_SETTINGS = frozenset(
    {
        "jiang_commands",
        "jiang_short_drama",
        "astrbot_plugin_parser",
        "astrbot_plugin_mimo_tts_clone",
        "astrbot_plugin_twitter",
    }
)
_HIDE_ADVANCED_SETTINGS = frozenset({"jiang_image"})


def get_plugin_guide(module_name: str) -> dict[str, Any]:
    row = _GUIDES.get(module_name)
    if row is None:
        return {}
    summary, step, prerequisite, paths = row
    guide = {
        "summary": summary,
        "setup_steps": (step, "保存后查看生效状态；需要账号或外部服务时，再在测试会话验证一次。"),
        "prerequisites": (prerequisite,),
        "common_fields": paths,
    }
    if module_name in _COMMANDS:
        guide["commands"] = copy.deepcopy(_COMMANDS[module_name])
    assessment = _ASSESSMENTS.get(module_name)
    if assessment:
        guide.update({
            "audience": assessment[0],
            "readiness": assessment[1],
            "recommendation": assessment[2],
        })
    return guide


def common_schema(module_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Project by known paths without changing schema values or defaults."""
    if module_name in _HIDE_ALL_SETTINGS:
        return {}
    paths = get_plugin_guide(module_name).get("common_fields", ())
    if not paths:
        return schema
    def select(items: dict[str, Any], prefix: str = "") -> dict[str, Any]:
        result = {}
        for key, field in items.items():
            path = prefix + key
            if not isinstance(field, dict) or field.get("invisible") is True:
                continue
            if path in paths:
                result[key] = copy.deepcopy(field)
            elif field.get("type") == "object" and isinstance(field.get("items"), dict):
                children = select(field["items"], path + ".")
                if children:
                    result[key] = {**copy.deepcopy(field), "items": children}
        return result
    projected = select(schema) or schema
    if module_name == "jiang_image":
        groups = (
            ("主生图模型", ("primary_model",)),
            ("备用生图模型", ("secondary_enabled", "secondary_model")),
            ("本地代理", ("use_system_proxy", "local_proxy_port")),
        )
        ordered = {}
        for title, keys in groups:
            for key in keys:
                if key not in projected:
                    continue
                field = projected[key]
                field["_ui_group"] = title
                if key.endswith("_enabled") or key == "use_system_proxy":
                    field["_ui_full"] = True
                if key == "secondary_enabled":
                    field["description"] = "使用备用生图模型"
                    field["hint"] = "主模型不可用时才会尝试；备用服务从已配置的图片模型中选择。"
                    field["_ui_disclosure"] = "添加备用模型"
                if key == "primary_model":
                    field["_blank_label"] = "请选择已配置的图片模型"
                    field["hint"] = "接口地址、模型名称和密钥统一在“设置 → 模型服务”管理。"
                if key == "secondary_model":
                    field["_blank_label"] = "请选择备用图片模型"
                    field["hint"] = "备用模型需先在“设置 → 模型服务”中添加。"
                if key.startswith("secondary_") and key != "secondary_enabled":
                    field["_ui_depends_on"] = "secondary_enabled"
                if key == "use_system_proxy":
                    field["description"] = "使用本地代理"
                    field["hint"] = "关闭时直连；开启后可填写本机代理端口。端口留空时使用系统已有代理设置，未配置代理时仍直连。"
                if key == "local_proxy_port":
                    field["description"] = "本地代理端口（可留空）"
                    field["hint"] = "例如 7890。仅连接本机 127.0.0.1；留空时使用系统已有代理设置。"
                    field["_ui_control"] = "proxy-port"
                    field["_ui_depends_on"] = "use_system_proxy"
                    field["min"] = 1
                    field["max"] = 65535
                ordered[key] = field
        return ordered
    return projected


def advanced_schema(module_name: str, schema: dict[str, Any]) -> dict[str, Any]:
    """Hide发行方维护的内部参数 from every customer-facing settings tab."""
    if module_name in _HIDE_ALL_SETTINGS or module_name in _HIDE_ADVANCED_SETTINGS:
        return {}
    return schema
