(() => {
  const $app = document.querySelector("#app");
  const query = new URLSearchParams(location.search);
  const screen = query.get("screen") || "run-running";

  const paths = {
    home: '<path d="M3 10.5 12 3l9 7.5v9A1.5 1.5 0 0 1 19.5 21h-15A1.5 1.5 0 0 1 3 19.5zM9 21v-7h6v7"/>',
    grid: '<rect x="4" y="4" width="6" height="6" rx="1"/><rect x="14" y="4" width="6" height="6" rx="1"/><rect x="4" y="14" width="6" height="6" rx="1"/><rect x="14" y="14" width="6" height="6" rx="1"/>',
    users: '<path d="M16 20v-1.5a4 4 0 0 0-4-4H7a4 4 0 0 0-4 4V20M9.5 10.5a3.5 3.5 0 1 0 0-7 3.5 3.5 0 0 0 0 7M15 4.3a3.5 3.5 0 0 1 0 6.4M17 14.8a4 4 0 0 1 4 4V20"/>',
    chat: '<path d="M4 5h16v12H8l-4 4zm4 4h8M8 13h5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    list: '<path d="M9 6h11M9 12h11M9 18h11M4 6h.01M4 12h.01M4 18h.01"/>',
    settings: '<circle cx="12" cy="12" r="3"/><path d="M19 13.5v-3l-2-.7-.6-1.4.9-1.9-2.1-2.1-1.9.9-1.4-.6-.7-2h-3l-.7 2-1.4.6-1.9-.9-2.1 2.1.9 1.9-.6 1.4-2 .7v3l2 .7.6 1.4-.9 1.9 2.1 2.1 1.9-.9 1.4.6.7 2h3l.7-2 1.4-.6 1.9.9 2.1-2.1-.9-1.9.6-1.4z"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5M6.2 8a7 7 0 0 1 11.5-1L20 12M4 12l2.3 5a7 7 0 0 0 11.5-1"/>',
    chevron: '<path d="m8 10 4 4 4-4"/>',
    chevronRight: '<path d="m9 6 6 6-6 6"/>',
    play: '<path d="m9 6 9 6-9 6z"/>',
    stop: '<rect x="7" y="7" width="10" height="10" rx="1"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="m16 16 5 5"/>',
    download: '<path d="M12 3v12m-5-5 5 5 5-5M4 20h16"/>',
    copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
    alert: '<path d="M12 3 2.5 20h19zM12 9v4M12 17h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v6M12 7h.01"/>',
    check: '<path d="m5 12 4 4L19 6"/>',
    calendar: '<rect x="3" y="5" width="18" height="16" rx="2"/><path d="M16 3v4M8 3v4M3 10h18"/>',
    trash: '<path d="M4 7h16M9 7V4h6v3m3 0-1 14H7L6 7m4 4v6m4-6v6"/>',
    folder: '<path d="M3 6h7l2 2h9v11H3z"/>',
    help: '<circle cx="12" cy="12" r="9"/><path d="M9.7 9a2.5 2.5 0 1 1 4.1 1.9c-1.2.9-1.8 1.3-1.8 2.6M12 17h.01"/>',
    shield: '<path d="M12 3 4.5 6v5.5c0 4.4 3 7.8 7.5 9.5 4.5-1.7 7.5-5.1 7.5-9.5V6z"/><path d="m8.5 12 2.2 2.2 4.8-5"/>',
    close: '<path d="m6 6 12 12M18 6 6 18"/>',
  };

  function icon(name, label = "") {
    return `<svg viewBox="0 0 24 24" aria-hidden="true" ${label ? `data-label="${label}"` : ""}>${paths[name] || paths.info}</svg>`;
  }

  const nav = [
    ["home", "运行中心", "run-running", "home"],
    ["features", "功能中心", "feature-config", "grid"],
    ["groups", "群聊管理", "groups-policy", "users"],
    ["history", "聊天记录", "history", "chat"],
    ["schedules", "定时任务", "schedules", "clock"],
    ["activity", "运行记录", "activity", "list"],
    ["settings", "设置", "settings-model", "settings"],
  ];

  const screenMeta = {
    "design-system": ["home", "设计系统"],
    "run-running": ["home", "运行中心"],
    "run-stopped": ["home", "运行中心"],
    "run-environment-error": ["home", "运行中心"],
    "feature-overview": ["features", "功能中心"],
    "feature-config": ["features", "功能中心"],
    "feature-advanced": ["features", "功能中心"],
    "groups": ["groups", "群聊管理"],
    "groups-policy": ["groups", "群聊管理"],
    "groups-members": ["groups", "群聊管理"],
    "history": ["history", "聊天记录"],
    "history-empty": ["history", "聊天记录"],
    "schedules": ["schedules", "定时任务"],
    "activity": ["activity", "运行记录"],
    "settings-model": ["settings", "设置"],
    "settings-persona": ["settings", "设置"],
    "settings-memory": ["settings", "设置"],
    "settings-system": ["settings", "设置"],
  };

  const modalScreens = {
    "modal-onboarding": "onboarding",
    "modal-unsaved": "unsaved",
    "modal-group-picker": "group-picker",
    "modal-history-delete": "history-delete",
    "modal-schedule-edit": "schedule-edit",
    "modal-schedule-delete": "schedule-delete",
    "modal-provider-delete": "provider-delete",
    "modal-backup-create": "backup-create",
    "modal-backup-restore": "backup-restore",
    "modal-history-clean": "history-clean",
    "modal-update-available": "update-available",
    "modal-update-progress": "update-progress",
  };

  const dropdownScreens = {
    "dropdown-service-type": "service-type",
    "dropdown-model": "model",
    "dropdown-reply-mode": "reply-mode",
    "dropdown-persona-tone": "persona-tone",
    "dropdown-feature-option": "feature-option",
    "dropdown-date": "date",
  };

  function shell(active, title, body, options = {}) {
    const running = options.running !== false;
    const version = "v0.9.0 设计提案";
    return `
      <div class="app-shell">
        <aside class="sidebar">
          <div class="brand"><div class="brand-mark">姜</div><div class="brand-copy"><strong>姜小妹机器人</strong><span>微信机器人管理</span></div></div>
          <div class="nav-label">机器人管理</div>
          <nav class="main-nav" aria-label="主导航">
            ${nav.map(([id, label, target, iconName]) => `<button class="nav-item ${active === id ? "active" : ""}" data-screen="${target}">${icon(iconName)}<span>${label}</span></button>`).join("")}
          </nav>
          <div class="sidebar-help"><strong>第一次使用？</strong><span>跟着运行中心的步骤操作，不需要理解技术名词。</span></div>
          <div class="sidebar-footer"><div class="health-mini"><i class="dot ${running ? "" : "gray"}"></i><span>${running ? "机器人在线" : "机器人已停止"}</span></div><span>${version}</span></div>
        </aside>
        <main class="main">
          <header class="topbar">
            <div class="crumb"><span>控制台</span>${icon("chevronRight")}<strong>${title}</strong></div>
            <div class="top-actions">
              <button class="button ghost small">${icon("refresh")}刷新状态</button>
              <span class="badge ${running ? "success" : ""}"><i class="mini-dot"></i>${running ? "机器人可用" : "机器人已停止"}</span>
            </div>
          </header>
          <div class="page-wrap">${body}</div>
        </main>
      </div>`;
  }

  function heading(kicker, title, copy, action = "") {
    return `<div class="page-heading"><div><span class="eyebrow">${kicker}</span><h1>${title}</h1><p>${copy}</p></div>${action ? `<div class="heading-side">${action}</div>` : ""}</div>`;
  }

  function badge(text, style = "") {
    return `<span class="badge ${style}">${style === "success" || style === "warning" || style === "danger" ? '<i class="mini-dot"></i>' : ""}${text}</span>`;
  }

  function selectBox(value, dropdown = "") {
    return `<div class="select">${value}${icon("chevron")}</div>${dropdown}`;
  }

  function field(label, control, hint = "") {
    return `<label class="field"><span>${label}</span>${control}${hint ? `<small>${hint}</small>` : ""}</label>`;
  }

  const featureItems = [
    ["群聊上下文", "群聊常用 · 已启用", true],
    ["长期记忆", "群聊常用 · 智能召回就绪", true],
    ["点歌", "群聊常用 · 已启用", true],
    ["链接解析", "群聊常用 · 已启用", true],
    ["群聊分析", "群主管理用 · 按需启用", false, true],
    ["常用指令", "群聊常用 · 已启用", true],
    ["群成员变动通知", "群主管理用 · 按需启用", false],
    ["复读功能", "群聊互动 · 按需启用", false],
  ];

  function featureCatalog() {
    return `<aside class="catalog panel">
      <div class="catalog-top">
        <div class="search">${icon("search")}<span>搜索功能</span></div>
        <div class="segmented"><span class="active">常用</span><span>按需</span><span>进阶</span><span>全部</span></div>
      </div>
      <div class="catalog-summary">11 / 22 项功能 · 设置可立即应用</div>
      <div class="feature-list">${featureItems.map(([name, meta, on, selected]) => `<div class="feature-item ${selected ? "active" : ""}"><div><strong>${name}</strong><small>${meta}</small></div><i class="status-pin ${on ? "" : "off"}"></i></div>`).join("")}</div>
    </aside>`;
  }

  function runPage(mode = "running") {
    const config = mode === "running" ? {
      title: "机器人可用", copy: "微信连接和全部功能运行正常。现在可以在已允许的群里使用机器人。", orbit: "", power: "停止", powerClass: "", powerIcon: "stop", account: "已登录：wxid_demo_account", running: true,
    } : mode === "stopped" ? {
      title: "机器人已停止", copy: "微信环境已经准备好。点击“启动”后，软件会依次连接微信并检查消息能力。", orbit: "stopped", power: "启动", powerClass: "start", powerIcon: "play", account: "上次账号：wxid_demo_account", running: false,
    } : {
      title: "请先安装兼容微信", copy: "没有检测到可用的微信客户端。完成上方操作后再启动机器人。", orbit: "error", power: "暂不可用", powerClass: "disabled", powerIcon: "alert", account: "尚未连接", running: false,
    };
    const alert = mode === "error" ? `<div class="alert"><div class="alert-icon">${icon("alert")}</div><div><h3>未检测到兼容的微信客户端</h3><p>请安装推荐版本，或选择本机已有的 Weixin.exe。重新检测不会启动机器人。</p></div><div class="button-row"><button class="button primary small">下载安装微信</button><button class="button ghost small">选择本机微信</button><button class="button ghost small">重新检测</button></div></div>` : "";
    const stepState = mode === "running" ? ["已通过", "已就绪", "已连接"] : mode === "stopped" ? ["已通过", "未启动", "未连接"] : ["需要处理", "等待环境", "等待环境"];
    const stepClasses = mode === "running" ? ["", "", ""] : mode === "stopped" ? ["", "pending", "pending"] : ["error", "pending", "pending"];
    const body = `<section class="page">
      ${heading("今日运行", "机器人控制台", "先看结果，再按步骤处理；不需要接触命令或后台组件。", `<div class="account-chip"><span class="eyebrow">当前账号</span><strong>${config.account}</strong></div>`)}
      ${alert}
      <div class="run-grid">
        <section class="panel run-control">
          <div class="panel-head"><div><span class="eyebrow">实时状态</span><h2>启动与停止</h2></div><span class="summary-line">刚刚检查</span></div>
          <div class="run-control-body"><div class="orbit ${config.orbit}"><button class="power ${config.powerClass}">${icon(config.powerIcon)}<span>${config.power}</span></button></div><div class="state-copy"><span class="state-label">机器人状态</span><h2>${config.title}</h2><p>${config.copy}</p><div class="state-note">${icon("info")}<span>${mode === "running" ? "停止机器人时，若微信仍在运行，软件会提示你先关闭微信，不会强制结束客户端。" : mode === "stopped" ? "启动过程通常需要 10–30 秒；需要登录微信时会在这里提示下一步。" : "推荐优先使用“下载安装微信”，适合第一次配置的用户。"}</span></div></div></div>
        </section>
        <section class="panel"><div class="panel-head"><div><span class="eyebrow">启动进度</span><h2>连接台账</h2></div>${badge(mode === "running" ? "全部通过" : mode === "stopped" ? "可以启动" : "需要处理", mode === "running" ? "success" : mode === "error" ? "warning" : "primary")}</div>
          <ol class="step-list">
            ${[["微信环境","安装路径与兼容版本"],["机器人功能","基础服务与功能加载"],["消息连接","账号、通信与心跳"]].map((item,i)=>`<li class="step ${stepClasses[i]}"><span class="step-index">${i+1}</span><div><strong>${item[0]}</strong><small>${item[1]}</small></div><em>${stepState[i]}</em></li>`).join("")}
          </ol>
        </section>
      </div>
      <section class="panel signal-grid"><article class="signal"><span>微信客户端</span><strong>${mode === "error" ? "未找到" : "版本兼容"}</strong><small>${mode === "error" ? "等待选择安装位置" : "4.1.8.27 · 正在运行"}</small></article><article class="signal"><span>机器人功能</span><strong>${mode === "running" ? "运行正常" : "未启动"}</strong><small>由软件统一管理</small></article><article class="signal"><span>消息通信</span><strong>${mode === "running" ? "通信正常" : "未连接"}</strong><small>连接和心跳双重确认</small></article></section>
    </section>`;
    return shell("home", "运行中心", body, {running: config.running});
  }

  function featurePage(tab = "basic") {
    const overview = tab === "overview";
    const advanced = tab === "advanced";
    const form = overview ? `<div class="empty"><div class="empty-symbol">析</div><strong>群聊分析适合群主和社群运营者</strong><p>它会把一天的群聊整理成可读日报。启用前先选择目标群、生成时间和日报样式。</p><button class="button primary">开始配置</button></div>` : advanced ? `
      <div class="advanced-banner">${icon("alert")}<div><strong>专业设置会保留当前所有值</strong><br>不确定用途时保持原值即可；修改后仍需点击底部“保存并应用”。</div></div>
      <div class="form-card"><h3>生成与并发</h3><div class="form-grid three">${field("最多并行任务", '<div class="input">2</div>', "建议 1–3")}${field("请求超时（秒）", '<div class="input">180</div>')}${field("失败重试次数", '<div class="input">2</div>')}</div></div>
      <div class="form-card"><h3>提示词与输出</h3><div class="form-grid">${field("日报系统提示", '<div class="textarea">你是一名社群运营助理，请基于当天群聊生成清晰、克制的日报……</div>')}${field("忽略的消息类型", '<div class="textarea">system, notice, recalled</div>', "每行一个类型")}</div></div>
      <div class="form-card"><h3>兼容参数</h3><div class="check-grid"><div class="check"><span class="checkbox on">✓</span><span>保留消息发送者昵称</span></div><div class="check"><span class="checkbox">✓</span><span>在报告中显示群标识</span></div></div></div>` : `
      <div class="form-card"><h3>日报设置</h3><div class="form-grid">${field("报告样式", selectBox("手账风（示例样式）"), "适合直接分享至群聊")}${field("统计最近几天", '<div class="input">1</div>', "通常保持 1 天；需要周报时可改为 7 天")}${field("最少消息数", '<div class="input">200</div>', "少于该数量时不生成日报")}${field("每天生成时间", '<div class="input">21:30</div>', "会显示在定时任务中心")}</div></div>
      <div class="form-card"><h3>发送到哪些群</h3><div class="check-grid"><div class="check"><span class="checkbox on">✓</span><span>旅行交流群<br><small>51632940287@chatroom</small></span></div><div class="check"><span class="checkbox">✓</span><span>短剧分享群<br><small>81263495012@chatroom</small></span></div></div><div class="inline-row" style="margin-top:10px"><button class="button secondary small">从群聊列表添加</button><span class="summary-line">保存前不会发送消息</span></div></div>`;
    const detail = `<section class="feature-detail panel" style="position:relative">
      <div class="feature-title"><div><span class="eyebrow">功能设置</span><h2>群聊分析</h2><p>把一天群聊整理成日报</p></div><div class="switch-line"><span class="toggle ${overview ? "" : "on"}"></span>${overview ? "已停用" : "已启用"}</div></div>
      <div class="assessment"><div>${badge("群主管理用", "primary")} ${badge("已适配本机聊天记录")}</div><span>建议按需启用；数据按当前机器人账号和群聊双重隔离。</span></div>
      <div class="guide"><div><strong>从这里开始</strong><p>先改常用项，其余保持当前设置。每项都提供普通用户能理解的说明。</p></div><ol><li>选择要生成日报的群聊</li><li>确认生成时间和日报样式</li><li>保存后在定时任务中心查看</li></ol></div>
      <div class="feature-tabs"><span class="${!overview && !advanced ? "active" : ""}">常用设置</span><span>模型设置</span><span class="${advanced ? "active" : ""}">专业设置（全部）</span></div>
      <div class="form-scroll">${form}</div>
      ${overview ? "" : `<div class="sticky-save"><span>修改会先保存，再确认运行中的机器人已读取。</span><div class="button-row"><button class="button ghost">重新读取</button><button class="button primary">保存并应用</button></div></div>`}
    </section>`;
    const body = `<section class="page">${heading("功能管理", "功能中心", "每项功能都可以单独启停，并明确说明适合谁、需要准备什么。", `<button class="button ghost">${icon("refresh")}检查准备情况</button>`)}<div class="two-col">${featureCatalog()}${detail}</div></section>`;
    return shell("features", "功能中心", body);
  }

  function groupRows(selected = false) {
    const rows = [
      ["旅", "旅行交流群", "51632940287@chatroom", "128", true],
      ["短", "短剧分享群", "81263495012@chatroom", "76", false],
      ["业", "业主交流群", "66547231890@chatroom", "—", false],
      ["周", "周末羽毛球", "34902765188@chatroom", "42", false],
      ["同", "同学交流群", "82765043912@chatroom", "93", false],
      ["团", "社区团购通知", "45871023498@chatroom", "210", false],
    ];
    return rows.map((r,i)=>`<tr class="${selected && i===0 ? "selected" : ""}"><td><div class="cell-title"><span class="avatar ${i%3===1?"blue":i%3===2?"purple":""}">${r[0]}</span><div><strong>${r[1]}</strong><small>${i===0?"机器人已启用":"未单独设置"}</small></div></div></td><td class="code">${r[2]}</td><td>${r[3]}</td><td><div class="button-row"><button class="button ghost small">复制</button><button class="button secondary small">${selected&&i===0?"正在查看":"查看"}</button></div></td></tr>`).join("");
  }

  function groupInspector(mode = "empty") {
    if (mode === "empty") return `<aside class="panel inspector"><div class="empty"><div class="empty-symbol">群</div><strong>选择一个群聊</strong><p>可以查看群标识、成员，以及设置机器人在这个群里的回复规则。</p></div></aside>`;
    const members = mode === "members";
    return `<aside class="panel inspector" style="position:relative">
      <div class="inspector-hero"><div class="inline-row"><span class="avatar">旅</span><div><span class="eyebrow">当前群聊</span><h2>旅行交流群</h2><p>51632940287@chatroom · 128 位成员</p></div></div></div>
      <div class="inspector-scroll">
        ${members ? `<div class="panel-head" style="padding:0 0 12px;border:0"><div><h3>群成员</h3><p>按需读取，最多展示 500 人</p></div><button class="button ghost small">重新读取</button></div><div class="member-grid">${[["张","张三｜3栋","wxid_zhang"],["李","李四","wxid_lisi"],["王","王老师","wxid_wang"],["陈","陈女士","wxid_chen"],["周","周末出发","wxid_zhou"],["赵","赵同学","wxid_zhao"]].map((m,i)=>`<div class="member"><span class="avatar ${i%2?"blue":"purple"}">${m[0]}</span><div><strong>${m[1]}</strong><small>${m[2]}</small></div></div>`).join("")}</div><div class="modal-note">群主、管理员和头像等微信接口未提供的信息保持“未知”，不会推断。</div>` : `<div class="rule"><div class="rule-head"><div><strong>在本群使用机器人</strong><small>关闭后不回复，也不执行主动任务</small></div><span class="toggle on"></span></div></div>
        <div class="rule">${field("回复方式", selectBox("只在被点名时回复（推荐）"), "适合新群，避免机器人频繁插话")}</div>
        <div class="rule"><div class="rule-head"><div><strong>允许主动消息</strong><small>当前关闭；定时任务不会主动发到本群</small></div><span class="toggle"></span></div></div>
        <div class="rule"><div class="form-grid">${field("安静时段", '<div class="input">22:30 — 08:00</div>')}${field("每日主动消息上限", '<div class="input">3 条</div>')}</div></div>
        <div class="rule"><div class="rule-head"><div><strong>本群允许的功能</strong><small>未限制：使用所有已启用功能</small></div><button class="button ghost small">选择功能</button></div></div>
        <div class="inline-row" style="margin-top:12px"><button class="button secondary">查看群成员</button><span class="summary-line">只读取，不发送群消息</span></div>`}
      </div>
      <div class="sticky-save"><span>${members ? "成员列表不会写入配置。" : "在线保存会立即应用到当前群。"}</span><div class="button-row">${members?'<button class="button ghost">返回群规则</button>':'<button class="button primary">保存群聊规则</button>'}</div></div>
    </aside>`;
  }

  function groupsPage(mode = "empty", dropdown = false) {
    const selected = mode !== "empty";
    const inspector = dropdown ? groupInspector("policy").replace(selectBox("只在被点名时回复（推荐）"), selectBox("只在被点名时回复（推荐）", `<div class="dropdown"><div class="dropdown-option active"><span>只在被点名时回复</span><span class="recommend">推荐</span></div><div class="dropdown-option"><span>相关话题可自然参与</span><small>适合活跃小群</small></div><div class="dropdown-option"><span>不回复</span><small>仅保留定时任务</small></div></div>`)) : groupInspector(mode);
    const body = `<section class="page">${heading("群主管理", "群聊管理", "从当前登录微信读取；查看和刷新都不会向群里发送消息。", `<button class="button secondary">${icon("refresh")}刷新群列表</button>`)}
      <div class="callout">已读取当前账号的 6 个群聊，更新时间 09:45:18。账号变化时会自动清空旧列表，避免串号。</div>
      <div class="directory-layout"><section class="panel directory-panel"><div class="directory-toolbar"><div class="search">${icon("search")}<span>搜索群名称或群标识</span></div><div><strong style="font-size:11px">共 6 个群聊</strong><div class="summary-line">刚刚更新</div></div></div><table class="table"><thead><tr><th style="width:34%">群名称</th><th style="width:31%">群标识</th><th style="width:12%">成员数</th><th style="width:23%">操作</th></tr></thead><tbody>${groupRows(selected)}</tbody></table></section>${inspector}</div>
    </section>`;
    return shell("groups", "群聊管理", body);
  }

  function historyPage(empty = false, dateDropdown = false) {
    const conversations = empty ? "" : [
      ["旅","旅行交流群","51632940287@chatroom","张三｜3栋：帮我找一下今天更新的短剧","09:45",""],
      ["张","张三","wxid_zhangsan","明天杭州会下雨吗？","08:45","blue"],
      ["短","短剧分享群","81263495012@chatroom","李四｜摄影：收到，这样看起来很方便","07:45",""],
      ["业","业主交流群","66547231890@chatroom","暂无本机消息","—","purple"],
      ["周","周末羽毛球","34902765188@chatroom","暂无本机消息","—",""]
    ].map((c,i)=>`<div class="conversation ${i===0?"active":""}"><span class="avatar ${c[5]}">${c[0]}</span><div class="conversation-copy"><strong>${c[1]}</strong><div class="conversation-id">${c[2]}</div><div class="conversation-preview">${c[3]}</div></div><time>${c[4]}</time></div>`).join("");
    const calendar = dateDropdown ? `<div class="calendar"><div class="calendar-head"><button class="icon-button" style="width:28px;height:28px">‹</button><span>2026 年 9 月</span><button class="icon-button" style="width:28px;height:28px">›</button></div><div class="calendar-week"><span>一</span><span>二</span><span>三</span><span>四</span><span>五</span><span>六</span><span>日</span></div><div class="calendar-days">${[31,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24,25,26,27,28,29,30,1,2,3,4].map((d,i)=>`<span class="${i===0||i>30?"muted":i===10?"active":i>3&&i<10?"range":""}">${d}</span>`).join("")}</div><div class="modal-actions" style="margin-top:10px"><button class="button ghost small">清除</button><button class="button primary small">确定</button></div></div>` : "";
    const messages = empty ? `<div class="empty"><div class="empty-symbol">聊</div><strong>还没有符合条件的聊天</strong><p>可以清除日期范围或切换“全部”，群聊目录中没有本机消息的群仍会显示。</p><button class="button secondary">清除筛选</button></div>` : `
      <div class="message">
        <div class="message-avatar"><span class="avatar blue">张</span><i class="avatar-presence"></i></div>
        <div class="message-card"><div class="message-identity"><div class="identity-main"><strong>张三｜3栋</strong><span class="identity-tag">群昵称</span><time>09:42:19</time></div><div class="identity-id">wxid_zhangsan</div></div><div class="bubble">帮我找一下今天更新的短剧</div></div>
      </div>
      <div class="message mine">
        <div class="message-card"><div class="message-identity"><div class="identity-main"><time>09:42:39</time><span class="identity-tag">机器人</span><strong>姜小妹</strong></div><div class="identity-id">wxid_demo_account</div></div><div class="bubble">找到了 8 部今天更新的内容，我已经整理成播放页发到群里。</div></div>
        <div class="message-avatar"><span class="avatar">姜</span><i class="avatar-presence"></i></div>
      </div>
      <div class="message">
        <div class="message-avatar"><span class="avatar purple">李</span></div>
        <div class="message-card"><div class="message-identity"><div class="identity-main"><strong>李四｜摄影</strong><span class="identity-tag">群昵称</span><time>09:43:19</time></div><div class="identity-id">wxid_lisi</div></div><div class="bubble">收到，这样看起来很方便</div></div>
      </div>`;
    const body = `<section class="page">${heading("本机数据", "聊天记录", "这是便于查看的本机副本；删除不会影响微信原记录或长期记忆。", `<button class="button secondary">${icon("download")}导出记录</button>`)}
      <section class="panel history-filters"><div class="segmented" style="margin:0"><span class="active">全部</span><span>个人聊天</span><span>群聊</span></div><div class="search">${icon("search")}<span>搜索昵称、微信号或内容</span></div><div class="field" style="position:relative"><div class="date-box"><span>从 2026/09/01</span>${icon("calendar")}</div>${calendar}</div><div class="date-box"><span>到 2026/09/10</span>${icon("calendar")}</div><button class="button ghost">筛选</button></section>
      <div class="summary-line">${empty ? "0 个聊天 · 当前筛选没有结果" : "5 个聊天 · 238 条消息 · 个人消息 34 · 群聊消息 204"}</div>
      <section class="panel history-layout"><aside class="conversation-list">${conversations || `<div class="empty" style="min-height:300px"><strong>没有聊天</strong><p>调整筛选条件后再试</p></div>`}</aside><div class="message-pane"><header class="message-head"><div class="message-head-main">${empty?"":'<span class="avatar">旅</span>'}<div class="message-head-copy"><strong>${empty?"没有选中的聊天":"旅行交流群"}</strong><span>${empty?"消息内容会显示在这里":"51632940287@chatroom"}</span></div></div>${empty?"":badge("128 条消息 · 自动更新中","success")}</header><div class="messages">${messages}</div></div><aside class="chat-info"><span class="eyebrow">聊天信息</span>${empty?`<h3>尚未选择</h3><p>选择一个聊天后查看信息</p>`:`<div class="chat-group-card"><span class="avatar">旅</span><div><strong>旅行交流群</strong><span>51632940287@chatroom</span></div></div><div class="sender-card"><div class="sender-card-label">当前消息发送者</div><div class="sender-card-main"><span class="avatar blue">张</span><div><strong>张三｜3栋</strong><code>wxid_zhangsan</code></div><span class="copy-id">复制</span></div></div>`}<dl class="info-list"><div><dt>分类</dt><dd>${empty?"—":"群聊"}</dd></div><div><dt>群成员身份</dt><dd>${empty?"—":"头像 + 群昵称 + wxid"}</dd></div><div><dt>消息数量</dt><dd>${empty?"0 条":"128 条"}</dd></div><div><dt>最近消息</dt><dd>${empty?"—":"2026/09/10 09:45:19"}</dd></div></dl>${empty?"":'<button class="button danger-ghost small">删除这段记录</button>'}</aside></section>
    </section>`;
    return shell("history", "聊天记录", body);
  }

  function schedulesPage() {
    const rows = [
      ["每日群聊日报","群聊分析","旅行交流群","每天 21:30","今天 21:30","运行中"],
      ["早间天气提醒","常用指令","业主交流群","工作日 07:40","明天 07:40","运行中"],
      ["短剧更新推送","短剧","短剧分享群","每天 12:00","今天 12:00","已暂停"],
      ["周末活动提醒","定时任务","周末羽毛球","每周五 18:00","周五 18:00","运行中"],
      ["米游社签到","米游社签到","个人账号","每天 06:30","明天 06:30","运行中"],
    ];
    const body = `<section class="page">${heading("自动运行", "定时任务", "这里只显示会产生业务结果的任务；系统内部维护任务不会出现。", `<button class="button ghost">${icon("refresh")}刷新</button>`)}
      <section class="panel proactive"><div><h3>默认不主动打扰群聊</h3><p>主动消息按群单独授权，还会受到安静时段和每日上限约束。</p></div><button class="button secondary small">去管理群聊规则</button></section>
      <div class="metrics"><article class="panel metric"><span>全部任务</span><strong>5</strong></article><article class="panel metric"><span>运行中</span><strong style="color:var(--green-700)">4</strong></article><article class="panel metric"><span>已暂停</span><strong style="color:var(--amber-700)">1</strong></article></div>
      <section class="panel schedule-main"><div class="panel-head"><div><h2>任务列表</h2><p>暂停、改时间和删除都会先确认，不会误触。</p></div>${badge("Asia/Shanghai", "primary")}</div><table class="table"><thead><tr><th style="width:20%">任务名称</th><th style="width:13%">来源</th><th style="width:17%">发送到</th><th style="width:14%">执行时间</th><th style="width:13%">下次运行</th><th style="width:10%">状态</th><th style="width:13%">操作</th></tr></thead><tbody>${rows.map((r,i)=>`<tr><td><strong>${r[0]}</strong></td><td>${r[1]}</td><td>${r[2]}</td><td>${r[3]}</td><td>${r[4]}</td><td>${badge(r[5],i===2?"warning":"success")}</td><td><div class="button-row"><button class="button ghost small">${i===2?"开启":"暂停"}</button><button class="button ghost small">编辑</button><button class="icon-button" style="width:32px;height:32px;color:var(--red-700)">${icon("trash")}</button></div></td></tr>`).join("")}</tbody></table></section>
    </section>`;
    return shell("schedules", "定时任务", body);
  }

  function activityPage() {
    const stages = [["1","回复判断","已点名，走快速回复"],["2","长期记忆","明确问起，召回 2 条"],["3","情感上下文","使用精简关系摘要"],["4","回复模型","免费 Grok 4.6"],["5","发送结果","文字已发送"]];
    const rows = [["2026/09/10 09:45:19","机器人回复已发送","已通过微信发送文字消息","成功"],["2026/09/10 09:44:52","群聊规则已应用","旅行交流群 · 仅点名回复","成功"],["2026/09/10 09:44:03","长期记忆已召回","为本次回复提供 2 条相关摘要","成功"],["2026/09/10 09:42:18","短剧推送跳过","当前处于群聊安静时段","已保护"],["2026/09/10 09:40:06","模型请求未完成","已自动重试并恢复，不影响后续消息","已恢复"],["2026/09/10 09:35:12","机器人进入可用状态","账号、通信与心跳全部通过","成功"]];
    const body = `<section class="page">${heading("清晰可追溯", "运行记录", "只显示普通用户能理解的处理阶段，不展示聊天正文、私人记忆或内部名称。", `<div class="button-row"><button class="button ghost">筛选记录</button><button class="button ghost">${icon("refresh")}刷新</button></div>`)}
      <section class="panel trace-panel"><div class="panel-head" style="padding:0 0 13px;border:0"><div><span class="eyebrow">最近回复</span><h2>消息处理链路</h2></div>${badge("已成功发送", "success")}</div><div class="trace-grid">${stages.map(s=>`<article class="trace-stage"><span class="trace-icon">${s[0]}</span><strong>${s[1]}</strong><small>${s[2]}</small></article>`).join("")}</div></section>
      <section class="panel" style="flex:1;min-height:0"><div class="panel-head"><div><h2>最近活动</h2><p>按本机时间显示，失败信息会给出下一步。</p></div><span class="summary-line">共 36 条 · 当前显示最近 6 条</span></div><div class="timeline">${rows.map((r,i)=>`<div class="timeline-row"><time>${r[0]}</time><span class="timeline-node ${i===3?"warn":i===4?"fail":""}">${i===3?"!":i===4?"×":"✓"}</span><div><strong>${r[1]}</strong><small>${r[2]}</small></div>${badge(r[3],i===3?"warning":i===4?"danger":"success")}</div>`).join("")}</div></section>
    </section>`;
    return shell("activity", "运行记录", body);
  }

  function settingsNav(active) {
    return `<section class="panel settings-nav"><div class="settings-tabs"><span class="${active==="model"?"active":""}">模型服务</span><span class="${active==="persona"?"active":""}">人格与回复</span><span class="${active==="memory"?"active":""}">记忆与情感</span><span class="${active==="system"?"active":""}">身份与维护</span></div><span class="summary-line">修改后会明确提示“已应用”或“启动时使用”</span></section>`;
  }

  function settingsPage(section = "model", dropdown = "") {
    let content = "";
    if (section === "model") {
      const serviceDropdown = dropdown === "service-type" ? `<div class="dropdown"><div class="dropdown-option active"><span>免费 Grok 4.6</span><span class="recommend">推荐</span></div><div class="dropdown-option"><span>DeepSeek</span><small>使用自己的密钥</small></div><div class="dropdown-option"><span>其他兼容服务</span><small>适合开发者</small></div></div>` : "";
      const modelDropdown = dropdown === "model" ? `<div class="dropdown"><div class="dropdown-option active"><span>grok-4.6</span><small>当前选择</small></div><div class="dropdown-option"><span>grok-4.6-fast</span><small>响应更快</small></div><div class="dropdown-option"><span>grok-4.5</span><small>兼容模式</small></div><div class="dropdown-option"><span>手动输入其他模型名</span></div></div>` : "";
      content = `<div class="settings-content"><section class="panel settings-card"><div class="panel-head"><div><span class="eyebrow">统一模型服务</span><h2>模型服务中心</h2><p>机器人和功能中心共用同一份模型配置。</p></div>${badge("免费 Grok 4.6 使用中", "success")}</div><div class="service-list"><div class="service active"><div><strong>免费 Grok 4.6</strong><small>当前回复模型 · 配置已统一共享</small></div><div class="button-row">${badge("使用中", "success")}<button class="button secondary small">编辑</button></div></div><div class="service"><div><strong>DeepSeek · v4-flash</strong><small>已配置 · 可供相关功能选择</small></div><div class="button-row"><button class="button ghost small">设为回复模型</button><button class="button ghost small">编辑</button></div></div></div><div class="panel-head"><div><h3>编辑：免费 Grok 4.6</h3><p>普通用户保持推荐项即可</p></div><button class="button ghost small">添加新服务</button></div><div class="settings-form"><div class="field" style="position:relative">${field("服务类型", selectBox("免费 Grok 4.6（推荐）", serviceDropdown), "面向普通用户的默认回复服务")}</div><div class="form-grid"><div>${field("接口地址", '<div class="input">https://api.xiuxianjyj.xin/v1</div>')}</div><div class="field" style="position:relative">${field("模型名称", `<div class="select">grok-4.6${icon("chevron")}</div>${modelDropdown}`, "可手动填写，也可从服务端读取后选择")}</div></div>${field("服务密钥", '<div class="input" style="justify-content:space-between"><span>••••••••••••••••••••</span><button class="button ghost small">显示</button></div>', "已安全保存；留空不会覆盖")}</div><div class="sticky-save"><span>运行中保存会进行热更新并回读确认。</span><div class="button-row"><button class="button ghost">测试连接</button><button class="button primary">保存并设为回复模型</button></div></div></section><aside class="panel settings-card"><div class="panel-head"><div><span class="eyebrow">小白提示</span><h2>怎么选模型？</h2></div>${icon("help")}</div><div class="panel-body"><div class="callout"><strong>推荐保持免费 Grok 4.6</strong><br>日常群聊无需额外注册或填写密钥。</div><div class="setting-row"><div><strong>我有自己的 DeepSeek 密钥</strong><small>添加为备用或指定功能使用，不会自动覆盖当前模型。</small></div></div><div class="setting-row"><div><strong>获取模型名称</strong><small>读取到的建议只出现在下拉中，不会擅自替换当前值。</small></div></div><div class="setting-row"><div><strong>删除正在使用的模型</strong><small>必须先选择接替模型，避免机器人突然无法回复。</small></div></div></div></aside></div>`;
    } else if (section === "persona") {
      const toneDropdown = dropdown === "persona-tone" ? `<div class="dropdown"><div class="dropdown-option active"><span>自然熟人</span><span class="recommend">推荐</span></div><div class="dropdown-option"><span>温柔陪伴</span></div><div class="dropdown-option"><span>专业助理</span></div><div class="dropdown-option"><span>活泼群友</span></div></div>` : "";
      content = `<div class="settings-content equal"><section class="panel settings-card" style="position:relative"><div class="panel-head"><div><span class="eyebrow">人格与回复</span><h2>她是谁、什么时候说话</h2><p>先用简单选项调整风格，高级文字可保持默认。</p></div>${badge("保存后立即更新")}</div><div class="settings-form"><div class="form-grid"><div class="field" style="position:relative">${field("说话风格", selectBox("自然熟人", toneDropdown), "语气亲切，但不过度热情")}</div>${field("日常回复长度", selectBox("简短，适合群聊"))}</div>${field("机器人昵称", '<div class="input">姜小妹｜小妹</div>', "多个昵称用｜分隔，被直接叫到时也算点名")}
      <div class="check-grid"><div class="check"><span class="checkbox on">✓</span><span>群聊中只在被点名后加入聊天</span></div><div class="check"><span class="checkbox on">✓</span><span>点名或引用时直接交给回复模型</span></div></div><div class="form-card"><h3>高级人格文字</h3><div class="textarea">你是姜小妹，一位自然、可靠、边界清晰的群聊伙伴……</div></div></div><div class="sticky-save"><span>不会清空未修改的高级内容。</span><button class="button primary">保存人格设置</button></div></section><section class="panel settings-card"><div class="panel-head"><div><span class="eyebrow">回复预览</span><h2>设置会怎样影响回复</h2></div>${badge("示例，不会发送", "primary")}</div><div class="panel-body"><div class="callout">当前组合：自然熟人 · 简短回复 · 仅点名参与</div><div class="message" style="margin-top:18px"><span class="avatar blue">张</span><div class="bubble-wrap"><div class="bubble-meta"><span>张三</span></div><div class="bubble">小妹，明天杭州会下雨吗？</div></div></div><div class="message mine"><div class="bubble-wrap"><div class="bubble-meta"><span>姜小妹</span></div><div class="bubble">明天杭州有阵雨，出门带把伞更稳妥。</div></div><span class="avatar">姜</span></div><div class="modal-note">没有点名时默认不插话；被点名后优先快速回应。</div></div></section></div>`;
    } else if (section === "memory") {
      content = `<div class="settings-content equal"><section class="panel settings-card" style="position:relative"><div class="panel-head"><div><span class="eyebrow">长期记忆</span><h2>记住重要的聊天</h2><p>普通寒暄不查询，明确问起时优先回忆。</p></div>${badge("已启用", "success")}</div><div class="settings-form"><div class="setting-row"><div><strong>使用长期记忆</strong><small>关闭后不自动检索往事</small></div><span class="toggle on"></span></div>${field("自动回忆方式", selectBox("智能（推荐）"), "在需要时查询，兼顾速度和连续性")}<div class="setting-row"><div><strong>明确问起往事时优先回忆</strong><small>例如“上次说了什么”“你还记得吗”</small></div><span class="toggle on"></span></div>${field("回忆触发说法", '<div class="textarea">上次说了什么，还记得吗，之前提到</div>', "用逗号分隔，最多 20 条")}</div><div class="sticky-save"><span>高级向量记忆启用时会替代本功能，不会重复处理。</span><div class="button-row"><button class="button ghost">查看和删除记忆</button><button class="button primary">保存记忆设置</button></div></div></section><section class="panel settings-card" style="position:relative"><div class="panel-head"><div><span class="eyebrow">情感成长</span><h2>好感度与每日心情</h2><p>只提供精简关系摘要，不把完整记录塞给模型。</p></div>${badge("已启用", "success")}</div><div class="settings-form"><div class="setting-row"><div><strong>使用情感成长</strong><small>让相处方式随互动保持连续</small></div><span class="toggle on"></span></div><div class="check-grid"><div class="check"><span class="checkbox on">✓</span><span>每天自然更换心情</span></div><div class="check"><span class="checkbox on">✓</span><span>回复时参考好感度</span></div><div class="check"><span class="checkbox on">✓</span><span>回复时参考当前心情</span></div><div class="check"><span class="checkbox">✓</span><span>在运行记录展示数值</span></div></div><div class="callout"><strong>隐私说明</strong><br>运行记录只显示“已使用情感上下文”，不会展示私人学习记录。</div></div><div class="sticky-save"><span>设置只影响后续回复。</span><button class="button primary">保存情感设置</button></div></section></div>`;
    } else {
      content = `<div class="settings-content equal"><section class="panel settings-card" style="position:relative"><div class="panel-head"><div><span class="eyebrow">一键替换</span><h2>机器人名称与身份</h2><p>统一更新窗口、导航和回复身份。</p></div>${badge("当前：姜小妹")}</div><div class="settings-form"><div class="form-grid">${field("软件与机器人名称", '<div class="input">姜小妹机器人</div>')}${field("简称", '<div class="input">姜小妹</div>')}</div>${field("人格称呼", '<div class="input">小妹</div>')}${field("欢迎语", '<div class="textarea">你好，我是姜小妹。需要我帮你整理群聊、提醒任务或查询信息都可以。</div>')}${field("管理员微信号", '<div class="input">wxid_admin_demo</div>', "留空表示暂不设置管理员")}</div><div class="sticky-save"><span>保存后统一替换，不需要逐处修改。</span><button class="button primary">保存并统一替换</button></div></section><section class="panel settings-card"><div class="panel-head"><div><span class="eyebrow">微信环境</span><h2>客户端位置</h2></div>${badge("版本兼容", "success")}</div><div class="panel-body"><div class="path-box">C:\\Program Files\\Tencent\\Weixin\\Weixin.exe</div><div class="button-row" style="margin-top:10px"><button class="button secondary small">重新选择</button><button class="button ghost small">重新检测</button></div></div><div class="panel-head"><div><span class="eyebrow">数据与版本</span><h2>维护</h2></div></div><div class="panel-body" style="padding-top:4px"><div class="setting-row"><div><strong>设置与记忆备份</strong><small>机器人停止后可创建</small></div><button class="button secondary small">立即备份</button></div><div class="setting-row"><div><strong>恢复备份</strong><small>最近备份：2026/09/09 22:18</small></div><button class="button ghost small">选择恢复</button></div><div class="setting-row"><div><strong>安全诊断</strong><small>不包含密钥、聊天正文或完整本机路径</small></div><button class="button ghost small">导出摘要</button></div><div class="setting-row"><div><strong>软件版本</strong><small>v0.9.0 · 发现 v0.9.1</small></div><button class="button ghost small">检查更新</button></div><div class="setting-row"><div><strong>彻底卸载</strong><small>删除程序与全部本机数据</small></div><button class="button danger-ghost small">打开卸载程序</button></div></div></section></div>`;
    }
    const body = `<section class="page settings-page">${heading("统一配置", "设置", "先完成推荐项；只有需要时再展开高级设置。")}${settingsNav(section)}${content}</section>`;
    return shell("settings", "设置", body);
  }

  function designSystemPage() {
    const body = `<section class="page">${heading("DESIGN SYSTEM", "机器人界面视觉规范", "可靠、安静、易理解；所有状态都有文字，不只依赖颜色。", badge("桌面端 1600 × 1000", "primary"))}<div class="component-board"><section class="panel"><div class="panel-head"><div><h2>颜色与层级</h2><p>蓝色用于主操作，绿/黄/红只表达状态。</p></div></div><div class="panel-body"><div class="swatches">${[["#071A2F","海军蓝 / 导航"],["#2F6BFF","主蓝 / 主操作"],["#0F9F5C","成功 / 可用"],["#D63E54","危险 / 删除"],["#F3F6FB","页面画布"],["#FFFFFF","内容表面"],["#10243D","主要文字"],["#697B90","辅助文字"]].map(c=>`<div class="swatch"><div class="swatch-color" style="background:${c[0]}"></div><label>${c[1]}<br>${c[0]}</label></div>`).join("")}</div><div style="margin-top:16px"><div class="type-sample"><strong style="font-size:28px">页面标题 28px / 750</strong><span>只说明当前页面，不使用技术名词</span></div><div class="type-sample"><strong style="font-size:18px">面板标题 18px / 700</strong><span>一个面板只解决一类问题</span></div><div class="type-sample"><strong>正文 14px / 400</strong><span>中文正文行高 1.5，辅助说明不小于 10px（正式实现建议 12px）</span></div></div></div></section><section class="panel"><div class="panel-head"><div><h2>核心组件</h2><p>所有可点击区域至少 32px，键盘焦点清晰。</p></div></div><div class="panel-body"><div class="component-group"><button class="button primary">主要操作</button><button class="button secondary">次要操作</button><button class="button ghost">普通操作</button><button class="button danger">危险操作</button></div><div class="component-group">${badge("机器人可用", "success")}${badge("等待用户", "warning")}${badge("操作失败", "danger")}${badge("推荐设置", "primary")}</div><div class="component-group" style="display:grid;grid-template-columns:1fr 1fr">${field("输入框", '<div class="input">输入内容</div>', "始终有标签和帮助文字")}${field("下拉选择", selectBox("推荐选项"), "保留手动输入能力")}</div><div class="component-group"><div class="setting-row" style="width:100%"><div><strong>开关项</strong><small>标题说明结果，副文本说明影响</small></div><span class="toggle on"></span></div></div><div class="component-group"><div class="callout" style="width:100%"><strong>反馈原则</strong><br>短时成功使用 Toast；持续故障留在页面；删除和覆盖必须使用自有确认弹窗。</div></div></div></section></div></section>`;
    return shell("home", "设计系统", body);
  }

  function modalContent(type) {
    const defs = {
      unsaved: ["alert", "离开前丢弃修改？", "群聊分析的这些设置还没有保存。留在此页可继续编辑或保存。", "继续编辑", "丢弃修改并继续", "danger"],
      "history-delete": ["trash", "删除这段聊天记录？", "仅删除管理软件中的本机副本，不会影响微信原始聊天，也不会损伤机器人的长期记忆。", "取消", "删除记录", "danger"],
      "schedule-delete": ["trash", "删除定时任务？", "“每日群聊日报”删除后将停止执行，群聊分析的其他设置不受影响。", "取消", "删除任务", "danger"],
      "backup-create": ["shield", "创建设置与记忆备份？", "备份包含机器人设置、人格、情感数据和长期记忆，请像保管账号密码一样妥善保管。", "取消", "创建备份", "primary"],
      "update-available": ["download", "发现新版本 v0.9.1", "本次更新优化群聊管理和新手引导。安装前请先停止机器人；更新失败会自动恢复原版本。", "稍后再说", "下载并安装", "primary"],
    };
    if (type === "onboarding") return `<div class="overlay"><section class="modal onboarding-modal"><span class="eyebrow">第一次使用</span><h2>三步让机器人安全跑起来</h2><p>不用理解插件或命令；推荐设置已经替你做好了保守选择。</p><div class="onboarding-steps"><article class="onboard-step"><span class="onboard-index">1</span><strong>连接微信</strong><small>自动检查兼容版本，需要时提供下载或选择路径。</small></article><article class="onboard-step"><span class="onboard-index">2</span><strong>确认回复模型</strong><small>普通用户保持免费 Grok 4.6，无需额外配置。</small></article><article class="onboard-step"><span class="onboard-index">3</span><strong>管理目标群</strong><small>默认只在被点名时回复，不会主动打扰。</small></article></div><div class="modal-note">引导不会自动启动机器人，也不会向任何群发送测试消息。</div><div class="modal-actions"><button class="button ghost">稍后设置</button><button class="button secondary">按推荐设置开始</button><button class="button primary">逐项检查设置</button></div></section></div>`;
    if (type === "group-picker") return `<div class="overlay"><section class="modal wide"><div class="modal-icon info">${icon("users")}</div><h2>选择群聊</h2><p>只会把群标识加入当前草稿；点击“保存并应用”后才会生效，不会向群里发送消息。</p><div class="modal-field"><div class="search">${icon("search")}<span>搜索群名称或群标识</span></div></div><div class="check-grid" style="margin-top:12px"><div class="check"><span class="checkbox on">✓</span><span><strong>旅行交流群</strong><br><small>51632940287@chatroom · 128 人</small></span></div><div class="check"><span class="checkbox">✓</span><span><strong>短剧分享群</strong><br><small>81263495012@chatroom · 76 人</small></span></div><div class="check"><span class="checkbox">✓</span><span><strong>业主交流群</strong><br><small>66547231890@chatroom · 成员未知</small></span></div><div class="check"><span class="checkbox">✓</span><span><strong>周末羽毛球</strong><br><small>34902765188@chatroom · 42 人</small></span></div></div><div class="modal-actions"><button class="button ghost">取消</button><button class="button primary">添加到列表（1）</button></div></section></div>`;
    if (type === "schedule-edit") return `<div class="overlay"><section class="modal"><div class="modal-icon info">${icon("clock")}</div><h2>修改执行时间</h2><p>“每日群聊日报”当前安排为“每天 21:30”。保存后会同步到群聊分析功能。</p><div class="modal-field"><span>每天执行时间</span><div class="input">21:30</div><small style="display:block;margin-top:6px;color:var(--muted)">使用本机时区 Asia/Shanghai</small></div><div class="modal-actions"><button class="button ghost">取消</button><button class="button primary">保存时间</button></div></section></div>`;
    if (type === "provider-delete") return `<div class="overlay"><section class="modal"><div class="modal-icon">${icon("trash")}</div><h2>删除 DeepSeek · v4-flash？</h2><p>群聊分析和图片生成正在使用它。删除前会自动切换到你选择的接替模型。</p><div class="modal-field field" style="position:relative"><span>接替模型</span>${selectBox("免费 Grok 4.6（推荐）", '<div class="dropdown"><div class="dropdown-option active"><span>免费 Grok 4.6</span><span class="recommend">推荐</span></div><div class="dropdown-option"><span>其他兼容服务 · qwen-max</span></div></div>')}</div><div class="modal-note">聊天记录、功能设置和密钥不会因切换而丢失。</div><div class="modal-actions"><button class="button ghost">取消</button><button class="button danger">切换并删除</button></div></section></div>`;
    if (type === "backup-restore") return `<div class="overlay"><section class="modal"><div class="modal-icon info">${icon("folder")}</div><h2>恢复设置与记忆？</h2><p>恢复会覆盖备份中包含的设置和记忆，并先自动保留恢复前副本。机器人必须保持停止。</p><div class="modal-field field" style="position:relative"><span>选择备份</span>${selectBox("2026/09/09 22:18 · 自动备份", '<div class="dropdown"><div class="dropdown-option active"><span>2026/09/09 22:18</span><small>自动备份 · 18.4 MB</small></div><div class="dropdown-option"><span>2026/09/08 20:06</span><small>手动备份 · 18.1 MB</small></div><div class="dropdown-option"><span>2026/09/01 10:12</span><small>更新前备份 · 17.9 MB</small></div></div>')}</div><div class="modal-actions"><button class="button ghost">取消</button><button class="button danger">校验并恢复</button></div></section></div>`;
    if (type === "history-clean") return `<div class="overlay"><section class="modal"><div class="modal-icon">${icon("trash")}</div><h2>按日期清理聊天记录</h2><p>将删除所选日期以前的本机记录副本。此操作无法撤销，但不会影响微信原记录或长期记忆。</p><div class="modal-field"><span>保留此日期及之后的记录</span><div class="date-box">2026/08/01${icon("calendar")}</div></div><div class="modal-note">预计删除约 1,248 条本机记录。</div><div class="modal-actions"><button class="button ghost">取消</button><button class="button danger">开始清理</button></div></section></div>`;
    if (type === "update-progress") return `<div class="overlay"><section class="modal"><div class="modal-icon info">${icon("download")}</div><h2>正在安装 v0.9.1</h2><p>正在校验更新包。完成后软件会自动重启，请勿关闭软件或断开网络。</p><div class="modal-progress"><span></span></div><div class="progress-copy"><span>验证文件完整性</span><strong>67%</strong></div><div class="modal-note">更新失败会自动恢复 v0.9.0，不会清除设置、聊天记录或记忆。</div><div class="modal-actions"><button class="button disabled">更新处理中…</button></div></section></div>`;
    const d = defs[type] || defs.unsaved;
    return `<div class="overlay"><section class="modal"><div class="modal-icon ${d[5]==="primary"?"info":""}">${icon(d[0])}</div><h2>${d[1]}</h2><p>${d[2]}</p><div class="modal-actions"><button class="button ghost">${d[3]}</button><button class="button ${d[5]}">${d[4]}</button></div></section></div>`;
  }

  function modalPage(type) {
    const bases = {
      onboarding: runPage("stopped"), unsaved: featurePage("basic"), "group-picker": featurePage("basic"), "history-delete": historyPage(false), "schedule-edit": schedulesPage(), "schedule-delete": schedulesPage(), "provider-delete": settingsPage("model"), "backup-create": settingsPage("system"), "backup-restore": settingsPage("system"), "history-clean": settingsPage("system"), "update-available": settingsPage("system"), "update-progress": settingsPage("system"),
    };
    return `${bases[type] || runPage("running")}${modalContent(type)}`;
  }

  function toastPage() {
    return `${runPage("running")}<div class="toast-stack"><div class="toast"><span class="toast-icon">✓</span><div><strong>设置已保存并应用</strong><small>机器人已读取最新配置</small></div><time>刚刚</time></div><div class="toast warning"><span class="toast-icon">!</span><div><strong>已保存，启动时使用</strong><small>机器人当前未连接，启动后自动加载</small></div><time>刚刚</time></div><div class="toast error"><span class="toast-icon">×</span><div><strong>新设置未能生效</strong><small>已恢复原设置，输入内容仍然保留</small></div><time>刚刚</time></div></div>`;
  }

  function dropdownPage(type) {
    if (type === "service-type" || type === "model") return settingsPage("model", type);
    if (type === "reply-mode") return groupsPage("policy", true);
    if (type === "persona-tone") return settingsPage("persona", type);
    if (type === "date") return historyPage(false, true);
    if (type === "feature-option") {
      const page = featurePage("basic");
      return page.replace(selectBox("手账风（示例样式）"), selectBox("手账风（示例样式）", `<div class="dropdown"><div class="dropdown-option active"><span>手账风</span><span class="recommend">推荐</span></div><div class="dropdown-option"><span>简洁摘要</span><small>适合工作群</small></div><div class="dropdown-option"><span>数据看板</span><small>突出统计数据</small></div><div class="dropdown-option"><span>轻松群报</span><small>适合兴趣群</small></div></div>`));
    }
    return runPage("running");
  }

  function render() {
    let html;
    if (modalScreens[screen]) html = modalPage(modalScreens[screen]);
    else if (dropdownScreens[screen]) html = dropdownPage(dropdownScreens[screen]);
    else if (screen === "feedback-toasts") html = toastPage();
    else {
      const pages = {
        "design-system": designSystemPage,
        "run-running": () => runPage("running"),
        "run-stopped": () => runPage("stopped"),
        "run-environment-error": () => runPage("error"),
        "feature-overview": () => featurePage("overview"),
        "feature-config": () => featurePage("basic"),
        "feature-advanced": () => featurePage("advanced"),
        groups: () => groupsPage("empty"),
        "groups-policy": () => groupsPage("policy"),
        "groups-members": () => groupsPage("members"),
        history: () => historyPage(false),
        "history-empty": () => historyPage(true),
        schedules: schedulesPage,
        activity: activityPage,
        "settings-model": () => settingsPage("model"),
        "settings-persona": () => settingsPage("persona"),
        "settings-memory": () => settingsPage("memory"),
        "settings-system": () => settingsPage("system"),
      };
      html = (pages[screen] || pages["run-running"])();
    }
    $app.innerHTML = html;
    document.querySelectorAll("[data-screen]").forEach(button => button.addEventListener("click", () => {
      const next = button.dataset.screen;
      location.href = `${location.pathname}?screen=${encodeURIComponent(next)}`;
    }));
  }

  render();
})();
