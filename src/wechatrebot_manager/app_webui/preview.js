/* Preview controller; the shell supplies shared services explicitly. */
window.createPreviewApi = function (helpers) {
  "use strict";
  const {
    $,
    HISTORY_PAGE_SIZE,
    SCHEDULED_ACTIONS,
    api,
    inferProviderModalities,
    providerCapabilityLabel,
    providerThinkingSupported,
    scheduledTimeText,
    state,
  } = helpers;

  function previewApi() {
    const status = {
      state: "RUNNING", title: "机器人可用", message: "微信连接和全部功能运行正常。", busy: false, online: true, can_start: false, can_stop: true, error: null,
      environment: {ready: true, code: "10000", title: "微信环境正常", message: "已检测到兼容微信。", solutions: [], path: "C:\\Program Files\\Tencent\\Weixin\\Weixin.exe", version: "4.1.8.27", process_running: true},
      runtime: {engine_ready: true, connection_ready: true, wechat_identity: "wxid_demo_account", hook_ready: true, upstream_connected: true, heartbeat_fresh: true},
    };
    const previewAudience = {
      "feature-memory": "群聊常用", "feature-long-memory": "群聊常用", "feature-bittorrent": "群聊常用",
      "feature-voice-clone": "进阶选配", "feature-memory-manager": "进阶选配",
      "feature-music": "群聊常用", "feature-parser": "群聊常用", "feature-twitter": "兴趣选配",
      "feature-proactive": "进阶选配", "feature-group-analysis": "群管理常用", "feature-self-iterative": "不建议默认启用",
      "feature-router": "底层支持", "feature-commands": "群聊常用", "feature-group-notice": "群管理常用",
      "feature-image": "场景选配", "feature-emoji-voice": "进阶选配",
      "feature-mystool": "兴趣选配", "feature-repeater": "群聊常用",
      "feature-rocom": "兴趣选配", "feature-schedule": "群管理常用",
      "feature-drama": "场景选配", "feature-video": "群聊常用", "feature-meme": "群聊常用",
    };
    const previewFeature = (id, displayName, configuration, description, options = {}) => ({
      id,
      display_name: displayName,
      enabled: true,
      configurable: configuration.includes("schema"),
      field_count: configuration.includes("schema") ? (options.fieldCount || 4) : 0,
      model_field_count: options.modelFieldCount || 0,
      pages: configuration.includes("page") ? [options.page || "plugin-settings"] : [],
      configuration,
      description,
      audience: previewAudience[id] || "按需使用",
      scheduled_action: options.scheduledAction || "",
      schedule_provider: options.scheduleProvider === true,
      ...options,
    });
    const features = [
      previewFeature("feature-long-memory", "长期记忆", "schema-and-page", "保存对话总结并在需要时召回；明确询问过去内容时强制尝试召回，普通寒暄默认跳过。", {page: "dashboard", readiness: "智能召回已就绪"}),
      previewFeature("feature-bittorrent", "资源检索", "schema", "按关键词检索公开的资源索引，使用前请遵守当地法律与内容授权规则。"),
      previewFeature("feature-voice-clone", "语音克隆", "page", "管理授权音色，让文字回复按需要转换为语音。", {page: "voice-management"}),
      previewFeature("feature-music", "点歌", "none", "默认使用在线接口并自动回退到直连音源，无需额外配置。"),
      previewFeature("feature-parser", "链接解析", "none", "启用后直接使用内置配置解析抖音等常见平台分享链接。"),
      previewFeature("feature-twitter", "推特推送", "page", "在专属订阅管理页配置 X/Twitter 数据源、订阅会话和推送选项。", {page: "subscriptions"}),
      previewFeature("feature-proactive", "主动聊天", "schema", "按活跃度、时间段和会话上下文主动发起合适的消息。"),
      previewFeature("feature-group-analysis", "群聊分析", "schema", "汇总群聊活跃时段、热门话题、成员称号和精彩发言，支持手动分析与定时日报。", {fieldCount: 10, modelFieldCount: 1, simplified: true, scheduledAction: "group_analysis", scheduleProvider: true}),
      previewFeature("feature-commands", "常用指令", "schema", "提供新闻、天气、Epic、油价、趣味内容和帮助等常用指令。", {scheduledAction: "weather", scheduleProvider: true}),
      previewFeature("feature-group-notice", "群成员变动通知", "schema", "在成员加入、退出或群昵称变化时发送提示；不能发布或编辑微信群公告。"),
      previewFeature("feature-image", "图片生成", "schema-and-page", "提供文生图、图生图和模型管理能力。", {page: "image-models", fieldCount: 9}),
      previewFeature("feature-emoji-voice", "表情语音", "schema", "把选中的表情回复转换为匹配情绪的语音。"),
      previewFeature("feature-mystool", "米游社签到", "none", "提供米游社账号绑定、查询和签到能力。"),
      previewFeature("feature-repeater", "复读功能", "none", "在群内出现连续相同消息时进行轻量互动。"),
      previewFeature("feature-rocom", "洛克王国助手", "none", "提供洛克王国角色、阵容、交换与家园等游戏能力，并需要按插件流程绑定账号。"),
      previewFeature("feature-schedule", "定时任务", "page-and-global-schedule-center", "集中管理新闻、天气、内容推送和多站点签到计划。", {page: "schedule-center", scheduledAction: "weather"}),
      previewFeature("feature-drama", "短剧", "none", "搜索影视与体育内容，并把最新推荐定时发送到所选群聊或联系人。", {scheduledAction: "short_drama", scheduleProvider: true}),
      previewFeature("feature-video", "视频理解", "schema", "从微信短视频提取关键帧和音轨，并由已配置的视觉模型生成时间线摘要。", {modelFieldCount: 3}),
      previewFeature("feature-meme", "表情管理", "schema-and-page", "管理多套表情包、会话绑定、资源目录和同步设置。", {page: "meme-management"}),
    ];
    const groupAnalysisBasicSchema = {
      basic: {description: "基础设置", type: "object", items: {
        admin_ids: {description: "群聊分析管理员", type: "list", _ui_control: "line-list", _ui_tint: true, placeholder: "例如：wxid_admin", hint: "每行填写一个用户 ID，使用权限提示中显示的 ID；仅授予本插件的指令权限，机器人管理员仍可使用。留空时仅机器人管理员可用。"},
        group_list_mode: {description: "群聊权限模式", type: "string", options: ["whitelist", "blacklist", "none"], labels: ["白名单：仅允许列表中的群", "黑名单：屏蔽列表中的群", "不限制群聊"], hint: "白名单为空时没有群可用；黑名单为空时不屏蔽群；不限制群聊时忽略名单。此项不会授予管理员权限。"},
        group_list: {description: "群组白/黑名单列表", type: "list", _ui_control: "line-list", _group_selector: true, placeholder: "从群聊列表选择，或填写 123456@chatroom", hint: "从“群聊管理”复制群标识，例如 123456@chatroom；每行一个，不填写群昵称。"},
        bot_self_ids: {description: "群分析时屏蔽的用户ID列表", type: "list", _ui_control: "line-list", _ui_tint: true, placeholder: "例如：wxid_robot\nwxid_member", hint: "每行填写一个不希望参与报告统计的用户 ID，例如机器人或其他群成员；请勿填写群标识或昵称。留空表示不额外屏蔽用户；这里不会授予管理员权限。"},
        report_template: {description: "报告样式", type: "string", options: ["小妹", "BlueArchive", "scrapbook", "retro_futurism", "HatsuneMiku", "hack", "spring_festival", "simple", "format"], labels: ["小妹主题", "蔚蓝档案", "手账风（示例样式）", "复古未来", "初音未来", "极客终端", "新春风", "简洁风", "通用排版"], hint: "“手账风”就是示例截图中的长图样式。"},
        analysis_days: {description: "统计最近几天", type: "int", min: 1, max: 30, hint: "一般保持 1 天；需要周报时可改为 7 天。"},
        min_messages_threshold: {description: "最少消息数", type: "int", min: 1, max: 10000, hint: "低于这个数量时跳过报告，建议保持 200。"},
      }},
      auto_analysis: {description: "自动日报", type: "object", items: {
        scheduled_group_list: {description: "自动日报群", type: "list", _ui_control: "line-list", _group_selector: true, placeholder: "例如：123456@chatroom", hint: "点击“从群聊列表添加”选择群；多个群一行一个。留空时不自动发送。"},
        auto_analysis_time: {description: "每天生成时间", type: "list", _ui_control: "time-list", placeholder: "23:00", hint: "使用 24 小时制；需要多个时间时一行一个。"},
      }},
      analysis_features: {description: "报告内容", type: "object", items: {
        topic_analysis_enabled: {description: "热门话题", type: "bool", hint: "整理当天讨论最多的话题。"},
        user_title_analysis_enabled: {description: "群友画像", type: "bool", hint: "生成群友称号和简短画像。"},
        golden_quote_analysis_enabled: {description: "群聊金句", type: "bool", hint: "挑选有代表性的精彩发言。"},
        chat_quality_analysis_enabled: {description: "聊天质量点评", type: "bool", hint: "从多个角度总结当天群聊质量。"},
      }},
    };
    const groupAnalysisModelSchema = {llm: {description: "分析模型", type: "object", items: {
      llm_provider_id: {description: "用于分析的模型", type: "string", options: [""], labels: ["使用系统默认"], hint: "通常选择与智能回复相同的模型；不选择时使用当前群聊的默认模型。"},
    }}};
    const groupAnalysisConfig = {
      basic: {admin_ids: [], group_list_mode: "none", group_list: [], bot_self_ids: [], report_template: "scrapbook", analysis_days: 1, min_messages_threshold: 200},
      auto_analysis: {scheduled_group_list: ["18310516375@chatroom"], auto_analysis_time: ["23:00"]},
      analysis_features: {topic_analysis_enabled: true, user_title_analysis_enabled: true, golden_quote_analysis_enabled: true, chat_quality_analysis_enabled: true},
      llm: {llm_provider_id: ""},
      performance: {max_concurrent: 1},
    };
    const imageBasicSchema = {
      primary_model: {description: "主生图模型", type: "string", hint: "接口地址、模型名称和密钥统一在“设置 → 模型服务”管理。", _ui_group: "主生图模型"},
      secondary_enabled: {description: "使用备用生图模型", type: "bool", hint: "主模型不可用时才会尝试；备用服务从已配置的图片模型中选择。", _ui_group: "备用生图模型", _ui_full: true, _ui_disclosure: "添加备用模型"},
      secondary_model: {description: "备用生图模型", type: "string", hint: "备用模型需先在“设置 → 模型服务”中添加。", _ui_group: "备用生图模型"},
      use_system_proxy: {description: "使用本地代理", type: "bool", hint: "关闭时直连；端口留空时使用系统已有代理设置。", _ui_group: "本地代理", _ui_full: true},
      local_proxy_port: {description: "本地代理端口（可留空）", type: "string", hint: "例如 7890。仅连接本机 127.0.0.1；留空时使用系统已有代理设置。", _ui_group: "本地代理", _ui_control: "proxy-port", _ui_depends_on: "use_system_proxy", min: 1, max: 65535},
    };
    for (const [key, field] of Object.entries(imageBasicSchema)) {
      if (key.startsWith("secondary_") && key !== "secondary_enabled") field._ui_depends_on = "secondary_enabled";
    }
    const imageConfig = {
      primary_model: "",
      secondary_enabled: false, secondary_model: "",
      use_system_proxy: true, local_proxy_port: "",
    };
    const previewImageSchema = () => {
      const schema = structuredClone(imageBasicSchema);
      const choices = previewProviders
        .filter((item) => item.service_kind === "image")
        .map((item) => ({value: item.id, label: `${item.name || item.model} · ${item.model}`}));
      ["primary_model", "secondary_model"].forEach((key) => {
        schema[key].options = ["", ...choices.map((item) => item.value)];
        schema[key].labels = [key === "primary_model" ? "请选择已配置的图片模型" : "请选择备用图片模型", ...choices.map((item) => item.label)];
        schema[key].hint = choices.length
          ? "接口地址、模型名称和密钥统一在“设置 → 模型服务”管理。"
          : "先到“设置 → 模型服务”添加图片模型，返回后即可选择。";
      });
      return {schema, choices};
    };
    const previewGuides = {};
    const previewGuideOverrides = {
      "群聊上下文": {summary: "协调群聊接话和多人上下文", setup_steps: ["设置回复节奏和允许接管的会话。"], prerequisites: ["它不是长期记忆库。"], audience: "群聊常用", readiness: "可用但已纠正名称", recommendation: "保留。适合多人群聊的接话与上下文协调，但不应宣传为长期记忆。"},
      "群聊分析": {summary: "把一天群聊整理成日报", setup_steps: ["从管理器的群聊列表选择自动日报群。", "先让当前机器人账号在群里正常接收一段时间，再开启日报。"], prerequisites: ["只覆盖新桥接成功接收后的本机消息；安装前和离线期间的记录无法补拉。"], audience: "群管理常用", readiness: "已适配本机消息记录", recommendation: "建议保留并按需启用；数据按当前机器人账号和群聊双重隔离。"},
      "定时任务": {summary: "安排内容推送和签到", setup_steps: ["打开专属配置页创建计划，再到定时任务中心调整。"], prerequisites: ["写操作仅允许可靠管理员身份；共享账号签到仅允许机器人管理员。"], audience: "群管理常用", readiness: "权限已加固", recommendation: "建议保留；任务按当前会话隔离，微信无法确认群角色时默认拒绝修改。"},
      "自动改代码（实验）": {summary: "实验性的功能代码维护", setup_steps: ["仅由了解代码与回滚的维护人员使用。"], prerequisites: ["可读写并重新加载功能代码。"], audience: "不建议默认启用", readiness: "实验性高风险", recommendation: "不属于群主管理刚需，应从小白默认界面和新安装默认项移除。"},
      "群成员变动通知": {summary: "提示入群、退群和群昵称变化", setup_steps: ["设置提示文案后在测试群验证。"], prerequisites: ["不能发布或编辑微信群公告。"], audience: "群管理常用", readiness: "收到成员事件即可用", recommendation: "建议保留，并始终使用准确名称。"},
      "图片生成": {summary: "用文字或参考图生成图片", setup_steps: ["先到“设置 → 模型服务”添加图片模型。", "回到此处选择主模型；确有需要时再添加一个备用模型。", "需要代理时开启本地代理，并填写端口或沿用系统已有代理设置。"], prerequisites: ["需要支持生图的图片模型；聊天模型不能自动替代生图模型。"], audience: "场景选配", readiness: "需要生图服务", recommendation: "有生图需求时启用；图片模型服务统一管理。"},
      "洛克王国助手": {summary: "使用洛克王国游戏助手", setup_steps: ["通过二维码或已有凭据绑定游戏账号。"], prerequisites: ["账号凭据不要发到群里。"], audience: "兴趣选配", readiness: "需要游戏账号绑定", recommendation: "仅洛克王国用户群有必要，不是运势功能。"},
    };
    const previewSavedConfigs = new Map();
    const corePreviewSchema = {
      analyzer_model: {type: "string", description: "群聊分析模型", options: [""], labels: ["请选择模型（完整使用所必需）"], default: ""},
      personality: {type: "object", description: "身份与接话原则", items: {ai_self_identity: {type: "text", description: "群聊身份", default: "你是小妹，是群聊里的熟人型机器人。说话自然、直接。"}, reply_strategy_guide: {type: "text", description: "接话原则", default: "围绕当前话题提供帮助，不连续插话。"}}},
      wake_interaction: {type: "object", description: "点名与唤醒", items: {alias: {type: "string", description: "可识别的昵称", default: "小妹"}, enter_on_mention_only: {type: "bool", description: "仅在被点名后参与", default: true}, direct_wake_fast_path: {type: "bool", description: "点名时快速回应", default: true}}},
      timing: {type: "object", description: "回复节奏", items: {assistant_debounce_time: {type: "float", description: "点名后等待补充（秒）", default: 1, min: 0}, waiting_time: {type: "float", description: "回复后休息（秒）", default: 30, min: 0}}},
      energy: {type: "object", description: "能量设置", items: {initial_energy: {type: "float", description: "初始能量", default: 100}}},
      leave_reply: {type: "object", description: "离场应答", items: {leave_echo_reply: {type: "bool", description: "复读时离场应答", default: false}}},
      reply_length: {type: "object", description: "回复长度", items: {normal_reply_max_chars: {type: "int", description: "普通回复参考字数", default: 20}, focus_reply_max_chars: {type: "int", description: "分析类回复参考字数", default: 200}}},
    };
    const corePreviewTemplateSchema = Object.fromEntries(Object.entries(corePreviewSchema).filter(([, field]) => field.type === "object").map(([key, field]) => [key, structuredClone(field)]));
    delete corePreviewTemplateSchema.wake_interaction.items.direct_wake_fast_path;
    const corePreviewDefaults = (schema) => Object.fromEntries(Object.entries(schema).map(([key, field]) => [key, field.type === "object" ? corePreviewDefaults(field.items) : structuredClone(field.default)]));
    let corePreviewConfig = corePreviewDefaults(corePreviewSchema);
    let corePreviewIdentity = {robot_name: "小妹", admin_id: "wxid_admin"};
    const corePreviewTemplates = [];
    const corePreviewBindings = {};
    let corePreviewRevision = 1;
    let corePreviewTemplateRevision = 1;
    const corePreviewDetail = () => ({id: "feature-95a906d4b9173155", display_name: "机器人核心", enabled: true, engine_online: true, revision: String(corePreviewRevision), identity: structuredClone(corePreviewIdentity), basic_schema: Object.fromEntries(Object.entries(corePreviewSchema).filter(([key]) => !["analyzer_model", "energy", "leave_reply"].includes(key))), model_schema: {analyzer_model: corePreviewSchema.analyzer_model}, advanced_schema: structuredClone(corePreviewSchema), advanced_config: structuredClone(corePreviewConfig), template_schema: structuredClone(corePreviewTemplateSchema)});
    const corePreviewChats = [{chat_id: "wechat:GroupMessage:51632940287@chatroom", display_name: "旅行交流群", kind: "group"}, {chat_id: "wechat:FriendMessage:wxid_zhangsan", display_name: "张三", kind: "private"}];
    const corePreviewRequest = (method, endpoint, payload = {}) => {
      if (method === "GET" && endpoint === "profiles") return {templates: structuredClone(corePreviewTemplates), bindings: {...corePreviewBindings}};
      if (method === "GET" && endpoint === "chats") return structuredClone(corePreviewChats);
      if (method === "GET" && endpoint === "chat_status") return corePreviewChats.map((chat, index) => ({...chat, template_id: corePreviewBindings[chat.chat_id] || "", status: {current_status: index ? "NOT_PRESENT" : "OBSERVATION"}, energy: 86, patrol: {waiting: "", remaining: 0, total: 0}, last_decision: {should_reply: true, summary: "回应当前群友的问题"}}));
      if (endpoint === "profiles/create") { const item = {id: `tpl_preview_${++corePreviewTemplateRevision}`, name: payload.name, description: payload.description || "", updated_at: corePreviewTemplateRevision, config: payload.from_global ? Object.fromEntries(Object.entries(corePreviewConfig).filter(([key]) => key in corePreviewTemplateSchema).map(([key, value]) => [key, Object.fromEntries(Object.entries(value).filter(([field]) => field in corePreviewTemplateSchema[key].items))])) : {}}; corePreviewTemplates.push(item); return structuredClone(item); }
      if (endpoint === "profiles/update") { const item = corePreviewTemplates.find((tpl) => tpl.id === payload.id); if (!item || item.updated_at !== payload.updated_at) throw new Error("模板已被其他窗口修改，请重新读取后保存。"); const patch = structuredClone(payload); delete patch.updated_at; Object.assign(item, patch, {updated_at: ++corePreviewTemplateRevision}); return structuredClone(item); }
      if (endpoint === "profiles/delete") { corePreviewTemplates.splice(corePreviewTemplates.findIndex((tpl) => tpl.id === payload.id), 1); Object.keys(corePreviewBindings).forEach((id) => { if (corePreviewBindings[id] === payload.id) delete corePreviewBindings[id]; }); return {}; }
      if (endpoint === "bindings/set") { if (payload.template_id) corePreviewBindings[payload.chat_id] = payload.template_id; else delete corePreviewBindings[payload.chat_id]; return {}; }
      throw new Error("预览请求无效。");
    };
    function previewFeatureDetail(id) {
      const found = features.find((value) => value.id === id);
      const item = {...found, guide: previewGuideOverrides[found?.display_name] || previewGuides[found?.display_name] || {}};
      if (id === "feature-image") {
        const image = previewImageSchema();
        return {
        ...item,
        basic_schema: image.schema,
        model_schema: {},
        schema: image.schema,
        advanced_schema: {},
        config: structuredClone(previewSavedConfigs.get(id) || imageConfig),
        advanced_config: {},
        configuration_note: "图片服务地址、模型名称和密钥统一在模型服务管理；此页只选择主/备用模型与代理。",
        configuration_profile: "image-service-selection",
        has_legacy_image_model: false,
        setup_steps: item.guide.setup_steps,
        revision: "preview",
        providers: [],
        image_providers: image.choices,
        personas: [],
        engine_online: true,
        };
      }
      if (id !== "feature-group-analysis") return {...item, basic_schema: {}, model_schema: {}, schema: {}, config: {}, revision: "preview", providers: [], personas: [], engine_online: true};
      const {auto_analysis: analysisScheduleSchema, ...analysisCommonSchema} = groupAnalysisBasicSchema;
      return {
        ...item,
        basic_schema: {...analysisCommonSchema, ...groupAnalysisModelSchema},
        model_schema: {},
        configuration_profile: "group-analysis-simple",
        schedule_schema: {basic: groupAnalysisBasicSchema.basic, auto_analysis: analysisScheduleSchema},
        schedule_config: structuredClone(previewSavedConfigs.get(id) || groupAnalysisConfig),
        schema: {...analysisCommonSchema, ...groupAnalysisModelSchema},
        config: structuredClone(previewSavedConfigs.get(id) || groupAnalysisConfig),
        advanced_config: structuredClone(previewSavedConfigs.get(id) || groupAnalysisConfig),
        advanced_schema: {...groupAnalysisBasicSchema, ...groupAnalysisModelSchema, incremental: {type: "object", description: "增量分析设置", items: {
          incremental_group_list: {description: "增量分析群列表", type: "list", _ui_control: "line-list", _group_selector: true, placeholder: "从群聊列表选择，或填写 123456@chatroom", hint: "从“群聊管理”复制群标识，例如 123456@chatroom；每行一个。目标群还须通过基础使用范围和定时名单。"},
        }}, performance: {type: "object", description: "并发设置", items: {max_concurrent: {type: "int", description: "同时生成报告数量", min: 1, max: 5, default: 1}}}},
        configuration_note: "这里只展示日常会用到的设置。渲染、重试、并发和提示词等专业参数继续使用推荐值，保存时不会被重置。",
        setup_steps: ["在常用设置中选择分析模型与报告内容", "到主定时任务标签选择群聊、统计范围和生成时间，无需在群里发送命令", "也可在群里发送 /群分析 1 或 /群分析 7"],
        revision: "preview",
        providers: [],
        personas: [],
        engine_online: true,
      };
    }
    let previewProviders = [];
    if (new URLSearchParams(window.location.search).get("demo") === "thinking-mode") {
      previewProviders = [{id: "custom_mimo/mimo-v2.5", name: "mimo-v2.5", api_base: "https://token-plan-cn.xiaomimimo.com/v1", model: "mimo-v2.5", configured: true, api_key_configured: true, is_default: true, is_recommended: false, thinking_supported: true, thinking_enabled: true}];
    }
    const previewPresets = [
      {id: "deepseek", name: "DeepSeek", api_base: "https://api.deepseek.com", model: "deepseek-flash", service_kind: "chat", modalities: ["text", "image", "audio"], description: "DeepSeek 官方兼容接口；默认填入 DeepSeek Flash。"},
      {id: "glm", name: "智谱 GLM", api_base: "https://open.bigmodel.cn/api/paas/v4", model: "glm-4.6v", service_kind: "chat", modalities: ["text", "image", "audio"], description: "智谱 GLM 兼容接口；默认填入 GLM-4.6V。"},
      {id: "xiaomi_mimo", name: "小米 MiMo", api_base: "https://api.xiaomimimo.com/v1", model: "mimo-v2.5", service_kind: "chat", modalities: ["text", "image", "audio"], description: "小米 MiMo 兼容接口；默认填入 MiMo-V2.5。"},
      {id: "qwen", name: "通义千问 Qwen", api_base: "https://dashscope.aliyuncs.com/compatible-mode/v1", model: "qwen-plus", service_kind: "chat", modalities: ["text", "image", "audio"], description: "阿里云百炼兼容接口；默认填入 qwen-plus。"},
      {id: "volcengine", name: "火山引擎豆包", api_base: "https://ark.cn-beijing.volces.com/api/v3", model: "doubao-seed-2-0-lite-260215", service_kind: "chat", modalities: ["text", "image", "audio"], description: "火山方舟兼容接口；默认填入豆包 Seed 2.0。"},
      {id: "hunyuan", name: "腾讯混元", api_base: "https://tokenhub.tencentmaas.com/v1", model: "hy3-preview", service_kind: "chat", modalities: ["text", "image", "audio"], description: "腾讯 TokenHub 混元兼容接口；默认填入 hy3-preview。"},
      {id: "gemini_proxy", name: "Gemini", api_base: "https://generativelanguage.googleapis.com/v1beta/openai", model: "gemini-3.8-flash", service_kind: "chat", modalities: ["text", "image", "audio"], description: "Gemini 官方兼容接口；默认填入 gemini-3.8-flash。"},
      {id: "sub2", name: "Sub2 兼容服务", api_base: "", model: "", service_kind: "chat", modalities: ["text", "image", "audio"], description: "填写 Sub2 兼容接口地址和模型名称；按所选服务的实际能力使用。"},
      {id: "embedding_custom", name: "向量模型（兼容接口）", api_base: "", model: "", service_kind: "embedding", modalities: ["embedding"], description: "新增 Embedding 文本向量模型。"},
      {id: "image_custom", name: "图片模型（兼容接口）", api_base: "", model: "", service_kind: "image", modalities: ["image"], description: "新增图片生成模型。"},
      {id: "voice_custom", name: "语音模型（兼容接口）", api_base: "", model: "", service_kind: "voice", modalities: ["audio"], description: "新增语音模型。"},
      {id: "custom", name: "其他兼容服务", api_base: "", model: "", service_kind: "chat", modalities: ["text", "image", "audio"], description: "填写中转站或其他兼容接口地址、模型名称和凭据。"},
    ];
    const previewExperience = {
      onboarding_complete: true,
      capabilities: {persona: true, memory: true, emotion: true},
      memory_backend: "长期记忆",
      memory_explanation: "聊天摘要和相关往事由长期记忆保存与召回；明确问起上次内容时优先回忆，普通寒暄不查询。",
      readiness: {memory: true},
      persona: {tone: "natural", reply_length: "brief", mention_only: false, fast_reply: true, alias: "小妹", identity_prompt: "你是小妹，是群聊里的熟人型机器人。说话随意、直接、简短，不使用机械的客服措辞。", reply_guide: "一次只处理当前最相关的话题，不抢话，不连续刷屏。"},
      memory: {enabled: true, recall_mode: "smart", casual_query_max_chars: 24, top_k: 3, summary_trigger_rounds: 10},
      emotion: {enabled: true, daily_mood: true, include_affection: true, include_mood: true},
    };
    const previewOnboardingReadiness = () => ({
      schema_version: 1,
      readonly: true,
      complete: true,
      items: [
        {id: "wechat", label: "微信环境", status: "ready", detail: "预览环境正常", next_step: "继续检查"},
        {id: "account", label: "机器人账号", status: "ready", detail: "预览账号已登录", next_step: "已确认"},
        {id: "provider", label: "回复模型", status: "ready", detail: "预览回复模型已准备好", next_step: "继续检查"},
        {id: "groups", label: "目标群聊", status: "ready", detail: "已读取预览群聊目录", next_step: "进入群聊管理选择目标群"},
        {id: "core", label: "核心功能", status: "ready", detail: "机器人核心和功能目录已可检查", next_step: "继续检查"},
      ],
      test_hint: "准备完成后，可在选定群里点名机器人测试。",
      message: "预览数据不会启动机器人或发送消息。",
    });
    let previewWebSearch = {
      enabled: false,
      provider: "bocha",
      link_enabled: false,
      providers: Object.fromEntries([
        ["tavily", "Tavily", "https://app.tavily.com/"], ["bocha", "博查 Bocha", "https://open.bocha.cn/"], ["brave", "Brave Search", "https://brave.com/search/api/"],
        ["baidu_app_builder", "百度 AI Search", "https://cloud.baidu.com/product/ai_search"], ["firecrawl", "Firecrawl", "https://www.firecrawl.dev/"], ["exa", "Exa", "https://exa.ai/"],
      ].map(([id, name, website_url]) => [id, {id, name, website_url, description: id === "tavily" ? "适合通用英文与国际资讯搜索。免费方案每月含 1,000 API credits，实际消耗按请求类型计算。" : "支持联网搜索与网页结果整理。", configured: id === "bocha", key_count: id === "bocha" ? 1 : 0}])),
    };
    let previewGroupPolicy = {bot_enabled: true, reply_mode: "mention", allowed_features: ["feature-memory", "feature-music", "feature-parser", "feature-twitter", "feature-group-analysis", "feature-commands", "feature-group-notice", "feature-image"], scheduled_enabled: true, quiet_hours: {enabled: true, start: "22:00", end: "08:00", timezone: "Asia/Shanghai"}, daily_limit: 3, revision: "preview-policy-1"};
    const previewPolicyOverview = (groupId) => ({policy: {...structuredClone(previewGroupPolicy), account_id: "wxid_demo_account", group_id: groupId, permissions: {group_role: "unknown", can_edit_local_policy: true}}, features: features.map((item) => ({id: item.id, name: item.display_name, allowed: previewGroupPolicy.allowed_features.includes(item.id), globally_enabled_by_default: item.enabled})), proactive: {total: 2, running: 1, paused: 1, message: "主动聊天由各功能自行配置；定时任务按本群设置执行。"}, permissions: {group_role: "unknown", can_edit_local_policy: true}});
    let previewPrivatePolicy = {allowed_features: ["feature-memory", "feature-music", "feature-commands"], bot_enabled: true, scheduled_enabled: true, quiet_hours: {enabled: false, start: "22:00", end: "08:00", timezone: "Asia/Shanghai"}, daily_limit: 3, revision: "preview-private-1", configured: false};
    const previewPrivatePolicyOverview = (contactId) => ({policy: {...structuredClone(previewPrivatePolicy), account_id: "wxid_demo_account", contact_id: contactId}, features: features.filter((item) => item.id !== "feature-group-notice").map((item) => ({id: item.id, name: item.display_name, allowed: previewPrivatePolicy.allowed_features.includes(item.id), globally_enabled_by_default: item.enabled}))});
    const previewContacts = [
      {user_id: "wxid_zhangsan", name: "张三", nickname: "山川与海", remark: "张三", alias: "zhangsan_88", avatar: "", gender: "男", gender_code: 1, country: "CN", province: "Zhejiang", city: "Hangzhou", contact_type: "微信好友", signature: "周末去爬山"},
      {user_id: "wxid_lisi", name: "李四｜摄影", nickname: "光影之间", remark: "李四｜摄影", alias: "lisi_photo", avatar: "", gender: "女", gender_code: 2, country: "CN", province: "Shanghai", city: "Shanghai", contact_type: "微信好友"},
      {user_id: "wxid_wangwu", name: "王五", nickname: "王五", remark: "", alias: "", avatar: "", gender: "未知", country: "CN", province: "Jiangsu", city: "Nanjing", contact_type: "微信好友"},
      {user_id: "gh_service", name: "社区服务", nickname: "社区服务", remark: "", alias: "community_service", avatar: "", gender: "", country: "CN", province: "", city: "", contact_type: "公众号"},
      {user_id: "filehelper", name: "文件传输助手", nickname: "文件传输助手", remark: "", alias: "", avatar: "", gender: "", country: "", province: "", city: "", contact_type: "系统联系人"},
    ];
    const previewHistoryItems = [
      {conversation_id: "51632940287@chatroom", type: "group", display_name: "旅行交流群", message_count: 3, last_timestamp: Date.now() / 1000, last_content: "张三｜3栋：帮我找一下今天更新的短剧"},
      {conversation_id: "wxid_zhangsan", type: "private", display_name: "张三", message_count: 1, last_timestamp: Date.now() / 1000 - 3600, last_content: "明天杭州会下雨吗？"},
      {conversation_id: "81263495012@chatroom", type: "group", display_name: "短剧分享群", message_count: 1, last_timestamp: Date.now() / 1000 - 7200, last_content: "李四｜摄影：收到，这样看起来很方便"},
      {conversation_id: "22001983641@chatroom", type: "group", display_name: "业主交流群", message_count: 0, last_timestamp: 0},
      {conversation_id: "34902765188@chatroom", type: "group", display_name: "周末羽毛球", message_count: 0, last_timestamp: 0},
    ];
    const previewHistoryMessages = [
      {id: 1, conversation_id: "51632940287@chatroom", role: "user", sender_id: "wxid_zhangsan", wechat_id: "zhangsan_88", sender_name: "张三", group_nickname: "张三｜3栋", content: "帮我找一下今天更新的短剧", timestamp: Date.now() / 1000 - 180},
      {id: 2, conversation_id: "51632940287@chatroom", role: "assistant", sender_id: "bot", sender_name: "机器人", content: "找到了 8 部今天更新的内容，我已经整理成播放页发到群里。", timestamp: Date.now() / 1000 - 160},
      {id: 3, conversation_id: "51632940287@chatroom", role: "user", sender_id: "wxid_lisi", wechat_id: "lisi_photo", sender_name: "李四", group_nickname: "李四｜摄影", content: "收到，这样看起来很方便", timestamp: Date.now() / 1000 - 120},
      {id: 4, conversation_id: "wxid_zhangsan", role: "user", sender_id: "wxid_zhangsan", wechat_id: "zhangsan_88", sender_name: "张三", content: "明天杭州会下雨吗？", timestamp: Date.now() / 1000 - 3600},
      {id: 5, conversation_id: "81263495012@chatroom", role: "user", sender_id: "wxid_lisi", wechat_id: "lisi_photo", sender_name: "李四", group_nickname: "李四｜摄影", content: "收到，这样看起来很方便", timestamp: Date.now() / 1000 - 7200},
    ];
    const previewHistoryMatches = (message, keyword) => [message.sender_id, message.wechat_id].includes(keyword);
    const previewConversations = (filters = {}) => {
      const search = String(filters.search || "").trim().toLocaleLowerCase("zh-CN");
      const items = previewHistoryItems.filter((item) => {
        if (filters.kind && filters.kind !== "all" && item.type !== filters.kind) return false;
        if (!search) return true;
        if (search.startsWith("wxid_")) return item.conversation_id === search || previewHistoryMessages.some((message) => message.conversation_id === item.conversation_id && previewHistoryMatches(message, search));
        return [item.conversation_id, item.display_name, item.last_content].some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(search)) || previewHistoryMessages.some((message) => message.conversation_id === item.conversation_id && [message.sender_name, message.wechat_id, message.group_nickname, message.content].some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(search)));
      });
      const messages = search.startsWith("wxid_") ? previewHistoryMessages.filter((message) => previewHistoryMatches(message, search)) : previewHistoryMessages;
      const viewItems = items.map((item) => {
        if (!search.startsWith("wxid_")) return item;
        const own = messages.filter((message) => message.conversation_id === item.conversation_id);
        const latest = own.at(-1);
        return {...item, message_count: own.length, last_content: latest?.content || "", last_timestamp: latest?.timestamp || 0};
      });
      return {account_id: "wxid_demo_account", items: structuredClone(viewItems), summary: {messages: messages.filter((message) => items.some((item) => item.conversation_id === message.conversation_id)).length, conversations: items.length, visible_conversations: items.length, directory_groups: 6, private_messages: messages.filter((message) => items.some((item) => item.type === "private" && item.conversation_id === message.conversation_id)).length, group_messages: messages.filter((message) => items.some((item) => item.type === "group" && item.conversation_id === message.conversation_id)).length}};
    };
    const previewTaskGroups = [{id: "51632940287@chatroom", name: "旅行交流群"}, {id: "81263495012@chatroom", name: "短剧分享群"}, {id: "22001983641@chatroom", name: "业主交流群"}, {id: "34902765188@chatroom", name: "周末羽毛球"}];
    const previewTasks = new Map();
    let previewTaskRevision = 0;
    const previewTaskTimes = (schedule) => {
      const today = new Intl.DateTimeFormat("en-CA", {timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit"}).format(new Date());
      const start = new Date(`${schedule.kind === "once" ? schedule.date : today}T${schedule.time}:00+08:00`);
      const values = [];
      for (let day = 0; day < (schedule.kind === "once" ? 1 : 22); day += 1) {
        const candidate = new Date(start.getTime() + day * 86400000);
        const weekday = (new Date(candidate.getTime() + 8 * 3600000).getUTCDay() + 6) % 7;
        if (candidate.getTime() > Date.now() && (schedule.kind !== "weekly" || (schedule.weekdays || []).includes(weekday))) values.push(candidate.toISOString());
        if (values.length === 3) break;
      }
      return values;
    };
    const previewTaskSave = (payload) => {
      const item = structuredClone(payload);
      item.revision = String(++previewTaskRevision);
      item.enabled = !item.draft && Boolean(item.enabled);
      item.status = item.draft ? "draft" : item.enabled ? "enabled" : "paused";
      item.schedule_text = scheduledTimeText(item.schedule);
      item.next_run_time = item.enabled ? previewTaskTimes(item.schedule)[0] || null : null;
      previewTasks.set(item.id, item);
      return {item: structuredClone(item), message: item.draft ? "预览：任务草稿已保存。" : "预览：任务已保存，不会实际发送。"};
    };
    const ok = (data, message = "") => Promise.resolve({ok: true, data, message});
    const setPreviewRuntime = (running) => {
      Object.assign(status, running ? {
        state: "RUNNING", title: "机器人可用", message: "微信连接和全部功能运行正常。", busy: false,
        online: true, can_start: false, can_stop: true, error: null,
      } : {
        state: "STOPPED", title: "机器人尚未启动", message: "微信环境已准备好，可以启动机器人。", busy: false,
        online: false, can_start: true, can_stop: false, error: null,
      });
      status.runtime = {...status.runtime, engine_ready: running, connection_ready: running, upstream_connected: running, heartbeat_fresh: running};
      status.environment = {...status.environment, process_running: running};
    };
    // Only instantiated by the existing ?preview entry point; no live API uses these fixtures.
    const licenseParams = new URLSearchParams(window.location.search);
    const licenseDemo = licenseParams.get("demo") === "subscription" || licenseParams.has("license");
    const previewLicensePlans = [
      {id: "trial", name: "1 小时试用", price_fen: 0, duration_seconds: 3600},
      {id: "monthly", name: "月付", price_fen: 6900, duration_seconds: 30 * 86400},
      {id: "yearly", name: "年付", price_fen: 69900, duration_seconds: 365 * 86400},
    ];
    let previewLicenseState = licenseDemo ? licenseParams.get("license") || "unactivated" : "disabled";
    const previewTrialMode = ["trial-active", "trial-ended", "trial-used"].includes(previewLicenseState) ? previewLicenseState : "";
    if (previewTrialMode) previewLicenseState = previewTrialMode === "trial-active" ? "active" : previewTrialMode === "trial-ended" ? "expired" : "unactivated";
    const previewLicense = {license_id: ["pending", "active", "expired", "offline"].includes(previewLicenseState) ? "lic_preview" : "",
      device_id: ["pending", "active", "expired", "offline"].includes(previewLicenseState) ? "a".repeat(64) : "", plan: "monthly", account: ["active", "expired", "offline"].includes(previewLicenseState) ? "wxid_demo_account" : "",
      valid_until: previewLicenseState === "expired" ? Math.floor(Date.now() / 1000) - 60 : Math.floor(Date.now() / 1000) + 30 * 86400, can_run: false};
    if (["trial-active", "trial-ended"].includes(previewTrialMode)) {
      previewLicense.plan = "trial";
      if (previewTrialMode === "trial-active") previewLicense.valid_until = Math.floor(Date.now() / 1000) + 3600;
    }
    const previewCodes = new Map();
    const redeemedPreviewCodes = new Set();
    if (licenseDemo) {
      setPreviewRuntime(false);
      if (!["active", "expired", "offline"].includes(previewLicenseState)) status.runtime.wechat_identity = "";
      if (previewTrialMode) status.runtime.wechat_identity = "wxid_demo_account";
    }
    const licenseError = (code, message) => Promise.resolve({ok: false, error: {code, message}});
    const licenseSnapshot = () => {
      const enabled = previewLicenseState !== "disabled";
      const configured = !["disabled", "unconfigured"].includes(previewLicenseState);
      const validLicense = Boolean(previewLicense.license_id) && previewLicense.valid_until > Date.now() / 1000;
      const canConnect = configured && (previewLicenseState === "unactivated" || validLicense);
      const authorized = validLicense && Boolean(previewLicense.account) && previewLicense.account === status.runtime.wechat_identity;
      const messages = {
        disabled: "此安装尚未启用商业授权，现有功能可按原方式使用。",
        unconfigured: "授权服务尚未配置，请联系软件提供者。",
        unactivated: "连接微信后可激活免费试用；已有激活码可在此查看并确认兑换。",
        pending: "激活码已兑换，请连接微信并确认实际登录账号。",
        active: "授权已生效，由你确认启动机器人。",
        expired: "自动化已暂停。可兑换续期码，微信与本机数据保留。",
        offline: "暂时无法连接授权服务，现有授权仍在离线有效期内。",
      };
      return {...previewLicense, enabled, configured, state: previewLicenseState, authorized, can_connect: canConnect,
        current_account: status.runtime.wechat_identity || "", remaining_seconds: previewLicense.license_id ? Math.max(0, Math.floor(previewLicense.valid_until - Date.now() / 1000)) : 0,
        valid_until: previewLicense.license_id ? previewLicense.valid_until : 0, plan: previewLicense.license_id ? previewLicense.plan : "",
        message: messages[previewLicenseState], plans: structuredClone(previewLicensePlans)};
    };
    const trialSnapshot = () => {
      const local = licenseSnapshot();
      const plan = local.plan;
      const state = plan === "trial" ? local.remaining_seconds > 0 ? "active" : "ended"
        : plan ? "paid" : previewTrialMode === "trial-used" ? "used" : "eligible";
      return {state, eligible: state === "eligible", recoverable: false,
        account: status.runtime.wechat_identity || "", valid_until: plan === "trial" ? local.valid_until : 0,
        remaining_seconds: plan === "trial" ? local.remaining_seconds : 0};
    };
    const previewLicenseApi = {
      get_license_status: () => ok(licenseSnapshot()),
      get_trial_status: () => status.runtime.wechat_identity ? ok({free_trial: trialSnapshot()})
        : licenseError("account_required", "请先连接微信，再激活免费试用。"),
      activate_free_trial: (expectedAccount) => {
        if (!status.runtime.wechat_identity) return licenseError("account_required", "请先连接微信，再激活免费试用。");
        if (expectedAccount !== status.runtime.wechat_identity) return licenseError("account_changed", "微信登录账号已变化，请重新查询试用资格。");
        const trial = trialSnapshot();
        if (!trial.eligible) return licenseError("trial_used", "你的试用已结束，可联系群主购买");
        Object.assign(previewLicense, {license_id: "lic_preview_trial", device_id: "a".repeat(64), plan: "trial",
          account: expectedAccount, valid_until: Math.floor(Date.now() / 1000) + 3600, can_run: false});
        previewLicenseState = "active";
        return ok(licenseSnapshot());
      },
      get_license_contact: async () => {
        const unavailable = {state: "unavailable", group_name: "", expires_at: 0, remaining_seconds: 0, image_data_url: "", source: "bundled",
          message: "暂时无法获取入群二维码，请刷新重试。"};
        if (licenseParams.get("contact") === "unavailable") return ok(unavailable);
        try {
          const response = await fetch("/assets/preview/licensing-contact.json", {cache: "no-store"});
          if (!response.ok) return ok(unavailable);
          const data = await response.json();
          const expired = licenseParams.get("contact") === "expired" || Number(data.expires_at) <= Date.now() / 1000;
          return ok({state: expired ? "expired" : "ready", group_name: data.group_name, expires_at: data.expires_at,
            remaining_seconds: expired ? 0 : Math.max(0, Number(data.expires_at) - Date.now() / 1000),
            image_data_url: expired ? "" : `data:${data.mime_type};base64,${data.image_base64}`, source: "bundled", message: ""});
        } catch { return ok(unavailable); }
      },
      prepare_license_device: () => { previewLicense.device_id = "a".repeat(64); return ok(licenseSnapshot()); },
      refresh_license: () => { if (previewLicenseState === "offline") previewLicenseState = "active"; return ok(licenseSnapshot()); },
      preview_activation_code: (code) => {
        if (!licenseSnapshot().configured) return licenseError("NOT_CONFIGURED", "授权服务尚未配置。");
        previewLicense.device_id = "a".repeat(64);
        const plan = previewLicensePlans.find((item) => code.trim().toUpperCase() === `WRB-${item.id.toUpperCase()}-DEMO`);
        if (!plan) return licenseError("INVALID_CODE", "激活码无效，请检查后重试。");
        const already = redeemedPreviewCodes.has(code);
        if (plan.id === "trial" && previewLicense.license_id && !already) return licenseError("TRIAL_USED", "当前设备已有授权，请使用月付或年付续期码。");
        const preview = {preview_id: `preview_${previewCodes.size + 1}`, plan: plan.id, mode: previewLicense.license_id ? "renewal" : "activation",
          valid_until: already ? previewLicense.valid_until : Math.floor(Math.max(Date.now() / 1000, previewLicense.license_id ? previewLicense.valid_until : 0)) + plan.duration_seconds,
          license_id: previewLicense.license_id, already_redeemed: already, message: "预览演示：确认后仅改变此页面的演示状态。"};
        previewCodes.set(preview.preview_id, {code, data: preview});
        return ok(preview);
      },
      redeem_activation_code: (code, previewId) => {
        const preview = previewCodes.get(previewId);
        if (!preview || preview.code !== code) return licenseError("PREVIEW_EXPIRED", "请重新查看激活码，再确认兑换。");
        const plan = previewLicensePlans.find((item) => item.id === preview.data.plan);
        const validUntil = redeemedPreviewCodes.has(code) ? previewLicense.valid_until
          : Math.floor(Math.max(Date.now() / 1000, previewLicense.license_id ? previewLicense.valid_until : 0)) + plan.duration_seconds;
        Object.assign(previewLicense, {license_id: "lic_preview", valid_until: validUntil, plan: preview.data.plan, can_run: false});
        redeemedPreviewCodes.add(code);
        previewLicenseState = previewLicense.account ? "active" : "pending";
        return ok(licenseSnapshot());
      },
      connect_license_wechat: () => {
        if (!licenseSnapshot().can_connect) return licenseError("NOT_AUTHORIZED", "请先激活授权。");
        status.runtime.wechat_identity = "wxid_demo_account";
        return ok(licenseSnapshot());
      },
      bind_license_account: (expectedAccount) => {
        if (!licenseSnapshot().can_connect || expectedAccount !== status.runtime.wechat_identity) return licenseError("ACCOUNT_CHANGED", "微信账号已变化，请重新确认。");
        previewLicense.account = expectedAccount; previewLicenseState = "active";
        return ok(licenseSnapshot());
      },
    };
    return {
      ...previewLicenseApi,
      bootstrap: () => ok({version: "0.9.9", install_root: "D:\\小妹", status, brand: {display_name: "小妹", short_name: "小妹", persona_name: "小妹", welcome_text: "你好，我是小妹。", admin_id: "wxid_admin"}, provider: {configured: false, api_base: "", model: "", api_key_configured: false}, providers: structuredClone(previewProviders), provider_presets: previewPresets, experience: structuredClone(previewExperience), readiness: previewOnboardingReadiness(), update: {enabled: true, channel: "stable", check_interval_seconds: 600, result: null}}),
      get_status: () => ok(status), check_environment: () => ok(status),
      start_robot: () => {
        if (licenseSnapshot().enabled && !licenseSnapshot().authorized) return licenseError("NOT_AUTHORIZED", "请完成授权后再启动。");
        previewLicense.can_run = true; setPreviewRuntime(true); return ok({accepted: true});
      },
      stop_robot: () => { previewLicense.can_run = false; setPreviewRuntime(false); return ok({accepted: true}); },
      force_stop_robot: () => { previewLicense.can_run = false; setPreviewRuntime(false); return ok({accepted: true}); }, select_wechat: () => ok(status), open_wechat_download: () => ok({opened: true}),
      get_features: () => ok({plugins: features, engine_online: true}),
      get_robot_core: () => ok(corePreviewDetail()),
      save_robot_core: (config, _revision, identity) => {
        const oldName = corePreviewIdentity.robot_name || "小妹";
        const name = String(identity?.robot_name || "小妹").trim();
        corePreviewConfig = structuredClone(config);
        const aliases = String(corePreviewConfig.wake_interaction?.alias || "").split("|").map((item) => item.trim()).filter((item) => item && ![oldName, name, "AI", "助手", "助理"].includes(item));
        corePreviewConfig.wake_interaction.alias = [name, ...aliases].join("|");
        corePreviewConfig.personality.ai_self_identity = String(corePreviewConfig.personality?.ai_self_identity || "").replaceAll(oldName, name).replaceAll("群聊里的熟人型助手", "群聊里的熟人型机器人").replaceAll("群聊助手", "群聊机器人");
        corePreviewIdentity = {robot_name: name, admin_id: String(identity?.admin_id || "").trim()};
        corePreviewRevision += 1;
        return ok({revision: String(corePreviewRevision), apply_state: "applied", brand: {display_name: name, short_name: name, persona_name: name, welcome_text: `你好，我是${name}。`, admin_id: corePreviewIdentity.admin_id}, message: "机器人名称与群聊身份已更新；管理员权限及底层初始人格重启机器人后生效。"});
      },
      request_robot_core: (method, endpoint, payload) => ok(corePreviewRequest(method, endpoint, payload)),
      get_feature: (id) => ok(previewFeatureDetail(id)),
      get_feature_page: () => ok({url: "data:text/html;charset=utf-8,%3Cstyle%3Ebody%7Bfont-family%3Asans-serif%3Bpadding%3A32px%3Bcolor%3A%23102a49%7D%3C%2Fstyle%3E%3Ch2%3E推特订阅管理（预览）%3C%2Fh2%3E%3Cp%3E正式版本将在此处加载实际订阅管理页。%3C%2Fp%3E", origin: window.location.origin}),
      request_feature_page: () => ok({status: "ok", data: {}}),
      get_groups: () => ok({account_id: "wxid_demo_account", source: "wechat_cache", stale: false, updated_at: new Date().toISOString(), total: 6, items: [
        {group_id: "51632940287@chatroom", name: "旅行交流群", member_count: 128},
        {group_id: "81263495012@chatroom", name: "短剧分享群", member_count: 76},
        {group_id: "22001983641@chatroom", name: "业主交流群", member_count: null},
        {group_id: "34902765188@chatroom", name: "周末羽毛球", member_count: 42},
        {group_id: "82765043912@chatroom", name: "同学交流群", member_count: 93},
        {group_id: "45871023498@chatroom", name: "社区团购通知", member_count: 210},
      ], message: "预览数据：正式版本从当前登录微信读取，刷新不会发送群消息。"}),
      get_group_members: (groupId) => ok({account_id: "wxid_demo_account", group_id: groupId, source: "wechat_cache", total: 3, truncated: false, items: [
        {user_id: "wxid_demo_01", wechat_id: "zhangsan_88", name: "张三", group_nickname: "张三｜3栋"},
        {user_id: "wxid_demo_02", wechat_id: "lisi_photo", name: "李四", group_nickname: "李四"},
        {user_id: "wxid_demo_03", name: "王五", group_nickname: ""},
      ]}),
      get_group_policy: (_accountId, groupId) => ok(previewPolicyOverview(groupId)),
      preview_group_policy: (_accountId, groupId, values) => ok({group_id: groupId, changed_fields: Object.keys(values || {}), impact: ["预览模式只影响当前群的机器人规则。"], will_send_messages: false}),
      save_group_policy: (_accountId, groupId, values) => { previewGroupPolicy = {...previewGroupPolicy, ...structuredClone(values), revision: `preview-policy-${Date.now()}`}; return ok(previewPolicyOverview(groupId).policy, "群聊策略已保存；未向群内发送消息。"); },
      get_contacts: () => ok({account_id: "wxid_demo_account", source: "wechat_cache", stale: false, updated_at: new Date().toISOString(), total: previewContacts.length, truncated: false, items: structuredClone(previewContacts), message: "预览数据：正式版本从当前登录微信通讯录读取，刷新不会发送消息。"}),
      get_contact_detail: (contactId) => { const item = previewContacts.find((contact) => contact.user_id === contactId); return ok({account_id: "wxid_demo_account", item: structuredClone(item || {}), items: item ? [structuredClone(item)] : [], total: item ? 1 : 0}); },
      get_contact_network_profile: (accountId, contactId) => { const item = previewContacts.find((contact) => contact.user_id === contactId); return ok({account_id: accountId, item: structuredClone(item || {}), items: item ? [structuredClone(item)] : [], total: item ? 1 : 0}); },
      get_private_policy: (_accountId, contactId) => ok(previewPrivatePolicyOverview(contactId)),
      save_private_policy: (_accountId, contactId, values) => { previewPrivatePolicy = {...previewPrivatePolicy, ...structuredClone(values), configured: true, revision: `preview-private-${Date.now()}`}; return ok(previewPrivatePolicyOverview(contactId).policy, "联系人可用功能已保存。"); },
      save_feature: (id, config) => { if (id === "feature-95a906d4b9173155") { corePreviewConfig = structuredClone(config); corePreviewRevision += 1; } else previewSavedConfigs.set(id, structuredClone(config)); return ok({revision: "preview-2", apply_state: "applied", message: "预览：已保存并确认生效，后续请求使用新设置。"}); }, reload_feature: () => ok({reloaded: true, message: "功能已经重新载入。"}), open_feature_center: () => ok({opened: true, message: "高级配置窗口正在打开。"}),
      get_conversations: (filters) => ok(previewConversations(filters)),
      get_group_qrcode: (accountId, groupId) => ok({account_id: accountId, group_id: groupId, image_data_url: ""}),
      get_messages: (conversationId, filters = {}) => {
        const search = String(filters.search || "").trim().toLocaleLowerCase("zh-CN");
        const matching = previewHistoryMessages.filter((message) => {
          if (message.conversation_id !== conversationId) return false;
          if (!search) return true;
          if (search.startsWith("wxid_")) return previewHistoryMatches(message, search);
          return [message.sender_name, message.wechat_id, message.group_nickname, message.content]
            .some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(search));
        });
        matching.forEach((message) => {
          const own = matching.filter((item) => item.role === message.role && item.sender_id === message.sender_id);
          message.sender_message_count = own.length;
          message.sender_last_timestamp = Math.max(...own.map((item) => item.timestamp));
        });
        if (filters.paged !== true) return ok(structuredClone(matching));
        const before = filters.before;
        const older = before ? matching.filter((message) => (
          Number(message.timestamp) < Number(before.timestamp)
          || (Number(message.timestamp) === Number(before.timestamp) && Number(message.id) < Number(before.id))
        )) : matching;
        const pageSize = Math.max(1, Math.min(Number(filters.page_size) || HISTORY_PAGE_SIZE, HISTORY_PAGE_SIZE));
        return ok({account_id: "wxid_demo_account", items: structuredClone(older.slice(-pageSize)), has_more: older.length > pageSize});
      },
      delete_conversation: () => ok({deleted: 34}), delete_history_before: () => ok({deleted: 120}), export_history: () => ok({path: "D:\\聊天记录.csv"}),
      get_schedules: () => ok({running: 0, paused: 0, items: []}),
      get_legacy_schedule_migrations: () => ok({items: []}),
      preview_legacy_schedule_migration: () => { throw new Error("请先选择属于当前账号的旧任务。"); },
      migrate_legacy_schedule: () => ok({pending: true, message: "预览模式不会停止机器人或迁移数据。"}),
      cancel_legacy_schedule_migration: () => ok({cancelled: true, message: "迁移确认已取消。"}),
      get_scheduled_task_context: () => ok({account_id: "wxid_demo_account", items: structuredClone([...previewTasks.values()]), capabilities: Object.entries(SCHEDULED_ACTIONS).map(([id, name]) => ({id, name, ready: true, message: "预览模式"})), command_catalog: Object.entries(SCHEDULED_ACTIONS).map(([id, display_name]) => ({id, display_name, action: id, status: "supported", parameters: [], dependencies: [], recipient_types: ["group", "private"], external_write: false, result: "预览内容", failure_policy: "按对象记录失败", reason: "", alternative: ""})).concat([{id: "checkin", display_name: "账号签到", action: null, status: "needs_condition", reason: "签到需要单独授权和重复执行保护。", alternative: "请先在对应功能页完成账号授权，再创建任务。"}]), command_definition_version: 1, groups: structuredClone(previewTaskGroups), contacts: previewContacts.filter((entry) => entry.contact_type === "微信好友").map((entry) => ({id: entry.user_id, name: entry.name})), online: true, runtime_ready: true, message: ""}),
      save_scheduled_task: (payload) => ok(previewTaskSave(payload)),
      delete_scheduled_task: (_account, id) => { previewTasks.delete(id); return ok({deleted: true}); },
      set_scheduled_task_enabled: (_account, id, enabled) => ok(previewTaskSave({...previewTasks.get(id), enabled, draft: false})),
      preview_scheduled_times: (schedule) => ok({items: previewTaskTimes(schedule), text: "请补齐有效的未来日期、星期和时间。"}),
      parse_scheduled_command: (command) => { const normalized = command.replace(/^\//, "").trim(); const match = normalized.match(/^(.+?)(明天|今日|今天)?天气$/); if (match) return ok({action: "weather", arguments: {city: match[1], day: match[2] === "明天" ? "tomorrow" : "today", output: "image"}, recognized: true, status: "supported", reason: "", alternative: ""}); if (/^(新闻|资讯|热点)$/.test(normalized)) return ok({action: "news", arguments: {count: 10}, recognized: true, status: "supported", reason: "", alternative: ""}); if (normalized === "短剧推荐") return ok({action: "short_drama", arguments: {media_type: "短剧", limit: 12}, recognized: true, status: "supported", reason: "", alternative: ""}); if (/^(签到|每日签到)$/.test(normalized)) return ok({recognized: true, status: "needs_condition", reason: "签到需要单独授权和重复执行保护。", alternative: "请先在对应功能页完成账号授权，再创建任务。"}); return ok({recognized: false, status: "unsupported", reason: "没有识别出可定时化的命令。", alternative: "请直接选择定时任务中的内容，或改为固定文字提醒。"}); },
      preview_scheduled_task: (payload) => {
        if (payload.action === "short_drama") return ok({kind: "link_card", title: `最新${payload.arguments.media_type || "短剧"}推荐｜${payload.arguments.limit || 12}部`, description: "定时推荐 · 点击查看封面并选择播放", url: "https://player.example/recommendations/preview", image_url: "https://player.example/cover.jpg"});
        if (payload.action !== "weather") return ok({kind: "text", text: payload.action === "text" ? payload.arguments.text : `预览示意：${SCHEDULED_ACTIONS[payload.action]}，正式执行时获取最新内容。`});
        const canvas = document.createElement("canvas"); canvas.width = 520; canvas.height = 230;
        const ctx = canvas.getContext("2d"); ctx.fillStyle = "#eef4ff"; ctx.fillRect(0, 0, 520, 230); ctx.fillStyle = "#10243d"; ctx.font = "bold 28px Microsoft YaHei UI"; ctx.fillText(`${payload.arguments.city || "城市"} · 天气卡片`, 28, 64); ctx.font = "20px Microsoft YaHei UI"; ctx.fillText(payload.arguments.day === "tomorrow" ? "执行日的明天天气" : "执行当天的天气", 28, 112); ctx.fillStyle = "#536982"; ctx.font = "18px Microsoft YaHei UI"; ctx.fillText("样式预览，不代表实时天气", 28, 181);
        return ok({kind: "image", image_data_url: canvas.toDataURL("image/png")});
      },
      test_scheduled_task: (payload) => ok({status: "submitted", message: `预览：测试消息已提交给 ${payload.test_target?.id || "所选聊天"}，不会实际发送。`, target: payload.test_target}),
      get_scheduled_task_runs: () => ok({items: []}),
      set_schedule_enabled: (_id, enabled) => ok({updated: true, enabled}), update_schedule_time: () => ok({updated: true}), delete_schedule: () => ok({deleted: true}),
      get_activity: () => ok([
        {id: "demo-model-401", code: "16002", title: "模型身份验证失败", level: "error", source: "engine-runtime", source_name: "回复引擎", message: "调用聊天模型失败：HTTP 401 — 身份验证未通过。", detail: "AuthenticationError: HTTP 401\n  at provider.py:42\nAuthorization: ***", operation: "生成机器人回复", count: 3, suggestion: "请检查该模型服务的 API Key。"},
        {id: "demo-send-unknown", code: "17002", title: "消息发送结果待确认", level: "warning", source: "wechat-connection", source_name: "微信连接", message: "发送请求已提交，等待回执超时（30 秒）。", detail: "TimeoutError: response not received", operation: "发送文本消息", count: 1, suggestion: "请先检查聊天记录，避免重复发送。"},
        {id: "demo-ready", code: "", title: "机器人功能已就绪", level: "info", source: "manager", source_name: "管理器", message: "启动检查完成。", detail: "CORE_READY", operation: "启动机器人", count: 1, suggestion: ""},
      ].map((item, index) => ({...item, time: new Date(Date.now() - index * 60000).toISOString(), timestamp: Date.now() / 1000 - index * 60, report: `预览示例 · 机器人问题报告\n软件版本：0.9.9\n事件编号：${item.id}\n错误码：${item.code}\n标题：${item.title}\n来源：${item.source_name}\n具体错误：${item.message}\n技术详情：${item.detail}\n处理建议：${item.suggestion}`}))),
      record_ui_error: () => ok({saved: true, event_id: "preview-error"}),
      export_activity_diagnostics: () => ok({file_name: "robot-diagnostics-preview.json", path: "预览模式，不实际生成文件"}),
      get_response_traces: () => ok([{time: new Date(Date.now() - 18000).toISOString(), title: "群聊点名", duration_text: "3.4 秒", stages: [{label: "点名快路径", status: "success", detail: "已跳过额外判断", duration_text: "0.4 秒"}, {label: "长期记忆", status: "skipped", detail: "普通寒暄无需回忆", duration_text: "<0.1 秒"}, {label: "精简情感", status: "success", detail: "已加入好感度与心情", duration_text: "0.1 秒"}, {label: "生成回复", status: "success", duration_text: "2.6 秒"}, {label: "文字发送", status: "success", duration_text: "0.3 秒"}]}, {time: new Date(Date.now() - 82000).toISOString(), title: "询问上次内容", duration_text: "5.1 秒", stages: [{label: "点名快路径", status: "success", duration_text: "0.4 秒"}, {label: "长期记忆 3 条", status: "success", detail: "命中明确回忆说法", duration_text: "0.8 秒"}, {label: "精简情感", status: "success", duration_text: "0.1 秒"}, {label: "生成回复", status: "success", duration_text: "3.5 秒"}, {label: "文字发送", status: "success", duration_text: "0.3 秒"}]}]),
      get_experience: () => ok(structuredClone(previewExperience)),
      get_onboarding_readiness: () => ok(previewOnboardingReadiness()),
      get_web_search_settings: () => ok(structuredClone(previewWebSearch)),
      save_web_search_settings: (values) => {
        const selected = previewWebSearch.providers[values.provider];
        if (!selected) return Promise.resolve({ok: false, error: {message: "预览请求无效。"}});
        if (values.clear_key) { selected.configured = false; selected.key_count = 0; }
        else if (values.key) { selected.configured = true; selected.key_count = 1; }
        previewWebSearch = {...previewWebSearch, enabled: Boolean(values.enabled), provider: values.provider, link_enabled: Boolean(values.link_enabled)};
        return ok({...structuredClone(previewWebSearch), apply_state: "applied"}, "联网搜索设置已热更新，下一条消息使用新配置。");
      },
      save_memory_settings: (values) => ok({...values, apply_state: "applied"}, "长期记忆设置已热更新，下一条消息立即使用。"),
      save_emotion_settings: (values) => ok({...values, apply_state: "applied"}, "情感设置已热更新，下一条消息立即使用。"),
      preview_experience: () => ok({text: "小妹：这是本机预览回复。", uses_recent_context: true, uses_long_term_memory: true, uses_emotion: true, uses_expression: false, uses_voice: false, will_send: false, message: "预览只在管理器中显示，不会发送到微信。"}),
      complete_onboarding: () => { previewExperience.onboarding_complete = true; return ok({onboarding_complete: true}, "基础设置已完成。"); },
      get_providers: () => ok(structuredClone(previewProviders)),
      save_provider: (values) => {
        const preset = previewPresets.find((item) => item.id === values.preset) || previewPresets.at(-1);
        const source = values.preset === "deepseek" ? "deepseek" : values.preset === "custom" ? "custom" : String(values.preset || "custom");
        const id = `${source}/${values.model}`;
        const original = previewProviders.find((item) => item.id === values.provider_id);
        const serviceKind = values.service_kind || preset?.service_kind || "chat";
        const makeDefault = serviceKind === "chat" && (
          !previewProviders.some((item) => item.service_kind === "chat")
          || Boolean(original?.is_default)
        );
        previewProviders = previewProviders
          .filter((item) => item.id !== values.provider_id && item.id !== id)
          .map((item) => makeDefault ? {...item, is_default: false} : item);
        const modalities = inferProviderModalities(serviceKind, values.model, values.modalities);
        const saved = {id, name: preset?.name || values.model, api_base: values.api_base, model: values.model, service_kind: serviceKind, modalities, modality_label: providerCapabilityLabel(serviceKind), configured: true, api_key_configured: true, is_default: makeDefault, is_recommended: false, thinking_supported: providerThinkingSupported(values.api_base, values.model), thinking_enabled: values.thinking_enabled !== false, apply_state: "applied"};
        previewProviders.unshift(saved);
        return ok(saved, makeDefault ? "回复模型已热更新并立即生效。" : "模型服务已保存，当前回复模型保持不变。");
      },
      set_default_provider: (id) => { previewProviders = previewProviders.map((item) => ({...item, is_default: item.id === id})); return ok({id, apply_state: "applied"}, "默认回复模型已切换并立即生效。"); },
      set_provider_thinking: (id, enabled) => { previewProviders = previewProviders.map((item) => item.id === id ? {...item, thinking_enabled: Boolean(enabled)} : item); return ok({id, thinking_enabled: Boolean(enabled), apply_state: "applied"}, "思考模式已热更新，下一条消息立即生效。"); },
      test_provider: () => ok({ok: true}, "模型服务连接正常。"),
      discover_provider_models: () => ok({items: ["deepseek-flash", "glm-4.6v", "gemini-3.8-flash", "qwen-plus", "doubao-seed-2-0-lite-260215"], total: 5}, "已读取 5 个可用模型。"),
      delete_provider: (id) => { previewProviders = previewProviders.filter((item) => item.id !== id); return ok({id}, "模型服务已删除并立即生效。"); },
      get_maintenance: () => ok({preview: {file_count: 18, uncompressed_size: 39636172, can_create: true}, backups: [{id: "robot-backup-preview.zip", file_name: "robot-backup-preview.zip", size: 12740121, modified_at: new Date(Date.now() - 86400000).toISOString()}], diagnostics: {summary: {error_count: 0, warning_count: 1, check_count: 14}}}),
      create_backup: () => ok({id: "robot-backup-new.zip"}, "设置、人格和长期记忆已备份。"),
      validate_restore: (id) => ok({valid: true, backup_id: id}), restore_backup: (id) => ok({backup_id: id}, "备份已经恢复，并已保留恢复前副本。"),
      save_theme: (theme) => ok({theme}),
      save_skin: (skin) => ok({skin}),
      get_diagnostics: () => ok({summary: {error_count: 0, warning_count: 1, check_count: 14}}), export_diagnostics: () => ok({file_name: "robot-diagnostics-preview.json"}, "安全诊断摘要已导出。"),
      open_install_folder: () => ok({opened: true}), open_uninstaller: () => ok({opened: true}),
      check_update: () => ok({enabled: true, current: true, release: null}), install_update: (version) => ok({accepted: true, version}),
    };
  }

  return {
    previewApi,
  };
};
