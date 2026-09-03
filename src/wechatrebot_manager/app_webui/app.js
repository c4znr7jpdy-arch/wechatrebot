(() => {
  "use strict";

  const state = {
    bootstrap: null,
    status: null,
    activePage: "home",
    featuresLoaded: false,
    features: [],
    selectedFeature: null,
    featureDetail: null,
    featureTab: "basic",
    historyKind: "all",
    conversations: [],
    selectedConversation: null,
    schedules: [],
    polling: false,
    composing: false,
    availableUpdate: null,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  function toast(message, type = "success") {
    const item = document.createElement("div");
    item.className = `toast ${type === "error" ? "error" : ""}`;
    item.textContent = message;
    $("#toastRegion").append(item);
    window.setTimeout(() => item.remove(), 3600);
  }

  async function api(method, ...args) {
    const bridge = window.pywebview?.api;
    if (!bridge || typeof bridge[method] !== "function") {
      throw new Error("软件界面尚未连接，请重新打开。 ");
    }
    const result = await bridge[method](...args);
    if (!result?.ok) {
      const detail = result?.error || {};
      const error = new Error(detail.message || "操作未完成，请稍后重试。 ");
      error.product = detail;
      throw error;
    }
    if (result.message) toast(result.message);
    return result.data;
  }

  function reportError(error) {
    const product = error?.product;
    const message = product
      ? `${product.title || "操作未完成"}${product.code ? `（${product.code}）` : ""}`
      : (error?.message || "操作未完成，请稍后重试。 ");
    toast(message, "error");
  }

  function debounce(fn, wait = 280) {
    let timer = 0;
    return (...args) => {
      window.clearTimeout(timer);
      timer = window.setTimeout(() => fn(...args), wait);
    };
  }

  function icon(id) {
    return `<svg aria-hidden="true"><use href="#${id}"/></svg>`;
  }

  function shortTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 16);
    return new Intl.DateTimeFormat("zh-CN", {
      month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false,
    }).format(date);
  }

  function fullTime(value) {
    if (!value) return "—";
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    if (Number.isNaN(date.getTime())) return String(value).replace("T", " ").slice(0, 19);
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
    }).format(date);
  }

  function setStage(element, mode, label) {
    element.classList.remove("done", "current", "problem");
    if (mode) element.classList.add(mode);
    $("em", element).textContent = label;
  }

  function renderStatus(status) {
    state.status = status;
    const runtime = status.runtime || {};
    const environment = status.environment || {};
    const current = status.state || "STOPPED";
    const online = Boolean(status.online);
    const waiting = ["WAITING_WECHAT", "VERIFYING_CONNECTION", "STOP_WAIT_WECHAT"].includes(current);
    const problem = current === "ERROR";
    const active = !["STOPPED", "ERROR"].includes(current);

    $("#stateTitle").textContent = status.title;
    $("#stateMessage").textContent = status.message;
    $("#accountText").textContent = runtime.wechat_identity ? `已登录：${runtime.wechat_identity}` : "尚未连接";
    $("#lastCheckText").textContent = `检查于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false})}`;

    const global = $("#globalStatus");
    global.className = `status-badge ${online ? "online" : problem ? "error" : waiting ? "warning" : "checking"}`;
    global.innerHTML = `<span></span>${escapeHtml(online ? "机器人可用" : problem ? "需要处理" : waiting ? status.title : active ? "正在启动" : environment.ready ? "等待启动" : "环境未就绪")}`;
    const sideDot = $("#sideHealthDot");
    sideDot.className = `health-dot ${online ? "online" : problem ? "error" : waiting || !environment.ready ? "warning" : ""}`;
    $("#sideHealthText").textContent = online ? "机器人在线" : problem ? "需要处理" : waiting ? "等待操作" : "当前未运行";

    const orbit = $("#statusOrbit");
    orbit.dataset.state = online ? "online" : problem ? "error" : waiting ? "waiting" : status.busy || active ? "starting" : "stopped";
    const primary = $("#primaryAction");
    const stopMode = status.can_stop || online;
    primary.innerHTML = `${icon(stopMode ? "i-stop" : "i-play")}<span>${stopMode ? (current === "STOP_WAIT_WECHAT" ? "等待关闭" : "停止") : "启动"}</span>`;
    primary.dataset.action = stopMode ? "stop" : "start";
    primary.disabled = current === "STOP_WAIT_WECHAT" || Boolean(status.busy) || (!status.can_start && !status.can_stop);

    const alert = $("#environmentAlert");
    alert.hidden = Boolean(environment.ready);
    if (!environment.ready) {
      $("#environmentCode").textContent = environment.code || "11001";
      $("#environmentTitle").textContent = environment.title || "微信环境未就绪";
      $("#environmentMessage").textContent = environment.message || "请完成微信安装后重新检测。 ";
      $("#environmentSolutions").innerHTML = (environment.solutions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    }

    const errorPanel = $("#errorPanel");
    errorPanel.hidden = !status.error;
    if (status.error) {
      $("#errorTitle").textContent = status.error.title;
      $("#errorMessage").textContent = status.error.message;
      $("#errorCode").textContent = status.error.code;
      $("#errorSolutions").innerHTML = (status.error.solutions || []).map((item) => `<li>${escapeHtml(item)}</li>`).join("");
    }

    setStage($("#stageEnvironment"), environment.ready ? "done" : "problem", environment.ready ? "已通过" : "未通过");
    const coreStarted = Boolean(runtime.engine_ready) || ["CORE_READY", "CONNECTION_STARTING", "WAITING_WECHAT", "VERIFYING_CONNECTION", "RUNNING", "STOP_WAIT_WECHAT"].includes(current);
    setStage($("#stageCore"), coreStarted ? "done" : active ? "current" : "", coreStarted ? "已就绪" : active ? "启动中" : "未启动");
    const connectionMode = online ? "done" : waiting ? "current" : problem && coreStarted ? "problem" : "";
    setStage($("#stageConnection"), connectionMode, online ? "已连接" : waiting ? "连接中" : problem && coreStarted ? "未完成" : "未连接");

    $("#signalWechat").textContent = environment.ready ? "版本兼容" : "未就绪";
    $("#signalWechatDetail").textContent = environment.ready ? `${environment.version || "已检测"} · ${environment.process_running ? "正在运行" : "尚未运行"}` : "请安装或重新选择微信";
    $("#signalCore").textContent = runtime.engine_ready ? "运行正常" : active ? "正在启动" : "未启动";
    $("#signalMessage").textContent = online ? "通信正常" : runtime.connection_ready ? "正在确认" : "未连接";
    $("#wechatPath").textContent = environment.path || "尚未选择";
  }

  async function refreshStatus() {
    if (state.polling) return;
    state.polling = true;
    try {
      renderStatus(await api("get_status"));
    } catch (error) {
      reportError(error);
    } finally {
      state.polling = false;
    }
  }

  async function runPrimaryAction() {
    const button = $("#primaryAction");
    button.disabled = true;
    try {
      await api(button.dataset.action === "stop" ? "stop_robot" : "start_robot");
      window.setTimeout(refreshStatus, 250);
    } catch (error) {
      reportError(error);
      await refreshStatus();
    }
  }

  async function checkEnvironment() {
    try { renderStatus(await api("check_environment")); }
    catch (error) { reportError(error); }
  }

  async function chooseWechat() {
    try {
      const result = await api("select_wechat", "");
      if (!result?.cancelled) {
        renderStatus(result);
        toast("微信路径已保存。 ");
      }
    } catch (error) { reportError(error); }
  }

  const PAGE_TITLES = {
    home: "运行中心", features: "功能中心", history: "聊天记录",
    schedules: "定时任务", activity: "运行记录", settings: "设置",
  };

  async function navigate(page) {
    if (!PAGE_TITLES[page]) return;
    state.activePage = page;
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
    $$(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
    $("#pageTitle").textContent = PAGE_TITLES[page];
    if (page === "features" && !state.featuresLoaded) await loadFeatures();
    if (page === "history") await loadConversations();
    if (page === "schedules") await loadSchedules();
    if (page === "activity") await loadActivity();
  }

  async function loadFeatures() {
    const list = $("#featureList");
    list.innerHTML = '<div class="loading-row">正在读取功能…</div>';
    try {
      const data = await api("get_features");
      state.features = data.plugins || [];
      state.featuresLoaded = true;
      $("#featureSummary").textContent = `${state.features.length} 项功能 · ${data.engine_online ? "设置可立即应用" : "设置将在下次启动时应用"}`;
      renderFeatureList();
    } catch (error) {
      list.innerHTML = '<div class="loading-row">功能暂时无法读取</div>';
      reportError(error);
    }
  }

  function renderFeatureList() {
    const keyword = $("#featureSearch").value.trim().toLocaleLowerCase("zh-CN");
    const items = state.features.filter((item) => !keyword || item.display_name.toLocaleLowerCase("zh-CN").includes(keyword));
    $("#featureList").innerHTML = items.length ? items.map((item) => `
      <button class="feature-item ${state.selectedFeature === item.id ? "active" : ""}" data-feature-id="${escapeHtml(item.id)}">
        <span><strong>${escapeHtml(item.display_name)}</strong><small>${item.configurable ? `${item.field_count} 项可配置内容` : "无需额外配置"}</small></span>
        <span class="feature-state ${item.enabled ? "enabled" : ""}" title="${item.enabled ? "已启用" : "已停用"}"></span>
      </button>`).join("") : '<div class="loading-row">没有符合条件的功能</div>';
    $$(".feature-item").forEach((button) => button.addEventListener("click", () => openFeature(button.dataset.featureId)));
  }

  async function openFeature(featureId) {
    state.selectedFeature = featureId;
    renderFeatureList();
    $("#featureDetail").innerHTML = '<div class="loading-row">正在读取功能设置…</div>';
    try {
      state.featureDetail = await api("get_feature", featureId);
      state.featureTab = Object.keys(state.featureDetail.basic_schema || {}).length ? "basic" : "models";
      renderFeatureDetail();
    } catch (error) {
      $("#featureDetail").innerHTML = '<div class="empty-state"><h2>设置暂时无法读取</h2><p>机器人未启动时仍可修改大部分基础设置，请稍后重试。</p></div>';
      reportError(error);
    }
  }

  function getAtPath(source, path) {
    return path.reduce((value, key) => value && typeof value === "object" ? value[key] : undefined, source);
  }

  function setAtPath(target, path, value) {
    let cursor = target;
    path.forEach((key, index) => {
      if (index === path.length - 1) cursor[key] = value;
      else {
        if (!cursor[key] || typeof cursor[key] !== "object" || Array.isArray(cursor[key])) cursor[key] = {};
        cursor = cursor[key];
      }
    });
  }

  function fieldMarkup(key, field, value, path) {
    const type = field.type || "string";
    const label = field.description || field.title || key.replaceAll("_", " ");
    const hint = field.hint || field.help || "";
    const encodedPath = escapeHtml(JSON.stringify(path));
    let control = "";
    if (type === "bool") {
      control = `<label class="toggle"><input class="config-input" data-path="${encodedPath}" data-type="bool" type="checkbox" ${value ? "checked" : ""}><span class="toggle-track"></span><span>${value ? "已启用" : "已停用"}</span></label>`;
    } else if (Array.isArray(field.options)) {
      const optionNames = Array.isArray(field.options_label) ? field.options_label : field.options;
      control = `<select class="config-input" data-path="${encodedPath}" data-type="${escapeHtml(type)}">${field.options.map((option, index) => `<option value="${escapeHtml(option)}" ${String(value) === String(option) ? "selected" : ""}>${escapeHtml(optionNames[index] ?? option)}</option>`).join("")}</select>`;
    } else if (["list", "template_list"].includes(type)) {
      control = `<textarea class="config-input" data-path="${encodedPath}" data-type="json" rows="5">${escapeHtml(JSON.stringify(value ?? [], null, 2))}</textarea>`;
    } else if (type === "text") {
      control = `<textarea class="config-input" data-path="${encodedPath}" data-type="string" rows="4">${escapeHtml(value ?? "")}</textarea>`;
    } else {
      const inputType = ["int", "float"].includes(type) ? "number" : (field.secret ? "password" : "text");
      const step = type === "float" ? "any" : "1";
      control = `<input class="config-input" data-path="${encodedPath}" data-type="${escapeHtml(type)}" type="${inputType}" ${inputType === "number" ? `step="${step}"` : ""} value="${escapeHtml(value ?? "")}">`;
    }
    return `<label class="form-field ${["text", "list", "template_list"].includes(type) ? "full" : ""}"><span>${escapeHtml(label)}</span>${control}${hint ? `<small>${escapeHtml(hint)}</small>` : ""}</label>`;
  }

  function schemaMarkup(schema, config, prefix = []) {
    return Object.entries(schema || {}).map(([key, field]) => {
      if (!field || typeof field !== "object") return "";
      const path = [...prefix, key];
      if (field.type === "object" && field.items && typeof field.items === "object") {
        return `<section class="config-section"><h3>${escapeHtml(field.description || field.title || key.replaceAll("_", " "))}</h3><div class="config-section-grid">${schemaMarkup(field.items, config, path)}</div></section>`;
      }
      return fieldMarkup(key, field, getAtPath(config, path), path);
    }).join("");
  }

  function renderFeatureDetail() {
    const detail = state.featureDetail;
    if (!detail) return;
    const basicCount = Object.keys(detail.basic_schema || {}).length;
    const modelCount = Object.keys(detail.model_schema || {}).length;
    const currentSchema = state.featureTab === "models" ? detail.model_schema : detail.basic_schema;
    $("#featureDetail").innerHTML = `
      <div class="feature-detail-head">
        <div><span class="eyebrow">功能设置</span><h2>${escapeHtml(detail.display_name)}</h2><p>${detail.configurable ? "修改参数后保存；运行中会尽量立即应用。" : "这项功能无需额外参数，可直接启停。"}</p></div>
        <label class="toggle"><input id="featureEnabled" type="checkbox" ${detail.enabled ? "checked" : ""}><span class="toggle-track"></span><span>${detail.enabled ? "已启用" : "已停用"}</span></label>
      </div>
      ${detail.configurable ? `<div class="config-tabs"><button data-tab="basic" class="${state.featureTab === "basic" ? "active" : ""}" ${!basicCount ? "disabled" : ""}>基础设置 ${basicCount ? `(${detail.field_count - detail.model_field_count})` : ""}</button><button data-tab="models" class="${state.featureTab === "models" ? "active" : ""}" ${!modelCount ? "disabled" : ""}>模型设置 ${modelCount ? `(${detail.model_field_count})` : ""}</button></div><form id="featureForm" class="feature-form" novalidate>${schemaMarkup(currentSchema, detail.config)}${!Object.keys(currentSchema || {}).length ? '<div class="empty-state compact"><p>这个分类暂无可配置内容</p></div>' : ""}</form><div class="feature-savebar"><span class="form-note" id="featureNote">${detail.engine_online ? "保存后立即应用" : "保存后将在下次启动时应用"}</span><button class="button primary" id="saveFeature">保存设置</button></div>` : '<div class="empty-state compact"><p>通过右上角开关即可管理这项功能。</p></div>'}`;
    $("#featureEnabled").addEventListener("change", toggleFeature);
    $$(".config-tabs button").forEach((button) => button.addEventListener("click", () => {
      if (!button.disabled) { state.featureTab = button.dataset.tab; renderFeatureDetail(); }
    }));
    $$(".config-input[data-type='bool']").forEach((input) => input.addEventListener("change", () => {
      const label = input.closest(".toggle")?.querySelector("span:last-child");
      if (label) label.textContent = input.checked ? "已启用" : "已停用";
    }));
    $("#saveFeature")?.addEventListener("click", saveFeature);
  }

  async function toggleFeature(event) {
    const input = event.currentTarget;
    input.disabled = true;
    try {
      const data = await api("set_feature_enabled", state.selectedFeature, input.checked);
      state.featureDetail.enabled = data.enabled;
      const item = state.features.find((value) => value.id === state.selectedFeature);
      if (item) item.enabled = data.enabled;
      toast(data.message || "功能状态已更新。 ");
      renderFeatureList(); renderFeatureDetail();
    } catch (error) { input.checked = !input.checked; reportError(error); }
    finally { input.disabled = false; }
  }

  function configFromForm() {
    const config = structuredClone(state.featureDetail.config || {});
    $$(".config-input", $("#featureForm")).forEach((input) => {
      const path = JSON.parse(input.dataset.path);
      let value;
      if (input.dataset.type === "bool") value = input.checked;
      else if (input.dataset.type === "int") value = Number.parseInt(input.value, 10);
      else if (input.dataset.type === "float") value = Number.parseFloat(input.value);
      else if (input.dataset.type === "json") value = JSON.parse(input.value || "[]");
      else value = input.value;
      setAtPath(config, path, value);
    });
    return config;
  }

  async function saveFeature() {
    const button = $("#saveFeature");
    button.disabled = true;
    try {
      const data = await api("save_feature", state.selectedFeature, configFromForm(), state.featureDetail.revision || "");
      state.featureDetail.config = configFromForm();
      state.featureDetail.revision = data.revision;
      $("#featureNote").textContent = data.message;
      toast(data.message);
    } catch (error) {
      reportError(error);
      $("#featureNote").textContent = error.message;
    } finally { button.disabled = false; }
  }

  async function loadConversations() {
    try {
      const data = await api("get_conversations", {
        kind: state.historyKind,
        search: $("#historySearch").value,
        start: $("#historyStart").value,
        end: $("#historyEnd").value,
      });
      state.conversations = data.items || [];
      const summary = data.summary || {};
      $("#historySummary").textContent = `${summary.conversations || 0} 个聊天 · ${summary.messages || 0} 条消息 · 个人 ${summary.private_messages || 0} · 群聊 ${summary.group_messages || 0}`;
      renderConversations();
    } catch (error) { reportError(error); }
  }

  function renderConversations() {
    const list = $("#conversationList");
    list.innerHTML = state.conversations.length ? state.conversations.map((item) => `
      <button class="conversation-item ${state.selectedConversation?.conversation_id === item.conversation_id ? "active" : ""}" data-conversation="${escapeHtml(item.conversation_id)}" data-type="${escapeHtml(item.type)}">
        <span class="conversation-avatar">${escapeHtml((item.display_name || "聊").slice(0, 1))}</span>
        <span class="conversation-main"><span class="conversation-line"><strong>${escapeHtml(item.display_name)}</strong><time>${escapeHtml(shortTime(item.last_timestamp * 1000))}</time></span><small>${item.type === "group" ? "群聊" : "个人聊天"} · ${item.message_count} 条</small></span>
      </button>`).join("") : '<div class="empty-state compact"><div class="empty-symbol">空</div><h2>没有找到聊天记录</h2><p>调整筛选条件或启动机器人接收新消息。</p></div>';
    $$(".conversation-item", list).forEach((button) => button.addEventListener("click", () => selectConversation(button.dataset.conversation)));
  }

  async function selectConversation(conversationId) {
    const item = state.conversations.find((value) => value.conversation_id === conversationId);
    if (!item) return;
    state.selectedConversation = item;
    renderConversations();
    $("#conversationHeader").innerHTML = `<div><strong>${escapeHtml(item.display_name)}</strong><span>${item.type === "group" ? "群聊" : "个人聊天"} · ${item.message_count} 条消息</span></div>`;
    $("#messageList").innerHTML = '<div class="loading-row">正在读取消息…</div>';
    try {
      const messages = await api("get_messages", conversationId);
      $("#messageList").innerHTML = messages.length ? messages.map((message) => {
        const assistant = message.role === "assistant";
        const name = assistant ? "机器人" : (message.sender_name || message.sender_id || "用户");
        return `<article class="message ${assistant ? "assistant" : ""}"><span class="message-avatar">${escapeHtml(name.slice(0, 1))}</span><div class="message-content"><div class="message-meta"><span>${escapeHtml(name)}</span><time>${escapeHtml(fullTime(message.timestamp))}</time></div><div class="message-bubble">${escapeHtml(message.content)}</div></div></article>`;
      }).join("") : '<div class="empty-state compact"><p>这个聊天暂无消息</p></div>';
      $("#messageList").scrollTop = $("#messageList").scrollHeight;
      renderConversationInspector(item);
    } catch (error) { reportError(error); }
  }

  function renderConversationInspector(item) {
    $("#conversationInspector").innerHTML = `<span class="eyebrow">聊天信息</span><h2>${escapeHtml(item.display_name)}</h2><p>${item.type === "group" ? "微信群聊记录" : "个人聊天记录"}</p><dl class="inspector-list"><div><dt>分类</dt><dd>${item.type === "group" ? "群聊" : "个人聊天"}</dd></div><div><dt>${item.type === "group" ? "群 ID" : "微信号"}</dt><dd>${escapeHtml(item.conversation_id)}</dd></div><div><dt>消息数量</dt><dd>${item.message_count} 条</dd></div><div><dt>最近消息</dt><dd>${escapeHtml(fullTime(item.last_timestamp))}</dd></div></dl><div class="inspector-actions"><button class="button ghost small" id="deleteConversation">删除这段记录</button></div>`;
    $("#deleteConversation").addEventListener("click", () => confirmAction({
      title: "删除这段聊天记录？", message: "仅删除管理软件中的本机副本，不会影响微信原始聊天，也不会损伤机器人的记忆数据。", confirmText: "删除记录",
      onConfirm: async () => {
        await api("delete_conversation", item.conversation_id);
        state.selectedConversation = null;
        $("#messageList").innerHTML = '<div class="empty-state compact"><p>请选择聊天记录</p></div>';
        $("#conversationInspector").innerHTML = '<span class="eyebrow">聊天信息</span><h2>尚未选择</h2>';
        await loadConversations(); toast("聊天记录副本已删除。 ");
      },
    }));
  }

  async function loadSchedules() {
    try {
      const data = await api("get_schedules");
      state.schedules = data.items || [];
      $("#scheduleTotal").textContent = state.schedules.length;
      $("#scheduleRunning").textContent = data.running || 0;
      $("#schedulePaused").textContent = data.paused || 0;
      renderSchedules();
    } catch (error) { reportError(error); }
  }

  function renderSchedules() {
    $("#scheduleRows").innerHTML = state.schedules.length ? state.schedules.map((item) => `
      <tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.source)}</td><td>${escapeHtml(item.target)}</td><td>${escapeHtml(item.schedule_text)}</td><td>${escapeHtml(shortTime(item.next_run_time))}</td><td><span class="task-status ${item.enabled ? "running" : ""}">${item.status}</span></td><td><div class="row-actions"><button class="row-action" data-action="toggle" data-id="${escapeHtml(item.id)}">${item.enabled ? "暂停" : "开启"}</button><button class="row-action" data-action="edit" data-id="${escapeHtml(item.id)}">改时间</button><button class="row-action delete" data-action="delete" data-id="${escapeHtml(item.id)}">删除</button></div></td></tr>`).join("") : '<tr><td colspan="7" class="table-empty">当前没有业务定时任务</td></tr>';
    $$(".row-action", $("#scheduleRows")).forEach((button) => button.addEventListener("click", () => scheduleAction(button.dataset.action, button.dataset.id)));
  }

  async function scheduleAction(action, id) {
    const item = state.schedules.find((value) => value.id === id);
    if (!item) return;
    if (action === "toggle") {
      try { await api("set_schedule_enabled", id, !item.enabled); await loadSchedules(); toast(item.enabled ? "任务已暂停。 " : "任务已开启。 "); }
      catch (error) { reportError(error); }
      return;
    }
    if (action === "edit") {
      const parts = item.cron_expression.split(" ");
      const clock = parts.length === 5 && Number.isFinite(Number(parts[0])) && Number.isFinite(Number(parts[1])) ? `${String(parts[1]).padStart(2, "0")}:${String(parts[0]).padStart(2, "0")}` : "08:00";
      confirmAction({ title: "修改执行时间", message: `${item.name}当前安排为“${item.schedule_text}”。`, confirmText: "保存时间", field: {label: "每天执行时间", type: "time", value: clock}, onConfirm: async (value) => { await api("update_schedule_time", id, value); await loadSchedules(); toast("执行时间已更新。 "); } });
      return;
    }
    confirmAction({ title: "删除定时任务？", message: `“${item.name}”删除后将停止执行，相关功能的其他设置不受影响。`, confirmText: "删除任务", onConfirm: async () => { await api("delete_schedule", id); await loadSchedules(); toast("定时任务已删除。 "); } });
  }

  async function loadActivity() {
    try {
      const items = await api("get_activity");
      $("#activityList").innerHTML = items.length ? items.map((item) => `<article class="activity-item ${escapeHtml(item.status)}"><time class="activity-time">${escapeHtml(fullTime(item.time))}</time><span class="activity-node">${item.status === "failed" ? "!" : "✓"}</span><div class="activity-title"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.status === "failed" ? "操作未完成，请按错误码处理" : "状态已记录")}</small></div><span class="activity-code">${escapeHtml(item.code || "")}</span></article>`).join("") : '<div class="empty-state compact"><p>当前还没有运行记录</p></div>';
    } catch (error) { reportError(error); }
  }

  let modalResolve = null;
  function confirmAction({title, message, confirmText = "确认", field = null, onConfirm}) {
    $("#modalTitle").textContent = title;
    $("#modalMessage").textContent = message;
    $("#modalConfirm").textContent = confirmText;
    $("#modalField").innerHTML = field ? `<label class="modal-field"><span>${escapeHtml(field.label)}</span><input id="modalInput" type="${escapeHtml(field.type || "text")}" value="${escapeHtml(field.value || "")}"></label>` : "";
    $("#modalBackdrop").hidden = false;
    $("#modalCancel").focus();
    modalResolve = async (confirmed) => {
      if (!confirmed) { $("#modalBackdrop").hidden = true; return; }
      const confirm = $("#modalConfirm"); confirm.disabled = true;
      try { await onConfirm(field ? $("#modalInput").value : undefined); $("#modalBackdrop").hidden = true; }
      catch (error) { reportError(error); }
      finally { confirm.disabled = false; }
    };
  }

  function requiredValues(form) {
    const missing = $$('[required]', form).find((input) => !input.value.trim());
    if (missing) { missing.focus(); toast("请填写所有必填项。 ", "error"); return false; }
    return true;
  }

  async function saveBrand(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!requiredValues(form)) return;
    const button = $('button[type="submit"]', form); button.disabled = true;
    try {
      const data = await api("save_brand", Object.fromEntries(new FormData(form)));
      state.bootstrap.brand = data;
      applyBrand(data);
      $("#brandNote").textContent = "已保存并统一更新。 ";
    } catch (error) { reportError(error); $("#brandNote").textContent = error.message; }
    finally { button.disabled = false; }
  }

  async function saveProvider(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!requiredValues(form)) return;
    const button = $('button[type="submit"]', form); button.disabled = true;
    try {
      const data = await api("save_provider", Object.fromEntries(new FormData(form)));
      state.bootstrap.provider = data;
      $("#apiKeyHint").textContent = "已安全保存；留空不会覆盖";
      form.elements.api_key.value = "";
      $("#providerNote").textContent = "已保存，下次启动后应用。 ";
    } catch (error) { reportError(error); $("#providerNote").textContent = error.message; }
    finally { button.disabled = false; }
  }

  function applyBrand(brand) {
    $("#brandName").textContent = brand.display_name;
    $("#brandMark").textContent = (brand.short_name || brand.display_name || "机").slice(0, 1);
    document.title = `${brand.display_name} · 机器人管理`;
    const form = $("#brandForm");
    for (const key of ["display_name", "short_name", "persona_name", "welcome_text", "admin_id"]) if (form.elements[key]) form.elements[key].value = brand[key] || "";
  }

  function fillSettings(data) {
    applyBrand(data.brand);
    const provider = data.provider || {};
    const form = $("#providerForm");
    form.elements.api_base.value = provider.api_base || "";
    form.elements.model.value = provider.model || "";
    $("#apiKeyHint").textContent = provider.api_key_configured ? "已安全保存；留空不会覆盖" : "尚未配置";
    $("#versionText").textContent = `v${data.version}`;
    $("#settingsVersion").textContent = `v${data.version}`;
    $("#installPath").textContent = data.install_root;
    const update = data.update || {};
    $("#updateStatus").textContent = update.enabled ? "稳定通道" : "在线更新未启用";
    $("#checkUpdate").disabled = !update.enabled;
    if (update.result) {
      const succeeded = update.result.status === "success";
      window.setTimeout(() => toast(
        succeeded ? `已更新到 v${update.result.version || data.version}` : "上次更新未能完成，软件已恢复到原版本。",
        succeeded ? "success" : "error",
      ), 350);
    }
  }

  async function refreshUpdate(manual = true) {
    const button = $("#checkUpdate");
    button.disabled = true;
    button.textContent = "检查中…";
    try {
      const data = await api("check_update");
      if (data.busy) return;
      state.availableUpdate = data.release || null;
      if (!data.enabled) {
        $("#updateStatus").textContent = "在线更新未启用";
      } else if (!data.release) {
        $("#updateStatus").textContent = "已是最新版本";
        if (manual) toast("当前已经是最新版本。 ");
      } else {
        const release = data.release;
        $("#updateStatus").textContent = `发现 v${release.version}`;
        confirmAction({
          title: `发现新版本 v${release.version}`,
          message: `${release.title || "新版本已就绪"}${release.notes?.length ? `：${release.notes.join("；")}` : ""}。安装前请先停止机器人，更新失败会自动恢复原版本。`,
          confirmText: "下载并安装",
          onConfirm: async () => {
            if (state.status?.state !== "STOPPED") {
              toast("请先在运行中心停止机器人，再安装更新。", "error");
              return;
            }
            button.textContent = "准备更新…";
            await api("install_update", release.version);
          },
        });
      }
    } catch (error) {
      $("#updateStatus").textContent = "检查失败";
      if (manual) reportError(error);
    } finally {
      button.disabled = !(state.bootstrap?.update?.enabled);
      button.textContent = state.availableUpdate ? "查看更新" : "检查更新";
    }
  }

  function bindSearch(boxSelector, inputSelector, callback) {
    const input = $(inputSelector);
    const box = $(boxSelector);
    const update = () => box.classList.toggle("has-value", Boolean(input.value));
    input.addEventListener("compositionstart", () => { state.composing = true; });
    input.addEventListener("compositionend", () => { state.composing = false; update(); callback(); });
    input.addEventListener("input", debounce(() => { update(); if (!state.composing) callback(); }));
    $(".clear-search", box).addEventListener("click", () => { input.value = ""; update(); input.focus(); callback(); });
  }

  function bindEvents() {
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.page)));
    $("#globalRefresh").addEventListener("click", refreshStatus);
    $("#primaryAction").addEventListener("click", runPrimaryAction);
    $("#retryEnvironment").addEventListener("click", checkEnvironment);
    $("#settingsCheckWechat").addEventListener("click", checkEnvironment);
    $("#chooseWechat").addEventListener("click", chooseWechat);
    $("#settingsChooseWechat").addEventListener("click", chooseWechat);
    $("#downloadWechat").addEventListener("click", async () => { try { await api("open_wechat_download"); } catch (error) { reportError(error); } });
    bindSearch("#featureSearchBox", "#featureSearch", renderFeatureList);
    bindSearch("#historySearchBox", "#historySearch", loadConversations);
    $$("#historyKinds button").forEach((button) => button.addEventListener("click", () => {
      state.historyKind = button.dataset.kind;
      $$("#historyKinds button").forEach((item) => item.classList.toggle("active", item === button));
      loadConversations();
    }));
    $("#historyFilter").addEventListener("click", loadConversations);
    $("#exportHistory").addEventListener("click", async () => { try { const result = await api("export_history", "csv"); if (!result?.cancelled) toast(`已导出到 ${result.path}`); } catch (error) { reportError(error); } });
    $("#refreshSchedules").addEventListener("click", loadSchedules);
    $("#refreshActivity").addEventListener("click", loadActivity);
    $("#brandForm").addEventListener("submit", saveBrand);
    $("#providerForm").addEventListener("submit", saveProvider);
    $("#openInstall").addEventListener("click", async () => { try { await api("open_install_folder"); } catch (error) { reportError(error); } });
    $("#cleanHistory").addEventListener("click", () => confirmAction({ title: "按日期清理聊天记录", message: "将删除所选日期以前的本机记录副本，此操作无法撤销。", confirmText: "开始清理", field: {label: "保留此日期及之后的记录", type: "date", value: ""}, onConfirm: async (value) => { const result = await api("delete_history_before", value); toast(`已清理 ${result.deleted} 条记录。 `); if (state.activePage === "history") await loadConversations(); } }));
    $("#checkUpdate").addEventListener("click", () => refreshUpdate(true));
    $("#modalCancel").addEventListener("click", () => modalResolve?.(false));
    $("#modalConfirm").addEventListener("click", () => modalResolve?.(true));
    $("#modalBackdrop").addEventListener("click", (event) => { if (event.target === event.currentTarget) modalResolve?.(false); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("#modalBackdrop").hidden) modalResolve?.(false); });
  }

  async function initialize() {
    bindEvents();
    try {
      state.bootstrap = await api("bootstrap");
      fillSettings(state.bootstrap);
      renderStatus(state.bootstrap.status);
      window.setInterval(refreshStatus, 1500);
      if (state.bootstrap.update?.enabled) window.setTimeout(() => refreshUpdate(false), 1800);
    } catch (error) {
      reportError(error);
      $("#stateTitle").textContent = "软件未能完成初始化";
      $("#stateMessage").textContent = "请重新打开软件；若问题持续，请联系技术支持。 ";
    }
  }

  function previewApi() {
    const status = {
      state: "RUNNING", title: "机器人可用", message: "微信连接和全部功能运行正常。", busy: false, online: true, can_start: false, can_stop: true, error: null,
      environment: {ready: true, code: "10000", title: "微信环境正常", message: "已检测到兼容微信。", solutions: [], path: "C:\\Program Files\\Tencent\\Weixin\\Weixin.exe", version: "4.1.8.27", process_running: true},
      runtime: {engine_ready: true, connection_ready: true, wechat_identity: "wxid_demo_account", hook_ready: true, upstream_connected: true, heartbeat_fresh: true},
    };
    const feature = {id: "feature-demo", display_name: "短剧解析", enabled: true, configurable: true, field_count: 6, model_field_count: 1, pages: []};
    const ok = (data, message = "") => Promise.resolve({ok: true, data, message});
    return {
      bootstrap: () => ok({version: "0.8.0", install_root: "D:\\姜小妹机器人", status, brand: {display_name: "姜小妹机器人", short_name: "姜小妹", persona_name: "姜小妹", welcome_text: "你好，我是姜小妹机器人。", admin_id: "wxid_admin"}, provider: {configured: true, api_base: "https://api.example.com/v1", model: "chat-model", api_key_configured: true}, update: {enabled: true, channel: "stable", result: null}}),
      get_status: () => ok(status), check_environment: () => ok(status), start_robot: () => ok({accepted: true}), stop_robot: () => ok({accepted: true}), select_wechat: () => ok(status), open_wechat_download: () => ok({opened: true}),
      get_features: () => ok({plugins: [feature, {id: "feature-music", display_name: "音乐点播", enabled: true, configurable: true, field_count: 4, model_field_count: 0}, {id: "feature-reply", display_name: "自动回复", enabled: false, configurable: true, field_count: 8, model_field_count: 2}, {id: "feature-image", display_name: "图片生成", enabled: true, configurable: true, field_count: 7, model_field_count: 3}, {id: "feature-report", display_name: "群聊日报", enabled: true, configurable: false, field_count: 0, model_field_count: 0}], engine_online: true}),
      get_feature: (id) => ok({...feature, id, display_name: id === "feature-music" ? "音乐点播" : "短剧解析", basic_schema: {enabled_notice: {type: "bool", description: "发送解析提示", default: true}, player_url: {type: "string", description: "播放页面地址", hint: "用于生成用户可打开的播放页面"}, recommendation: {type: "object", description: "推荐设置", items: {limit: {type: "int", description: "每次推荐数量", default: 10}, media_type: {type: "string", description: "内容类型", options: ["短剧", "电影"]}}}}, model_schema: {summary_provider_id: {type: "string", description: "内容总结模型"}}, config: {enabled_notice: true, player_url: "https://example.com/player", recommendation: {limit: 10, media_type: "短剧"}, summary_provider_id: "chat-model"}, revision: "preview", providers: [], personas: [], engine_online: true}),
      set_feature_enabled: (_id, enabled) => ok({enabled, message: "功能状态已立即更新。"}), save_feature: () => ok({revision: "preview-2", message: "配置已保存并立即应用。"}),
      get_conversations: () => ok({items: [{conversation_id: "51632940287@chatroom", type: "group", display_name: "旅行交流群", message_count: 128, last_timestamp: Date.now() / 1000}, {conversation_id: "wxid_zhangsan", type: "private", display_name: "张三", message_count: 34, last_timestamp: Date.now() / 1000 - 3600}, {conversation_id: "18310516375@chatroom", type: "group", display_name: "短剧分享群", message_count: 76, last_timestamp: Date.now() / 1000 - 7200}], summary: {messages: 238, conversations: 3, private_messages: 34, group_messages: 204}}),
      get_messages: () => ok([{role: "user", sender_id: "wxid_zhangsan", sender_name: "张三", content: "帮我找一下今天更新的短剧", timestamp: Date.now() / 1000 - 180}, {role: "assistant", sender_id: "bot", sender_name: "机器人", content: "找到了 8 部今天更新的内容，我已经整理成播放页发到群里。", timestamp: Date.now() / 1000 - 160}, {role: "user", sender_id: "wxid_lisi", sender_name: "李四", content: "收到，这样看起来很方便", timestamp: Date.now() / 1000 - 120}]),
      delete_conversation: () => ok({deleted: 34}), delete_history_before: () => ok({deleted: 120}), export_history: () => ok({path: "D:\\聊天记录.csv"}),
      get_schedules: () => ok({running: 3, paused: 1, items: [{id: "1", name: "天气预报", source: "天气", target: "微信群 · 51632940287", enabled: true, status: "运行中", schedule_text: "每天 08:00", cron_expression: "0 8 * * *", next_run_time: new Date(Date.now() + 86400000).toISOString()}, {id: "2", name: "短剧推荐", source: "短剧", target: "微信群 · 18310516375", enabled: true, status: "运行中", schedule_text: "每天 20:00", cron_expression: "0 20 * * *", next_run_time: new Date(Date.now() + 36000000).toISOString()}, {id: "3", name: "疯狂星期四", source: "趣味内容", target: "微信群 · 51632940287", enabled: false, status: "已暂停", schedule_text: "周四 09:00", cron_expression: "0 9 * * thu", next_run_time: ""}, {id: "4", name: "每日签到", source: "签到", target: "个人聊天 · wxid_admin", enabled: true, status: "运行中", schedule_text: "每天 08:30", cron_expression: "30 8 * * *", next_run_time: new Date(Date.now() + 50000000).toISOString()}]}),
      set_schedule_enabled: (_id, enabled) => ok({updated: true, enabled}), update_schedule_time: () => ok({updated: true}), delete_schedule: () => ok({deleted: true}),
      get_activity: () => ok([{time: new Date().toISOString(), title: "全部功能加载完成", event: "FUNCTION_LOADED", status: "success", code: ""}, {time: new Date(Date.now() - 1500).toISOString(), title: "微信连接成功", event: "CONNECTION_CONNECTED", status: "success", code: ""}, {time: new Date(Date.now() - 4000).toISOString(), title: "机器人功能已就绪", event: "CORE_READY", status: "success", code: ""}, {time: new Date(Date.now() - 7000).toISOString(), title: "微信环境检查通过", event: "WECHAT_READY", status: "success", code: ""}]),
      save_brand: (values) => ok(values, "机器人名称和回复身份已统一更新。"), save_provider: (values) => ok({...values, configured: true, api_key_configured: true}), open_install_folder: () => ok({opened: true}),
      check_update: () => ok({enabled: true, current: true, release: null}), install_update: (version) => ok({accepted: true, version}),
    };
  }

  if (new URLSearchParams(window.location.search).has("preview")) {
    window.pywebview = {api: previewApi()};
    window.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    window.addEventListener("pywebviewready", initialize, {once: true});
  }
})();
