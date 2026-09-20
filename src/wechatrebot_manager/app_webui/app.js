(() => {
  "use strict";

  const state = {
    bootstrap: null,
    status: null,
    activePage: "home",
    featuresLoaded: false,
    featuresEngineOnline: false,
    features: [],
    featureAudience: "all",
    helpAudience: "all",
    selectedFeature: null,
    featureDetail: null,
    featureTab: "basic",
    featureDirty: false,
    featureSaving: false,
    featureLoad: 0,
    featurePrefetch: 0,
    featureDetails: {},
    featureFeedback: "",
    groupsLoaded: false,
    groupsBusy: false,
    groups: [],
    groupAccount: "",
    groupUpdatedAt: "",
    groupStale: false,
    selectedGroup: null,
    groupMembers: {},
    groupMemberLoad: 0,
    groupRefreshRetryTimer: 0,
    groupPolicies: {},
    groupInspectorView: "policy",
    chatManagementTab: "groups",
    contactsLoaded: false,
    contactsBusy: false,
    contacts: [],
    contactAccount: "",
    contactUpdatedAt: "",
    contactStale: false,
    contactTruncated: false,
    contactPage: 1,
    selectedContact: null,
    contactDetails: {},
    privatePolicies: {},
    contactLoad: 0,
    historyKind: "all",
    conversations: [],
    selectedConversation: null,
    selectedMessages: [],
    selectedSenderKey: "",
    historyLoad: 0,
    messageLoad: 0,
    historyPolling: false,
    historyAutoDate: "",
    historyHasMore: false,
    historyLoadingOlder: false,
    historyOlderLoad: 0,
    historyMessageQuery: "",
    schedules: [],
    scheduledContext: null,
    scheduledEditor: null,
    scheduledDirty: false,
    scheduledBusy: false,
    scheduledLoad: 0,
    scheduledTimeLoad: 0,
    scheduledChatFilter: null,
    scheduledTargetTab: "group",
    scheduledReturnDraft: false,
    experience: null,
    maintenance: null,
    responseTraces: [],
    polling: false,
    startPending: false,
    composing: false,
    availableUpdate: null,
    updateChecking: false,
    updateCheckTimer: 0,
    settingsSection: "model",
    coreSettingsTab: "search",
    webSearch: null,
    webSearchDirty: false,
    providerScreen: "overview",
    providerDirty: false,
    providerBaseline: "",
    providerCleanStatus: "尚未保存",
    providerModels: [],
    providerModelFiltering: false,
    providerModelFocus: -1,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const CONTACT_PAGE_SIZE = 100;
  const HISTORY_PAGE_SIZE = 20;
  const HISTORY_PRELOAD_THRESHOLD = 120;
  const GROUP_DIRECTORY_RETRY_DELAYS_MS = [1500, 4000];
  const GROUP_MEMBER_RETRY_DELAYS_MS = [800, 2000];
  const AVATAR_RETRY_DELAYS_MS = [1200, 4000];
  let groupMemberRequestQueue = Promise.resolve();
  const groupMemberLoadsInFlight = new Set();
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  const coreSettings = window.createRobotCoreSettings({
    read: () => api("get_robot_core"),
    request: (method, endpoint, payload) => api("request_robot_core", method, endpoint, payload),
    save: (config, revision, identity) => api("save_robot_core", config, revision, identity),
    applyBrand: (brand) => {
      state.bootstrap.brand = brand;
      applyBrand(brand);
    },
    schemaMarkup, configFromForm, escapeHtml, confirmAction, toast, reportError, growTextarea,
  });

  function safeImageUrl(value) {
    try {
      const parsed = new URL(String(value || "").trim());
      return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
    } catch (_error) { return ""; }
  }

  function avatarMarkup(className, name, value) {
    const fallback = String(name || "聊").trim().slice(0, 1) || "聊";
    const source = safeImageUrl(value);
    return `<span class="${className} avatar-frame" aria-hidden="true"><span class="avatar-fallback">${escapeHtml(fallback)}</span>${source ? `<img src="${escapeHtml(source)}" data-avatar-source="${escapeHtml(source)}" alt="" loading="lazy">` : ""}</span>`;
  }

  function bindAvatarFallbacks(root = document) {
    $$(".avatar-frame img", root).forEach((image) => {
      if (image.dataset.avatarFallbackBound === "true") return;
      image.dataset.avatarFallbackBound = "true";
      image.addEventListener("error", () => {
        const frame = image.closest(".avatar-frame");
        const retry = Number(image.dataset.avatarRetry || "0");
        if (!frame || retry >= AVATAR_RETRY_DELAYS_MS.length) {
          image.remove();
          return;
        }

        image.dataset.avatarRetry = String(retry + 1);
        image.hidden = true;
        window.setTimeout(() => {
          if (!image.isConnected) return;
          try {
            const source = new URL(image.dataset.avatarSource || image.src);
            source.searchParams.set("avatar_retry", `${Date.now()}-${retry}`);
            image.src = source.href;
            image.hidden = false;
          } catch (_error) {
            image.remove();
          }
        }, AVATAR_RETRY_DELAYS_MS[retry]);
      });
    });
  }

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
    const detail = typeof product?.detail === "string" ? product.detail.trim() : "";
    const message = product
      ? `${product.title || "操作未完成"}${detail ? `：${detail}` : ""}${product.code ? `（${product.code}）` : ""}`
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

  function localDateInputValue(value = new Date()) {
    const year = value.getFullYear();
    const month = String(value.getMonth() + 1).padStart(2, "0");
    const day = String(value.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function initializeHistoryDates() {
    const today = localDateInputValue();
    if (!$("#historyStart").value) $("#historyStart").value = today;
    if (!$("#historyEnd").value) $("#historyEnd").value = today;
    state.historyAutoDate = today;
  }

  function rollHistoryDatesForward() {
    const today = localDateInputValue();
    const previous = state.historyAutoDate;
    if (!previous || previous === today) return false;
    state.historyAutoDate = today;
    const start = $("#historyStart");
    const end = $("#historyEnd");
    if (start.value !== previous || end.value !== previous) return false;
    start.value = today;
    end.value = today;
    return true;
  }

  function setStage(element, mode, label) {
    element.classList.remove("done", "current", "problem");
    if (mode) element.classList.add(mode);
    $("em", element).textContent = label;
  }

  const STARTUP_STATES = new Set([
    "CHECKING", "CORE_STARTING", "CORE_READY", "CONNECTION_STARTING",
  ]);

  function startingStatus(status) {
    return {
      ...(status || {}),
      state: "CORE_STARTING",
      title: "正在启动机器人",
      message: "正在准备机器人功能，请稍候。",
      busy: true,
      online: false,
      can_start: false,
      can_stop: false,
      error: null,
      environment: status?.environment || {},
      runtime: status?.runtime || {},
    };
  }

  function reconcileStartPending(status) {
    if (!state.startPending) return status;
    const current = status?.state || "STOPPED";
    if (current === "STOPPED" && status?.environment?.ready && !status?.error) {
      return startingStatus(status);
    }
    if (!STARTUP_STATES.has(current)) state.startPending = false;
    return status;
  }

  function renderStatus(rawStatus) {
    const status = reconcileStartPending(rawStatus);
    state.status = status;
    const runtime = status.runtime || {};
    const environment = status.environment || {};
    const current = status.state || "STOPPED";
    const online = Boolean(status.online);
    const waiting = ["WAITING_WECHAT", "VERIFYING_CONNECTION", "STOP_WAIT_WECHAT"].includes(current);
    const problem = current === "ERROR";
    const active = !["STOPPED", "ERROR"].includes(current);
    const starting = state.startPending || ["CORE_STARTING", "CORE_READY", "CONNECTION_STARTING"].includes(current);
    const wechatLoggedIn = Boolean(runtime.wechat_identity);

    $("#stateTitle").textContent = status.title;
    $("#stateMessage").textContent = status.message;
    $("#accountText").textContent = runtime.wechat_identity ? `已登录：${runtime.wechat_identity}` : "尚未连接";
    $("#lastCheckText").textContent = `检查于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false})}`;

    const global = $("#globalStatus");
    global.className = `status-badge ${online ? "online" : problem ? "error" : waiting ? "warning" : "checking"}`;
    global.innerHTML = `<span></span>${escapeHtml(online ? "机器人可用" : problem ? "需要处理" : waiting ? status.title : active ? "正在启动" : environment.ready ? "等待启动" : "环境未就绪")}`;
    const sideDot = $("#sideHealthDot");
    sideDot.className = `health-dot ${online ? "online" : problem ? "error" : waiting || !environment.ready ? "warning" : ""}`;
    $("#sideHealthText").textContent = online ? "机器人在线" : problem ? "需要处理" : waiting ? "等待操作" : active ? (current === "STOPPING" ? "正在停止" : "正在启动") : "当前未运行";

    const orbit = $("#statusOrbit");
    orbit.dataset.state = online ? "online" : problem ? "error" : waiting ? "waiting" : status.busy || active ? "starting" : "stopped";
    const primary = $("#primaryAction");
    const stopMode = status.can_stop || online;
    const primaryLabel = starting ? "启动中…" : stopMode ? (current === "STOP_WAIT_WECHAT" ? "等待关闭" : "停止运行") : "启动";
    primary.innerHTML = `${icon(starting ? "i-refresh" : stopMode ? "i-stop" : "i-play")}<span>${primaryLabel}</span>`;
    primary.dataset.action = stopMode ? "stop" : "start";
    primary.disabled = current === "STOP_WAIT_WECHAT" || Boolean(status.busy) || (!status.can_start && !status.can_stop);
    primary.setAttribute("aria-busy", String(Boolean(status.busy)));
    primary.setAttribute("aria-label", primaryLabel);
    primary.title = online ? "安全停止机器人" : primaryLabel;

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
    const coreStarted = Boolean(runtime.engine_ready);
    setStage($("#stageCore"), coreStarted ? "done" : active ? "current" : "", coreStarted ? "已就绪" : active ? "启动中" : "未启动");
    const connectionStarting = current === "CONNECTION_STARTING";
    const connectionMode = online || wechatLoggedIn ? "done" : waiting || connectionStarting ? "current" : problem && coreStarted ? "problem" : "";
    setStage($("#stageConnection"), connectionMode, online ? "已连接" : wechatLoggedIn ? "已登录" : connectionStarting ? "正在打开" : waiting ? "连接中" : problem && coreStarted ? "未完成" : "未连接");

    const canForceStop = current !== "STOPPED" && current !== "ERROR" && !status.busy;
    const coreStop = $("#stageCoreStop");
    const connectionStop = $("#stageConnectionStop");
    if (coreStop) {
      coreStop.hidden = !(canForceStop && (coreStarted || active));
      coreStop.disabled = !canForceStop;
      coreStop.setAttribute("aria-label", "强制停止机器人功能");
    }
    if (connectionStop) {
      const connectionActive = online || wechatLoggedIn || runtime.connection_ready
        || ["WAITING_WECHAT", "VERIFYING_CONNECTION", "RUNNING", "STOP_WAIT_WECHAT"].includes(current);
      connectionStop.hidden = !(canForceStop && connectionActive);
      connectionStop.disabled = !canForceStop;
      connectionStop.setAttribute("aria-label", "强制停止消息连接");
    }

    $("#signalWechat").textContent = environment.ready ? "版本兼容" : "未就绪";
    $("#signalWechatDetail").textContent = environment.ready ? `${environment.version || "已检测"} · ${environment.process_running ? "检测到微信进程" : "尚未运行"}` : "请安装或重新选择微信";
    $("#signalCore").textContent = runtime.engine_ready ? "运行正常" : active ? "正在启动" : "未启动";
    $("#signalMessage").textContent = online ? "通信正常" : wechatLoggedIn ? "已登录，功能启动中" : runtime.connection_ready ? "正在确认" : "未连接";
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
    const action = button.dataset.action === "stop" ? "stop" : "start";
    button.disabled = true;
    if (action === "start") {
      state.startPending = true;
      renderStatus(startingStatus(state.status));
    }
    try {
      const result = await api(action === "stop" ? "stop_robot" : "start_robot");
      if (action === "start" && result?.accepted === false) state.startPending = false;
      window.setTimeout(refreshStatus, 250);
    } catch (error) {
      if (action === "start") state.startPending = false;
      reportError(error);
      await refreshStatus();
    }
  }

  function runForceStop() {
    confirmAction({
      title: "强制停止机器人？",
      message: "将立即结束机器人功能和消息连接，不等待微信自行关闭。正在处理的消息可能会中断。",
      confirmText: "强制停止",
      pendingText: "正在强制停止…",
      onConfirm: async () => {
        const result = await api("force_stop_robot");
        if (result?.accepted === false) throw new Error(result.message || "正在处理上一个操作。 ");
        window.setTimeout(refreshStatus, 150);
      },
    });
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
    home: "运行中心", features: "功能中心", groups: "群聊&私聊管理", history: "消息中心",
    schedules: "定时任务", activity: "运行记录", settings: "设置", help: "帮助",
  };

  const SETTINGS_SECTIONS = new Set(["model", "core", "system"]);

  function providerFormSnapshot() {
    const form = $("#providerForm");
    if (!form) return "";
    const values = Object.fromEntries(new FormData(form));
    delete values.api_key;
    return JSON.stringify(values);
  }

  function markProviderClean(status = "尚未保存") {
    state.providerBaseline = providerFormSnapshot();
    state.providerDirty = false;
    state.providerCleanStatus = status;
    const badge = $("#providerEditorStatus");
    if (badge) {
      badge.className = `status-badge ${status === "已保存" ? "success" : "checking"}`;
      badge.textContent = status;
    }
  }

  function updateProviderDirty() {
    if (state.providerScreen !== "editor") return;
    const form = $("#providerForm");
    const keyChanged = Boolean(form?.elements.api_key.value);
    state.providerDirty = keyChanged || providerFormSnapshot() !== state.providerBaseline;
    const badge = $("#providerEditorStatus");
    badge.className = `status-badge ${state.providerDirty ? "warning" : state.providerCleanStatus === "已保存" ? "success" : "checking"}`;
    badge.textContent = state.providerDirty ? "有未保存修改" : state.providerCleanStatus;
  }

  function showProviderOverview(focus = false) {
    state.providerScreen = "overview";
    state.providerDirty = false;
    $("#providerOverview").hidden = false;
    $("#providerEditorPage").hidden = true;
    if (focus) $("#providerOverviewTitle")?.focus();
  }

  function showProviderEditor(focus = true) {
    state.providerScreen = "editor";
    $("#providerOverview").hidden = true;
    $("#providerEditorPage").hidden = false;
    if (focus) $("#providerEditorTitle")?.focus();
  }

  function guardProviderDraft(next) {
    if (!state.providerDirty || state.providerScreen !== "editor") return false;
    confirmAction({
      title: "放弃模型配置？",
      message: "接口地址、模型名称或密钥还没有保存。返回后这些修改会被清空。",
      confirmText: "放弃修改",
      onConfirm: async () => {
        showProviderOverview();
        await next();
      },
    });
    return true;
  }

  function requestProviderOverview() {
    if (guardProviderDraft(() => showProviderOverview(true))) return;
    showProviderOverview(true);
  }

  function selectSettingsSection(section) {
    if (section !== state.settingsSection && coreSettings.guard(() => selectSettingsSection(section))) return;
    if (section !== "model" && guardProviderDraft(() => setSettingsSection(section, true))) return;
    setSettingsSection(section, true);
  }

  function setSettingsSection(section, focus = false) {
    if (!SETTINGS_SECTIONS.has(section)) return;
    state.settingsSection = section;
    const page = $("#page-settings");
    page.dataset.settingsSection = section;
    $$('[data-settings-section]', page).forEach((item) => {
      const matches = item.dataset.settingsSection === section;
      const coreMemory = section === "core" && item.dataset.settingsSection === "core" && item.dataset.coreSubsection === "memory";
      item.hidden = !matches || (coreMemory && state.coreSettingsTab !== "memory");
    });
    $$("#settingsTabs [data-settings-tab]").forEach((button) => {
      const active = button.dataset.settingsTab === section;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
      if (focus && active) button.focus();
    });
    if (section === "model") {
      $("#providerOverview").hidden = state.providerScreen !== "overview";
      $("#providerEditorPage").hidden = state.providerScreen !== "editor";
    }
    if (section === "core") void coreSettings.load();
    if (section === "core") {
      setCoreSettingsTab(state.coreSettingsTab, false);
      void loadWebSearchSettings();
    }
  }

  function setCoreSettingsTab(tab, focus = false) {
    if (!["search", "conversation", "memory"].includes(tab)) return;
    state.coreSettingsTab = tab;
    const page = $("#page-settings");
    $$('[data-core-settings-tab]', page).forEach((button) => {
      const active = button.dataset.coreSettingsTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      if (focus && active) button.focus();
    });
    $$('[data-core-subsection]', page).forEach((item) => {
      item.hidden = item.dataset.coreSubsection !== tab;
    });
    if (tab === "memory") {
      const experience = state.experience;
      if (experience) fillExperience(experience);
    }
    if (tab === "conversation") void coreSettings.load();
  }

  async function navigate(page) {
    if (!PAGE_TITLES[page] || state.featureSaving || state.scheduledBusy || coreSettings.busy) return;
    if (page !== state.activePage && coreSettings.guard(() => navigate(page))) return;
    if (page !== state.activePage && state.featureDirty) { discardFeatureChanges(() => navigate(page)); return; }
    if (page !== state.activePage && guardProviderDraft(() => navigate(page))) return;
    if (page !== state.activePage && state.activePage === "schedules" && guardScheduledDraft(() => navigate(page))) return;
    const leavingGroupManagement = state.activePage === "groups" && page !== "groups";
    state.activePage = page;
    if (leavingGroupManagement) {
      window.clearTimeout(state.groupRefreshRetryTimer);
      state.groupRefreshRetryTimer = 0;
      state.groupMemberLoad += 1;
    }
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
    $$(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
    $("#pageTitle").textContent = PAGE_TITLES[page];
    $("#pageContextLabel").hidden = page === "schedules";
    $("#defaultTopActions").hidden = page === "schedules";
    $("#scheduleTopActions").hidden = page !== "schedules";
    document.title = `${PAGE_TITLES[page]} · ${state.bootstrap?.brand?.display_name || "机器人管理"}`;
    const content = $(".content-scroll");
    if (content) {
      content.classList.toggle("history-active", page === "history");
      content.scrollTop = 0;
    }
    if (page === "features" && !state.featuresLoaded) await loadFeatures();
    if (page === "groups") {
      if (state.chatManagementTab === "groups") await loadGroups(true, {background: state.groupsLoaded});
      if (state.chatManagementTab === "private" && !state.contactsLoaded) await loadContacts(false);
    }
    if (page === "history") {
      await loadConversations({refreshGroups: true});
      if (state.selectedConversation) await selectConversation(state.selectedConversation.conversation_id, {silent: true});
    }
    if (page === "schedules") { await loadSchedules(); if (state.scheduledReturnDraft && state.scheduledEditor) { state.scheduledReturnDraft = false; renderScheduledEditor(); } }
    if (page === "activity") await loadActivity();
    if (page === "help") {
      if (!state.featuresLoaded) await loadFeatures();
      renderHelp();
    }
    if (page === "settings") {
      setSettingsSection(state.settingsSection);
      if (!state.maintenance) await loadMaintenance();
    }
  }

  async function loadFeatures() {
    const list = $("#featureList");
    list.innerHTML = '<div class="loading-row">正在读取功能…</div>';
    try {
      const data = await api("get_features");
      state.features = data.plugins || [];
      state.featuresEngineOnline = Boolean(data.engine_online);
      state.featuresLoaded = true;
      $("#featureSummary").textContent = `${state.features.length} 项功能 · ${data.engine_online ? "设置可立即应用" : "设置将在下次启动时应用"}`;
      renderFeatureList();
      renderHelp();
      if (!state.selectedFeature && state.features.length) await openFeature(state.features[0].id);
      void prefetchFeatureDetails(++state.featurePrefetch);
    } catch (error) {
      list.innerHTML = '<div class="loading-row">功能暂时无法读取</div>';
      reportError(error);
    }
  }

  async function refreshFeatures() {
    state.featurePrefetch += 1;
    state.featuresLoaded = false;
    state.selectedFeature = null;
    state.featureDetail = null;
    state.featureDetails = {};
    coreSettings.invalidate();
    await loadFeatures();
    toast("功能准备情况已重新检查。 ");
  }

  async function prefetchFeatureDetails(generation) {
    const pending = state.features
      .map((item) => item.id)
      .filter((featureId) => featureId && !state.featureDetails[featureId]);
    let next = 0;
    const worker = async () => {
      while (next < pending.length && generation === state.featurePrefetch) {
        const featureId = pending[next++];
        try {
          const detail = await api("get_feature", featureId);
          if (generation !== state.featurePrefetch) return;
          if (!state.featureDetails[featureId]) state.featureDetails[featureId] = structuredClone(detail);
        } catch (_error) {
          // Background warming is intentionally quiet; opening the feature
          // still reports a useful error and offers the normal retry path.
        }
      }
    };
    await Promise.all(Array.from({length: Math.min(3, pending.length)}, () => worker()));
  }

  function renderFeatureList() {
    const keyword = $("#featureSearch").value.trim().toLocaleLowerCase("zh-CN");
    const audienceMatch = (item) => {
      const label = item.audience || "";
      if (state.featureAudience === "all") return true;
      return label.includes("常用");
    };
    const items = state.features.filter((item) => audienceMatch(item) && (!keyword || item.display_name.toLocaleLowerCase("zh-CN").includes(keyword)));
    const onlineText = state.featuresEngineOnline ? "设置可立即应用" : "设置将在下次启动时应用";
    $("#featureSummary").textContent = `${items.length} / ${state.features.length} 项功能 · ${onlineText}`;
    $("#featureList").innerHTML = items.length ? items.map((item) => `
      <button class="feature-item ${state.selectedFeature === item.id ? "active" : ""}" data-feature-id="${escapeHtml(item.id)}">
        <span><strong>${escapeHtml(item.display_name)}</strong><small>${escapeHtml(item.audience || "按需使用")} · ${escapeHtml(item.readiness || "已加载")}</small></span>
        <span class="feature-state enabled" title="已加载"></span>
      </button>`).join("") : '<div class="loading-row">没有符合条件的功能</div>';
    $$(".feature-item").forEach((button) => button.addEventListener("click", () => openFeature(button.dataset.featureId)));
  }

  function discardFeatureChanges(next) {
    confirmAction({title: "离开前丢弃修改？", message: "这些设置还没有保存。留在此页可继续编辑或保存。", confirmText: "丢弃修改并继续", onConfirm: async () => {
      state.featureDirty = false;
      if (state.featureDetail && state.featureBaseline) state.featureDetail.config = structuredClone(state.featureBaseline);
      renderFeatureDetail();
      await next();
    }});
  }

  function useFeatureDetail(source, afterSave = false, preserveTab = false) {
    const detail = structuredClone(source);
    detail.config = structuredClone(Object.keys(detail.advanced_config || {}).length ? detail.advanced_config : (detail.config || {}));
    state.featureDetail = detail;
    state.featureBaseline = structuredClone(detail.config);
    state.featureDirty = false;
    if (!afterSave) state.featureFeedback = "";
    const schemas = {basic: detail.basic_schema || {}, models: detail.model_schema || {}, advanced: detail.advanced_schema || detail.schema || {}};
    if (!preserveTab || !Object.keys(schemas[state.featureTab] || {}).length) {
      state.featureTab = Object.keys(schemas.basic).length ? "basic" : "models";
    }
  }

  async function openFeature(featureId, afterSave = false, force = false) {
    if (state.featureSaving && !afterSave) return;
    if (state.featureDirty && !afterSave) { discardFeatureChanges(() => openFeature(featureId, false, force)); return; }
    const load = ++state.featureLoad;
    const preserveTab = state.selectedFeature === featureId;
    state.selectedFeature = featureId;
    renderFeatureList();
    const cached = state.featureDetails[featureId];
    if (cached) {
      useFeatureDetail(cached, afterSave, preserveTab);
      renderFeatureDetail();
      const note = $("#featureNote");
      if (note && force) note.textContent = "正在重新读取最新设置…";
      // Switching back to a detail already read in this window is instant.
      // Saving and the explicit refresh button pass force=true and still read
      // the engine back for hot-apply verification.
      if (!force) return;
    } else {
      $("#featureDetail").innerHTML = '<div class="loading-row">正在读取功能设置…</div>';
    }
    try {
      const detail = await api("get_feature", featureId);
      if (load !== state.featureLoad) return;
      state.featureDetails[featureId] = structuredClone(detail);
      if (cached && state.featureDirty) return;
      useFeatureDetail(detail, afterSave, preserveTab);
      renderFeatureDetail();
    } catch (error) {
      if (load !== state.featureLoad) return;
      if (!cached) $("#featureDetail").innerHTML = '<div class="empty-state"><h2>设置暂时无法读取</h2><p>机器人未启动时仍可修改大部分基础设置，请稍后重试。</p></div>';
      else {
        const note = $("#featureNote");
        if (note) note.textContent = "暂时无法刷新，仍显示上次读取的设置。";
      }
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

  function fieldMarkup(key, field, value, path, namespace = "") {
    const type = field.type || "string";
    const uiControl = field._ui_control || "";
    const label = field.description || field.title || key.replaceAll("_", " ");
    const hint = field.hint || field.help || "";
    const encodedPath = escapeHtml(JSON.stringify(path));
    const encodedLabel = escapeHtml(label);
    const fieldId = namespace + "config-" + path.map((part) => encodeURIComponent(part)).join("--");
    const helpId = fieldId + "-help";
    let control = "";
    if (type === "bool") {
      control = `<span class="toggle"><input class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="bool" type="checkbox" ${value ? "checked" : ""}><label class="toggle-track" for="${fieldId}" aria-label="切换${encodedLabel}"></label><span>${value ? "已启用" : "已停用"}</span></span>`;
    } else if (type === "list" && ["line-list", "time-list"].includes(uiControl)) {
      const lines = Array.isArray(value) ? value.map(String).join("\n") : "";
      const placeholder = field.placeholder ? ` placeholder="${escapeHtml(field.placeholder)}"` : "";
      control = `<textarea class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="${escapeHtml(uiControl)}" rows="${uiControl === "time-list" ? 3 : 4}"${placeholder}>${escapeHtml(lines)}</textarea>`;
    } else if (type === "list" && Array.isArray(field.options)) {
      const optionNames = Array.isArray(field.labels) ? field.labels : (Array.isArray(field.options_label) ? field.options_label : field.options);
      const selected = Array.isArray(value) ? value.map(String) : [];
      const choices = [...field.options, ...selected.filter((item) => !field.options.some((option) => String(option) === item))];
      control = `<div class="option-grid">${choices.map((option, index) => `<label class="option-check"><input class="config-list-option" data-path="${encodedPath}" data-label="${encodedLabel}" value="${escapeHtml(String(option))}" type="checkbox" ${selected.includes(String(option)) ? "checked" : ""}><span>${escapeHtml(index < field.options.length ? (optionNames[index] ?? option) : `${option}（当前值）`)}</span></label>`).join("")}</div>`;
    } else if (Array.isArray(field.options)) {
      const optionNames = Array.isArray(field.labels) ? field.labels : (Array.isArray(field.options_label) ? field.options_label : field.options);
      const current = String(value ?? "");
      const unlisted = !field.options.some((option) => String(option) === current);
      control = `<select class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="${escapeHtml(type)}">${unlisted ? `<option value="${escapeHtml(current)}" selected>${escapeHtml(current || "未设置")}（当前值）</option>` : ""}${field.options.map((option, index) => `<option value="${escapeHtml(option)}" ${current === String(option) ? "selected" : ""}>${escapeHtml(optionNames[index] ?? option)}</option>`).join("")}</select>`;
    } else if (["list", "template_list"].includes(type)) {
      control = `<textarea class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="json" rows="5">${escapeHtml(JSON.stringify(value ?? [], null, 2))}</textarea>`;
    } else if (type === "text" || field.editor_mode || field.editor_language === "json") {
      control = `<textarea class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="${field.editor_language === "json" ? "json-string" : "string"}" rows="4">${escapeHtml(value ?? "")}</textarea>`;
    } else {
      const inputType = ["int", "float"].includes(type) || uiControl === "proxy-port" ? "number" : (field.secret ? "password" : "text");
      const step = type === "float" ? "any" : "1";
      const slider = field.slider && typeof field.slider === "object" ? field.slider : {};
      const numericRules = inputType === "number" ? `step="${escapeHtml(field.step ?? slider.step ?? step)}" ${(field.min ?? slider.min) !== undefined ? `min="${escapeHtml(field.min ?? slider.min)}"` : ""} ${(field.max ?? slider.max) !== undefined ? `max="${escapeHtml(field.max ?? slider.max)}"` : ""}` : "";
      const secretRules = field.secret ? `autocomplete="new-password" placeholder="${field._secret_configured ? "已安全保存；留空保持不变" : "请输入后保存"}"` : "";
      control = `<input class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="${escapeHtml(uiControl === "proxy-port" ? uiControl : type)}" type="${inputType}" ${numericRules} ${secretRules} value="${escapeHtml(value ?? "")}">${field.secret && field._secret_configured ? `<label class="secret-clear"><input class="config-secret-clear" data-path="${encodedPath}" type="checkbox" ${value === null ? "checked" : ""}>清除已保存的密钥</label>` : ""}`;
    }
    control = control.replace(/<(input|select|textarea) /, '<$1 id="' + fieldId + '" aria-describedby="' + helpId + '" ');
    if (field.secret) control += '<button type="button" class="secret-reveal button ghost" aria-controls="' + fieldId + '" aria-pressed="false">显示密钥</button>';
    if (field._group_selector) control += '<button type="button" class="group-field-button button secondary small" data-target="' + fieldId + '">' + icon("i-users") + '从群聊列表添加</button>';
    const dependsOn = field._ui_depends_on ? ` data-depends-on="${escapeHtml(JSON.stringify([...path.slice(0, -1), field._ui_depends_on]))}"` : "";
    const title = type === "bool"
      ? `<span class="field-label">${escapeHtml(label)}</span>`
      : `<label for="${fieldId}">${escapeHtml(label)}</label>`;
    return `<div class="form-field ${field._ui_tint ? "tinted-list" : ""} ${field._ui_disclosure ? "disclosure-controller" : ""} ${field._ui_full || ["text", "list", "template_list"].includes(type) || field.editor_mode ? "full" : ""}"${dependsOn}>${title}${control}<small id="${helpId}">${escapeHtml(hint)}</small><small class="field-error" role="alert" hidden></small></div>`;
  }

  function schemaMarkup(schema, config, prefix = [], namespace = "") {
    const markup = [];
    let groupTitle = "";
    let groupFields = [];
    let groupDisclosure = null;
    const flushGroup = () => {
      if (!groupFields.length) return;
      const disclosure = groupDisclosure;
      const heading = disclosure
        ? `<div class="optional-group-heading"><div><h3>${escapeHtml(groupTitle)}</h3><p>主模型不可用时才会尝试这一项。</p></div><button class="button secondary small" type="button" data-optional-group-toggle data-path="${escapeHtml(JSON.stringify(disclosure.path))}" aria-expanded="${disclosure.enabled}">${disclosure.enabled ? "不使用备用模型" : escapeHtml(disclosure.label)}</button></div>`
        : `<h3>${escapeHtml(groupTitle)}</h3>`;
      markup.push(`<section class="config-section grouped ${disclosure ? "optional-model-group" : ""}">${heading}<div class="config-section-grid" ${disclosure && !disclosure.enabled ? "hidden" : ""}>${groupFields.join("")}</div></section>`);
      groupTitle = "";
      groupFields = [];
      groupDisclosure = null;
    };
    Object.entries(schema || {}).forEach(([key, field]) => {
      if (!field || typeof field !== "object") return "";
      const path = [...prefix, key];
      if (field.type === "object" && field.items && typeof field.items === "object") {
        flushGroup();
        markup.push(`<section class="config-section" data-config-section="${escapeHtml(path.join("."))}"><h3>${escapeHtml(field.description || field.title || key.replaceAll("_", " "))}</h3><div class="config-section-grid">${schemaMarkup(field.items, config, path, namespace)}</div></section>`);
        return;
      }
      const fieldHtml = fieldMarkup(key, field, getAtPath(config, path), path, namespace);
      const nextGroup = String(field._ui_group || "").trim();
      if (!nextGroup) {
        flushGroup();
        markup.push(fieldHtml);
        return;
      }
      if (groupFields.length && groupTitle !== nextGroup) flushGroup();
      groupTitle = nextGroup;
      groupFields.push(fieldHtml);
      if (field._ui_disclosure) {
        groupDisclosure = {
          label: String(field._ui_disclosure),
          path,
          enabled: Boolean(getAtPath(config, path)),
        };
      }
    });
    flushGroup();
    return markup.join("");
  }

  function syncFeatureDependencies(form) {
    $$(".form-field[data-depends-on]", form).forEach((field) => {
      const sourcePath = field.dataset.dependsOn;
      const source = $$(".config-input[data-type='bool']", form).find((input) => input.dataset.path === sourcePath);
      const enabled = source ? source.checked : true;
      field.hidden = !enabled;
      $$("input, select, textarea", field).forEach((input) => { input.disabled = !enabled; });
    });
  }

  function renderFeatureDetail() {
    const detail = state.featureDetail;
    if (!detail) return;
    const schemas = {basic: detail.basic_schema || {}, models: detail.model_schema || {}, advanced: detail.advanced_schema || detail.schema || {}};
    const currentSchema = schemas[state.featureTab] || schemas.basic;
    const guide = detail.guide || {};
    const commandGroups = Array.isArray(guide.commands) ? guide.commands : [];
    const commandMarkup = commandGroups.length ? `<section class="feature-command-list" aria-labelledby="feature-command-title"><div class="feature-command-heading"><div><span class="eyebrow">快速参考</span><h3 id="feature-command-title">常用指令</h3></div><span>复制到聊天即可使用</span></div><div class="feature-command-groups">${commandGroups.map((group) => `<div class="feature-command-group"><strong>${escapeHtml(group.title || "指令")}</strong><div class="feature-command-items">${(Array.isArray(group.items) ? group.items : []).map((item) => `<div class="feature-command-item"><code>${escapeHtml(item[0] || "")}</code><span>${escapeHtml(item[1] || "")}</span></div>`).join("")}</div></div>`).join("")}</div></section>` : "";
    const scheduleAction = detail.scheduled_action || "";
    const scheduleCopy = scheduleAction === "short_drama"
      ? "短剧、影视与体育回放推荐可以安排发送到多个聊天。"
      : scheduleAction === "group_analysis"
        ? "选择自动日报群和每天生成时间，群聊分析会按群生成独立日报。"
        : "天气、资讯、油价和文字提醒可以安排发送到多个聊天。";
    const scheduleButton = scheduleAction === "group_analysis" ? "配置自动日报" : "创建定时任务";
    const defaultNote = !detail.engine_online ? "暂未连接到机器人：仅保存到本机；启动后加载。若机器人仍在运行，连接恢复后请重新保存。" : "保存时自动加载并核验，无需重启机器人。正在执行的工作可能被中断。";
    $("#featureDetail").innerHTML = `
      <div class="feature-detail-head">
        <div><span class="eyebrow">功能设置</span><h2>${escapeHtml(detail.display_name)}</h2><p>${escapeHtml(guide.summary || detail.description || "按需配置后保存。")}</p></div>
        <span class="status-badge online">统一加载</span>
      </div>
      ${scheduleAction ? `<div class="feature-schedule-entry"><p>${scheduleCopy}</p><button class="button secondary" id="featureScheduledTask" type="button" data-action-bound disabled>${scheduleButton}</button></div>` : ""}
      <div class="feature-help-entry"><span>完整用法、使用条件和示例已集中到帮助。</span><button class="button ghost small" id="openFeatureHelp" type="button">查看此功能帮助</button></div>
      ${commandMarkup}
      ${detail.configurable ? `
        <div class="config-tabs" aria-label="设置分类">${[["basic", "常用设置"], ["models", "模型设置"], ["advanced", "专业设置（全部）"]].filter(([key]) => Object.keys(schemas[key]).length).map(([key,label]) => `<button type="button" data-tab="${key}" aria-pressed="${state.featureTab === key}" class="${state.featureTab === key ? "active" : ""}">${label}</button>`).join("")}</div>
        ${state.featureTab === "advanced" ? '<p class="feature-prerequisite">这里包含完整参数。不了解的项目请保留原值；列表按字段说明填写。</p>' : ""}
        <form id="featureForm" class="feature-form" novalidate>${schemaMarkup(currentSchema, detail.config)}${!Object.keys(currentSchema).length ? '<div class="empty-state compact"><p>此分类无需配置</p></div>' : ""}</form>
        <div class="feature-savebar"><span class="form-note" id="featureNote" role="status">${escapeHtml(state.featureFeedback || (state.featureDirty ? "有修改尚未保存。" : defaultNote))}</span><div class="feature-save-actions">
        ${detail.pages?.length ? `<button class="button secondary" id="openFeatureCenter" type="button" ${!detail.engine_online ? 'disabled title="请先启动机器人"' : ""}>打开专属配置页</button>` : ""}
        <button class="button ghost" id="refreshFeature" type="button" data-action-bound disabled>重新读取</button>
        <button class="button primary" id="saveFeature" type="button" data-action-bound disabled>${detail.engine_online ? "保存并应用" : "保存设置"}</button></div></div>` :
        `<div class="empty-state compact"><p>${detail.display_name === "语音克隆" ? "请打开专属配置页填写服务密钥，管理授权音色、使用范围和语音参数；保存后查看生效反馈。" : detail.scheduled_action === "short_drama" ? "无需专属配置；需要定时发送时，从上方创建影视推荐任务。" : detail.pages?.length ? "请在专属配置页中管理；每次保存都以该页反馈为准。" : "无需填写参数；接入条件请按指引确认，使用范围在群聊中单独勾选。"}</p>${detail.pages?.length ? `<button class="button secondary" id="openFeatureCenter" type="button" ${!detail.engine_online ? 'disabled title="请先启动机器人"' : ""}>打开专属配置页</button>` : ""}</div>`}`;
    $("#featureScheduledTask")?.addEventListener("click", () => openScheduledFromFeature());
    $$(".config-tabs button").forEach((button) => button.addEventListener("click", () => {
      if (button.disabled || state.featureSaving) return;
      try {
        detail.config = configFromForm();
        state.featureTab = button.dataset.tab;
        renderFeatureDetail();
        $(`.config-tabs button[data-tab="${state.featureTab}"]`)?.focus();
      } catch (error) { showFeatureError(error); }
    }));
    const featureForm = $("#featureForm");
    featureForm?.addEventListener("input", () => {
      state.featureDirty = true; state.featureFeedback = "";
      $("#featureNote").textContent = "有修改尚未保存。";
      $$("textarea", featureForm).forEach(growTextarea);
    });
    if (featureForm) syncFeatureDependencies(featureForm);
    featureForm?.addEventListener("submit", (event) => { event.preventDefault(); saveFeature(); });
    $$(".config-input[data-type='bool']").forEach((input) => input.addEventListener("change", () => {
      const label = input.closest(".toggle")?.querySelector("span:last-child");
      if (label) label.textContent = input.checked ? "已启用" : "已停用";
      if (featureForm) syncFeatureDependencies(featureForm);
    }));
    if (featureForm) {
      $$('[data-optional-group-toggle]', featureForm).forEach((button) => button.addEventListener("click", () => {
        const section = button.closest(".optional-model-group");
        const controller = $$(".config-input[data-type='bool']", section).find((input) => input.dataset.path === button.dataset.path);
        if (!controller) return;
        controller.checked = !controller.checked;
        section.querySelector(".config-section-grid").hidden = !controller.checked;
        button.setAttribute("aria-expanded", String(controller.checked));
        button.textContent = controller.checked ? "不使用备用模型" : "添加备用模型";
        state.featureDirty = true;
        state.featureFeedback = "";
        $("#featureNote").textContent = "有修改尚未保存。";
        syncFeatureDependencies(featureForm);
      }));
    }
    $$(".secret-reveal").forEach((button) => button.addEventListener("click", () => {
      const input = document.getElementById(button.getAttribute("aria-controls"));
      const visible = input.type === "password";
      input.type = visible ? "text" : "password";
      button.textContent = visible ? "隐藏密钥" : "显示密钥";
      button.setAttribute("aria-pressed", String(visible));
    }));
    $$(".group-field-button").forEach((button) => button.addEventListener("click", () => addGroupToField(button.dataset.target)));
    $$("textarea", $("#featureForm") || $("#featureDetail")).forEach(growTextarea);
    $("#saveFeature")?.addEventListener("click", saveFeature);
    $("#refreshFeature")?.addEventListener("click", () => openFeature(state.selectedFeature, false, true));
    $("#openFeatureCenter")?.addEventListener("click", openFeatureCenter);
    $("#openFeatureHelp")?.addEventListener("click", () => openHelpForFeature(detail.display_name));
    $$("[data-action-bound]", $("#featureDetail")).forEach((button) => { button.disabled = false; });
  }

  function helpAudienceMatch(item) {
    const label = String(item.audience || "");
    if (state.helpAudience === "all") return true;
    if (state.helpAudience === "common") return label.includes("常用") || label.includes("基础");
    return !label.includes("常用") && !label.includes("基础");
  }

  function renderHelp() {
    const root = $("#helpFeatureGrid");
    if (!root) return;
    const keyword = String($("#helpSearch")?.value || "").trim().toLocaleLowerCase("zh-CN");
    const items = state.features.filter((item) => {
      const guide = item.guide || {};
      const haystack = [item.display_name, item.description, guide.summary, guide.recommendation, ...(guide.setup_steps || []), ...(guide.prerequisites || [])].join(" ").toLocaleLowerCase("zh-CN");
      return helpAudienceMatch(item) && (!keyword || haystack.includes(keyword));
    });
    $("#helpSummary").textContent = `${items.length} / ${state.features.length} 项功能`;
    root.innerHTML = items.length ? items.map((item) => {
      const guide = item.guide || {};
      const steps = guide.setup_steps?.length ? guide.setup_steps : ["在功能中心完成必要配置，再到群聊中勾选使用范围。", "在测试聊天中验证一次实际效果。"];
      const prerequisite = (guide.prerequisites || []).join(" ");
      return `<article class="help-feature-card panel" data-help-name="${escapeHtml(item.display_name)}"><div class="help-feature-head"><div><span class="help-audience-chip">${escapeHtml(item.audience || "按需使用")}</span><h3>${escapeHtml(item.display_name)}</h3></div><span class="feature-state enabled" title="已加载"></span></div><p>${escapeHtml(guide.summary || item.description || "按需使用此功能。")}</p><ol>${steps.map((step) => `<li>${escapeHtml(step)}</li>`).join("")}</ol>${prerequisite ? `<div class="help-prerequisite"><strong>使用前确认</strong><span>${escapeHtml(prerequisite)}</span></div>` : ""}<button class="row-action help-configure-feature" type="button" data-feature-id="${escapeHtml(item.id)}">前往功能设置</button></article>`;
    }).join("") : '<div class="empty-state compact panel"><h2>没有找到相关帮助</h2><p>换一个功能名或用法关键词试试。</p></div>';
    $$(".help-configure-feature", root).forEach((button) => button.addEventListener("click", async () => {
      await navigate("features");
      await openFeature(button.dataset.featureId);
    }));
  }

  async function openHelpForFeature(displayName) {
    const input = $("#helpSearch");
    if (input) input.value = String(displayName || "");
    await navigate("help");
    renderHelp();
    $("#helpFeatureGrid .help-feature-card")?.scrollIntoView({block: "start", behavior: "smooth"});
  }

  function growTextarea(input) {
    input.style.height = "auto";
    input.style.height = Math.min(480, Math.max(90, input.scrollHeight + 2)) + "px";
  }

  function showFeatureError(error) {
    reportError(error);
    const note = $("#featureNote");
    if (note) note.textContent = error.message;
    if (error.input) {
      error.input.setAttribute("aria-invalid", "true");
      const message = error.input.closest(".form-field").querySelector(".field-error");
      message.hidden = false; message.textContent = error.message;
      message.id = error.input.id + "-error";
      error.input.setAttribute("aria-describedby", error.input.id + "-help " + message.id);
      error.input.focus();
    }
  }

  function configFromForm(form = $("#featureForm"), source = state.featureDetail?.config || {}) {
    $$(".config-input[aria-invalid]", form).forEach((input) => input.removeAttribute("aria-invalid"));
    $$(".field-error", form).forEach((error) => { error.hidden = true; });
    const config = structuredClone(source);
    const listValues = new Map();
    $$(".config-list-option:not(:disabled)", form).forEach((input) => {
      const key = input.dataset.path;
      if (!listValues.has(key)) listValues.set(key, []);
      if (input.checked) listValues.get(key).push(input.value);
    });
    listValues.forEach((value, key) => setAtPath(config, JSON.parse(key), value));
    $$(".config-input:not(:disabled)", form).forEach((input) => {
      const path = JSON.parse(input.dataset.path);
      let value;
      try {
      if (input.dataset.type === "bool") value = input.checked;
      else if (input.dataset.type === "int") value = input.value.trim() === "" ? null : Number(input.value);
      else if (input.dataset.type === "float") value = input.value.trim() === "" ? null : Number(input.value);
      else if (["line-list", "time-list"].includes(input.dataset.type)) {
        value = input.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
        if (input.dataset.type === "time-list" && value.some((item) => !/^(?:[01]\d|2[0-3]):[0-5]\d$/.test(item))) {
          throw new Error(`${input.dataset.label || "生成时间"}请使用 24 小时制，例如 23:00；多个时间一行一个。`);
        }
      }
      else if (input.dataset.type === "json") {
        try { value = JSON.parse(input.value || "[]"); }
        catch (_error) { throw new Error(`${input.dataset.label || "列表配置"}的格式不正确，请检查括号和引号。`); }
        if (!Array.isArray(value)) throw new Error(`${input.dataset.label || "列表配置"}必须填写为列表。`);
      }
      else if (input.dataset.type === "json-string") {
        try { JSON.parse(input.value || "{}"); }
        catch (_error) { throw new Error(`${input.dataset.label || "映射配置"}必须填写有效的 JSON。`); }
        value = input.value;
      }
      else if (input.dataset.type === "proxy-port") {
        value = input.value.trim();
        if (input.validity.badInput || (value && (!/^[0-9]{1,5}$/.test(value) || Number(value) < 1 || Number(value) > 65535))) {
          throw new Error("本地代理端口请填写 1–65535 的整数，或留空使用已有代理设置。");
        }
      }
      else value = input.value;
      if (["int", "float"].includes(input.dataset.type) && value !== null && !Number.isFinite(value)) throw new Error(`${input.dataset.label || "数字配置"}必须填写有效数字。`);
      setAtPath(config, path, value);
      if (["int", "float"].includes(input.dataset.type) && value !== null) {
        if (input.dataset.type === "int" && !Number.isInteger(value)) throw new Error(`${input.dataset.label}请填写整数。`);
        if (input.min !== "" && value < Number(input.min)) throw new Error(`${input.dataset.label}不能小于 ${input.min}。`);
        if (input.max !== "" && value > Number(input.max)) throw new Error(`${input.dataset.label}不能大于 ${input.max}。`);
      }
      } catch (error) { error.input = input; throw error; }
    });
    $$(".config-secret-clear:checked", form).forEach((input) => setAtPath(config, JSON.parse(input.dataset.path), null));
    return config;
  }

  async function saveFeature() {
    if (state.featureSaving) return;
    const button = $("#saveFeature");
    try {
      const config = configFromForm();
      state.featureSaving = true;
      button.setAttribute("aria-busy", "true");
      $("#featureNote").textContent = "正在保存并确认生效，请稍候…";
      $$("#featureDetail input, #featureDetail select, #featureDetail textarea, #featureDetail button").forEach((input) => { input.dataset.wasDisabled = String(input.disabled); input.disabled = true; });
      const data = await api("save_feature", state.selectedFeature, config, state.featureDetail.revision || "");
      state.featureFeedback = data.message || "保存操作已结束，请确认生效状态。";
      state.featureDirty = false;
      const savedDetail = structuredClone(state.featureDetail);
      savedDetail.config = structuredClone(config);
      savedDetail.advanced_config = structuredClone(config);
      state.featureDetails[state.selectedFeature] = savedDetail;
      toast(state.featureFeedback);
      await openFeature(state.selectedFeature, true, true);
    } catch (error) {
      showFeatureError(error);
    } finally {
      state.featureSaving = false;
      if (button.isConnected) {
        $$("#featureDetail [data-was-disabled]").forEach((input) => { input.disabled = input.dataset.wasDisabled === "true"; delete input.dataset.wasDisabled; });
        button.removeAttribute("aria-busy");
      }
    }
  }

  async function reloadFeature() {
    const button = $("#reloadFeature");
    button.disabled = true;
    try {
      const data = await api("reload_feature", state.selectedFeature);
      toast(data.message || "功能已经重新载入。");
    } catch (error) { reportError(error); }
    finally { button.disabled = false; }
  }

  async function openFeatureCenter() {
    if (state.featureSaving) return;
    if (state.featureDirty) { toast("请先保存当前修改，再打开专属配置页。", "error"); return; }
    const button = $("#openFeatureCenter");
    button.disabled = true;
    try {
      const data = await api("open_feature_center", state.selectedFeature || "");
      toast(data.message || "高级配置窗口正在打开。");
    } catch (error) { reportError(error); }
    finally { button.disabled = false; }
  }

  async function refreshVisibleFeatureState() {
    if (
      document.hidden ||
      state.activePage !== "features" ||
      !state.selectedFeature ||
      state.featureDirty ||
      state.featureSaving
    ) return;
    await openFeature(state.selectedFeature, false, true);
  }

  async function copyText(value, label = "群标识") {
    const text = String(value || "");
    if (!text) return;
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
      else throw new Error("clipboard unavailable");
    } catch (_error) {
      const input = document.createElement("textarea");
      input.value = text;
      input.setAttribute("readonly", "");
      input.style.position = "fixed";
      input.style.opacity = "0";
      document.body.append(input);
      input.select();
      const copied = document.execCommand("copy");
      input.remove();
      if (!copied) { toast("复制失败，请选中群标识后手动复制。", "error"); return; }
    }
    toast(`${label}已复制。`);
  }

  async function addGroupToField(targetId) {
    const target = document.getElementById(targetId);
    if (!target || state.featureSaving) return;
    if (!state.groupsLoaded) await loadGroups(false);
    if (!state.groups.length) {
      toast("暂时没有可选群聊，请确认微信已连接后到“群聊管理”刷新。", "error");
      return;
    }
    confirmAction({
      title: "选择群聊",
      message: "只会把群标识加入当前草稿；点击“保存并应用”后才会生效，不会向群里发送消息。",
      confirmText: "添加到列表",
      field: {
        label: "群聊",
        type: "select",
        value: state.groups[0].group_id,
        options: state.groups.map((item) => ({value: item.group_id, label: `${item.name || "未命名群聊"} · ${item.group_id}`})),
      },
      onConfirm: async (groupId) => {
        const lines = target.value.split(/\r?\n/).map((item) => item.trim()).filter(Boolean);
        if (!lines.includes(groupId)) lines.push(groupId);
        target.value = lines.join("\n");
        target.dispatchEvent(new Event("input", {bubbles: true}));
        target.focus();
      },
    });
  }

  async function selectChatManagementTab(tab, focus = false) {
    if (!new Set(["groups", "private"]).has(tab)) return;
    state.chatManagementTab = tab;
    $$('[data-chat-management-tab]').forEach((button) => {
      const active = button.dataset.chatManagementTab === tab;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
      button.tabIndex = active ? 0 : -1;
      if (active && focus) button.focus();
    });
    $("#groupManagementPanel").hidden = tab !== "groups";
    $("#privateManagementPanel").hidden = tab !== "private";
    $("#refreshGroups").hidden = tab !== "groups";
    $("#refreshContacts").hidden = tab !== "private";
    $("#chatManagementDescription").textContent = tab === "groups"
      ? "管理机器人所在群聊的回复规则、可用功能和成员信息。"
      : "读取当前微信通讯录，查看个人资料并配置每位联系人的可用功能。";
    if (tab === "groups") await loadGroups(true, {background: state.groupsLoaded});
    if (tab === "private" && !state.contactsLoaded) await loadContacts(false);
  }

  function groupDirectoryStatus(data, failed = false) {
    const panel = $("#groupDirectoryStatus");
    const stale = Boolean(data?.stale || failed);
    panel.className = `directory-status ${stale ? "warning" : "success"}`;
    const title = stale
      ? (state.groups.length ? "本次未更新，正在显示上次结果" : "群聊暂时无法读取")
      : "群聊列表已更新";
    const message = data?.message || (failed ? "请确认机器人已连接微信，再重新刷新。" : "查看和刷新不会向任何群发送消息。");
    panel.innerHTML = `<div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>`;
  }

  function clearGroupRefreshRetry() {
    window.clearTimeout(state.groupRefreshRetryTimer);
    state.groupRefreshRetryTimer = 0;
  }

  function scheduleGroupRefreshRetry(retryAttempt) {
    if (retryAttempt >= GROUP_DIRECTORY_RETRY_DELAYS_MS.length
        || state.activePage !== "groups" || state.chatManagementTab !== "groups") return;
    clearGroupRefreshRetry();
    state.groupRefreshRetryTimer = window.setTimeout(() => {
      state.groupRefreshRetryTimer = 0;
      if (state.activePage !== "groups" || state.chatManagementTab !== "groups") return;
      void loadGroups(true, {background: true, retryAttempt: retryAttempt + 1});
    }, GROUP_DIRECTORY_RETRY_DELAYS_MS[retryAttempt]);
  }

  async function loadGroups(refresh = false, options = {}) {
    if (state.groupsBusy) return;
    const background = options.background === true;
    const retryAttempt = Number.isInteger(options.retryAttempt) ? options.retryAttempt : 0;
    if (refresh && retryAttempt === 0) clearGroupRefreshRetry();
    state.groupsBusy = true;
    const button = $("#refreshGroups");
    if (!background) {
      button.disabled = true;
      button.setAttribute("aria-busy", "true");
      button.innerHTML = `${icon("i-refresh")}<span>${refresh ? "正在刷新…" : "正在读取…"}</span>`;
    }
    if (!state.groups.length) $("#groupRows").innerHTML = '<tr><td colspan="4" class="table-empty">正在读取当前微信的群聊…</td></tr>';
    try {
      const data = await api("get_groups", refresh);
      const previousAccount = state.groupAccount;
      const previousGroups = JSON.stringify(state.groups);
      const previousStale = state.groupStale;
      const nextGroups = Array.isArray(data.items) ? data.items : [];
      state.groups = nextGroups;
      state.groupAccount = data.account_id || "";
      if (previousAccount && previousAccount !== state.groupAccount) {
        state.selectedGroup = null;
        state.groupMembers = {};
        state.groupPolicies = {};
      }
      if (background) state.groups.forEach((item) => {
        const cachedCount = state.groupMembers[item.group_id]?.total;
        if (!Number.isInteger(item.member_count) && Number.isInteger(cachedCount)) item.member_count = cachedCount;
      });
      state.groupUpdatedAt = data.updated_at || "";
      state.groupStale = Boolean(data.stale);
      if (state.groupStale) scheduleGroupRefreshRetry(retryAttempt);
      else clearGroupRefreshRetry();
      state.groupsLoaded = true;
      const currentGroupIds = new Set(state.groups.map((item) => item.group_id));
      state.groupMembers = Object.fromEntries(
        Object.entries(state.groupMembers).filter(([groupId]) => currentGroupIds.has(groupId)),
      );
      if (state.selectedGroup && !state.groups.some((item) => item.group_id === state.selectedGroup)) {
        state.selectedGroup = null;
      }
      const memberLoad = background && previousGroups === JSON.stringify(state.groups)
        && previousAccount === state.groupAccount
        ? state.groupMemberLoad : ++state.groupMemberLoad;
      const groupsNeedingCounts = state.groups.filter((item) =>
        !Number.isInteger(item.member_count)
        && (!background || !state.groupMembers[item.group_id]));
      groupsNeedingCounts.forEach((item) => {
        const previous = state.groupMembers[item.group_id] || {};
        state.groupMembers[item.group_id] = {
          ...previous,
          loading: true,
          error: "",
          items: Array.isArray(previous.items) ? previous.items : [],
        };
      });
      groupDirectoryStatus(data);
      if (!background || refresh || previousGroups !== JSON.stringify(state.groups)
          || previousAccount !== state.groupAccount || previousStale !== state.groupStale) {
        renderGroups({preserveInspector: background});
      } else {
        $("#groupUpdated").textContent = state.groupUpdatedAt
          ? `${state.groupStale ? "上次成功" : "更新于"} ${fullTime(state.groupUpdatedAt)}` : "尚无可用结果";
      }
      void hydrateGroupMemberCounts(groupsNeedingCounts.map((item) => item.group_id), memberLoad);
    } catch (error) {
      state.groupStale = true;
      scheduleGroupRefreshRetry(retryAttempt);
      groupDirectoryStatus({message: error?.message}, true);
      renderGroups({preserveInspector: background});
      if (!background) reportError(error);
    } finally {
      state.groupsBusy = false;
      if (!background) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.innerHTML = `${icon("i-refresh")}<span>刷新群列表</span>`;
      }
    }
  }

  function renderGroups(options = {}) {
    const scroll = $(".group-table-scroll");
    const previousScroll = scroll?.scrollTop || 0;
    const keyword = $("#groupSearch").value.trim().toLocaleLowerCase("zh-CN");
    const items = state.groups.filter((item) => {
      const haystack = `${item.name || ""}\n${item.group_id || ""}`.toLocaleLowerCase("zh-CN");
      return !keyword || haystack.includes(keyword);
    });
    const countUnavailable = state.groupStale && !state.groupUpdatedAt && !state.groups.length;
    $("#groupCount").textContent = countUnavailable
      ? "群聊数量暂不可用"
      : keyword ? `${items.length} / ${state.groups.length} 个群聊` : `${state.groups.length} 个群聊`;
    $("#groupUpdated").textContent = state.groupUpdatedAt ? `${state.groupStale ? "上次成功" : "更新于"} ${fullTime(state.groupUpdatedAt)}` : "尚无可用结果";
    const empty = state.groups.length
      ? "没有符合条件的群聊"
      : (state.groupStale ? "尚未读取到群聊，请连接微信后重试" : "当前账号没有可显示的群聊");
    $("#groupRows").innerHTML = items.length ? items.map((item) => {
      const selected = item.group_id === state.selectedGroup;
      const memberData = state.groupMembers[item.group_id];
      const memberCount = memberData?.loading
        ? '<span class="member-count-state loading">读取中</span>'
        : Number.isInteger(item.member_count)
          ? escapeHtml(item.member_count)
          : memberData?.error
            ? '<span class="member-count-state error">读取失败</span>'
            : '<span class="member-count-state loading">读取中</span>';
      const avatarTone = Math.abs([...String(item.group_id || "")].reduce((sum, char) => sum + char.charCodeAt(0), 0)) % 3;
      return `<tr class="group-row ${selected ? "selected" : ""}" data-group-id="${escapeHtml(item.group_id)}" tabindex="0" aria-selected="${selected}">
        <td><span class="group-name-cell">${avatarMarkup(`group-avatar tone-${avatarTone}`, item.name || "群", item.avatar)}<span><strong class="group-name">${escapeHtml(item.name || "未命名群聊")}</strong><small>${selected ? "机器人已启用" : "未单独设置"}</small></span></span></td>
        <td><code class="group-id">${escapeHtml(item.group_id)}</code></td>
        <td>${memberCount}</td>
        <td><div class="row-actions"><button class="row-action copy-group" type="button" data-group-id="${escapeHtml(item.group_id)}">复制</button><button class="row-action inspect-group ${selected ? "active" : ""}" type="button" data-group-id="${escapeHtml(item.group_id)}">${selected ? "正在查看" : "查看"}</button></div></td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="table-empty">${escapeHtml(empty)}</td></tr>`;
    $$(".group-row").forEach((row) => {
      const select = () => selectGroup(row.dataset.groupId);
      row.addEventListener("click", (event) => { if (!event.target.closest("button")) select(); });
      row.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
    });
    $$(".inspect-group").forEach((button) => button.addEventListener("click", () => selectGroup(button.dataset.groupId)));
    $$(".copy-group").forEach((button) => button.addEventListener("click", () => copyText(button.dataset.groupId)));
    bindAvatarFallbacks($("#groupRows"));
    if (!options.preserveInspector || !state.selectedGroup) renderGroupInspector();
    if (scroll) scroll.scrollTop = previousScroll;
  }

  function selectGroup(groupId) {
    state.selectedGroup = groupId;
    state.groupInspectorView = "policy";
    renderGroups();
    $("#groupInspector").scrollIntoView({block: "nearest", behavior: "smooth"});
    loadGroupPolicy(groupId);
  }

  async function loadGroupPolicy(groupId) {
    if (!state.groupAccount || !groupId) return;
    state.groupPolicies[groupId] = {loading: true};
    renderGroupInspector();
    try {
      state.groupPolicies[groupId] = await api("get_group_policy", state.groupAccount, groupId);
    } catch (error) {
      state.groupPolicies[groupId] = {error: error?.message || "群聊规则暂时无法读取。"};
      reportError(error);
    }
    if (state.selectedGroup === groupId) renderGroupInspector();
  }

  function renderGroupInspector() {
    const panel = $("#groupInspector");
    const group = state.groups.find((item) => item.group_id === state.selectedGroup);
    panel.classList.toggle("empty", !group);
    if (!group) {
      const message = state.groups.length ? "选择左侧群聊后，可查看群规则和群成员。" : "刷新并读取到群聊后，可在这里查看群规则和群成员。";
      panel.innerHTML = `<div class="group-inspector-empty"><div class="empty-symbol">群</div><span class="eyebrow">群聊详情</span><h2>${state.groups.length ? "选择一个群聊" : "暂时没有群聊"}</h2><p>${message}</p></div>`;
      return;
    }
    const memberData = state.groupMembers[group.group_id];
    const policyData = state.groupPolicies[group.group_id];
    const memberCount = memberData?.loading ? "读取中"
      : Number.isInteger(group.member_count) ? `${group.member_count} 人`
      : memberData?.error ? "读取失败" : "待读取";
    const memberMarkup = !memberData ? '<div class="member-empty">点击“重新读取”后显示当前微信缓存中的成员。</div>'
      : memberData.loading ? '<div class="member-empty">正在读取成员…</div>'
      : memberData.error ? `<div class="member-empty error">${escapeHtml(memberData.error)}</div>`
      : memberData.items.length ? `<div class="member-list">${memberData.items.map((item) => `<article>${avatarMarkup("member-avatar", item.group_nickname || item.name || "群", item.avatar)}<div><strong>${escapeHtml(item.group_nickname || item.name || "未命名成员")}</strong><div class="member-identities"><small><span>微信号</span>${escapeHtml(item.wechat_id || "未提供")}</small><small><span>微信标识</span>${escapeHtml(item.user_id || "未提供")}</small></div></div></article>`).join("")}</div>${memberData.truncated ? '<p class="member-note">成员较多，本次仅显示前 500 人。</p>' : ""}`
      : '<div class="member-empty">当前接口没有返回成员。</div>';
    const header = `<header class="inspector-hero"><span class="eyebrow">当前群聊</span><div class="inspector-group-title">${avatarMarkup("group-avatar tone-0", group.name || "群", group.avatar)}<div><h2>${escapeHtml(group.name || "未命名群聊")}</h2><code class="inspector-id">${escapeHtml(group.group_id)} · ${escapeHtml(memberCount)}</code></div></div><button class="row-action chat-scheduled-entry" type="button">本群的定时任务</button></header>`;
    const tabs = `<div class="inspector-tabs" aria-label="群聊详情分类"><button type="button" data-inspector-view="policy" aria-pressed="${state.groupInspectorView === "policy"}" class="${state.groupInspectorView === "policy" ? "active" : ""}">群聊规则</button><button type="button" data-inspector-view="members" aria-pressed="${state.groupInspectorView === "members"}" class="${state.groupInspectorView === "members" ? "active" : ""}">群成员</button></div>`;
    const policyBody = `<div class="inspector-scroll">${groupPolicyMarkup(policyData)}</div><footer class="inspector-footer"><span>在线保存会立即应用到当前群。</span><button class="button primary" type="submit" form="groupPolicyForm" ${policyData?.loading || policyData?.error ? "disabled" : ""}>保存群聊规则</button></footer>`;
    const membersBody = `<div class="inspector-scroll members-view"><section class="members-section"><div class="members-heading"><div><strong>群成员</strong><span>按需读取，最多展示 500 人</span></div><button class="button ghost small" id="loadGroupMembers" type="button" ${memberData?.loading ? "disabled" : ""}>${memberData?.loading ? "正在读取…" : "重新读取"}</button></div>${memberMarkup}<div class="policy-summary">群主、管理员和头像等微信接口未提供的信息保持“未知”，不会推断。</div></section></div><footer class="inspector-footer"><span>成员列表不会写入配置。</span><button class="button ghost" type="button" data-inspector-view="policy">返回群规则</button></footer>`;
    panel.innerHTML = `${header}${tabs}${state.groupInspectorView === "members" ? membersBody : policyBody}`;
    $(".chat-scheduled-entry", panel)?.addEventListener("click", () => openScheduledForChat({type: "group", id: group.group_id, name: group.name}, state.groupAccount));
    $$('[data-inspector-view]', panel).forEach((button) => button.addEventListener("click", () => {
      state.groupInspectorView = button.dataset.inspectorView;
      renderGroupInspector();
      if (state.groupInspectorView === "members" && !state.groupMembers[group.group_id]) loadGroupMembers(group.group_id);
    }));
    $("#loadGroupMembers")?.addEventListener("click", () => loadGroupMembers(group.group_id));
    bindAvatarFallbacks(panel);
    const policyForm = $("#groupPolicyForm");
    if (policyForm) {
      policyForm.addEventListener("submit", saveGroupPolicy);
      $$(".toggle input", policyForm).forEach((input) => input.addEventListener("change", updateToggleCopy));
    }
  }

  function groupPolicyMarkup(data) {
    if (!data) return '<section class="group-policy-form"><h3>本群机器人规则</h3><button class="button secondary small" id="loadGroupPolicy" type="button">读取群聊规则</button></section>';
    if (data.loading) return '<section class="group-policy-form"><h3>本群机器人规则</h3><div class="loading-row">正在读取规则…</div></section>';
    if (data.error) return `<section class="group-policy-form"><h3>本群机器人规则</h3><div class="member-empty error">${escapeHtml(data.error)}</div></section>`;
    const policy = data.policy || {};
    const quiet = policy.quiet_hours || {};
    const allowed = new Set(policy.allowed_features || []);
    const featureChoices = (data.features || []).map((feature) => `<label class="option-check"><input name="allowed_features" type="checkbox" value="${escapeHtml(feature.id)}" ${allowed.has(feature.id) ? "checked" : ""}><span>${escapeHtml(feature.name)}</span></label>`).join("");
    return `<form class="group-policy-form" id="groupPolicyForm" data-revision="${escapeHtml(policy.revision || "")}" novalidate>
      <h3>本群机器人规则</h3>
      <div class="toggle-line"><span><strong>在本群使用机器人</strong><small>关闭后本群不应触发回复或主动任务</small></span><span class="toggle"><input id="groupBotEnabled" name="bot_enabled" type="checkbox" ${policy.bot_enabled ? "checked" : ""}><label class="toggle-track" for="groupBotEnabled" aria-label="切换本群机器人"></label><span>${policy.bot_enabled ? "已启用" : "已停用"}</span></span></div>
      <label><span>回复方式</span><select name="reply_mode"><option value="mention" ${policy.reply_mode === "mention" ? "selected" : ""}>仅在点名或引用时回复（推荐）</option><option value="natural" ${policy.reply_mode === "natural" ? "selected" : ""}>相关话题可自然参与</option><option value="off" ${policy.reply_mode === "off" ? "selected" : ""}>不回复</option></select><small>点名或引用会自动快速回应；此设置只影响当前群。</small></label>
      <details class="advanced-details"><summary>限制本群可以使用的功能</summary><div class="option-grid compact-options group-feature-options">${featureChoices || "<span class='subtle'>当前没有可分配的功能</span>"}</div></details>
      <div class="toggle-line"><span><strong>接收已配置的定时任务</strong><small>关闭后暂停本群全部定时推送，保留任务设置</small></span><span class="toggle"><input id="groupScheduledEnabled" name="scheduled_enabled" type="checkbox" ${policy.scheduled_enabled !== false ? "checked" : ""}><label class="toggle-track" for="groupScheduledEnabled" aria-label="切换本群定时任务"></label><span>${policy.scheduled_enabled !== false ? "已允许" : "已暂停"}</span></span></div>
      <div class="toggle-line"><span><strong>定时任务安静时段</strong><small>仅限制本群已配置的定时任务</small></span><span class="toggle"><input id="groupQuietEnabled" name="quiet_enabled" type="checkbox" ${quiet.enabled ? "checked" : ""}><label class="toggle-track" for="groupQuietEnabled" aria-label="切换本群安静时段"></label><span>${quiet.enabled ? "已启用" : "已关闭"}</span></span></div>
      <div class="field-pair"><label><span>开始</span><input name="quiet_start" type="time" value="${escapeHtml(quiet.start || "22:00")}"></label><label><span>结束</span><input name="quiet_end" type="time" value="${escapeHtml(quiet.end || "08:00")}"></label></div>
      <label><span>每日定时任务上限</span><input name="daily_limit" type="number" min="0" max="100" value="${escapeHtml(policy.daily_limit ?? 3)}"><small>只限制已配置的定时任务；填 0 表示暂停定时推送。</small></label>
      <div class="policy-summary">普通消息与卡片不受定时任务限额影响；主动聊天由各功能自行配置。当前接口未提供可靠群主权限时，只允许修改本机机器人规则，不会冒充群主管理成员。</div>
    </form>`;
  }

  async function saveGroupPolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $('button[form="groupPolicyForm"]');
    if (!button) return;
    button.disabled = true;
    button.textContent = "正在保存…";
    try {
      const values = {
        bot_enabled: form.elements.bot_enabled.checked,
        reply_mode: form.elements.reply_mode.value,
        scheduled_enabled: form.elements.scheduled_enabled.checked,
        allowed_features: $$('input[name="allowed_features"]:checked', form).map((input) => input.value),
        quiet_hours: {
          enabled: form.elements.quiet_enabled.checked,
          start: form.elements.quiet_start.value,
          end: form.elements.quiet_end.value,
          timezone: "Asia/Shanghai",
        },
        daily_limit: Number(form.elements.daily_limit.value),
      };
      await api("save_group_policy", state.groupAccount, state.selectedGroup, values, form.dataset.revision || "");
      await loadGroupPolicy(state.selectedGroup);
    } catch (error) { reportError(error); }
    finally { button.disabled = false; button.textContent = "保存本群规则"; }
  }

  async function hydrateGroupMemberCounts(groupIds, generation) {
    const pending = [...new Set(groupIds)];
    for (const groupId of pending) {
      if (generation !== state.groupMemberLoad) return;
      await loadGroupMembers(groupId, {background: true, generation});
    }
  }

  function queueGroupMemberRequest(groupId, generation, background) {
    const request = groupMemberRequestQueue.then(() => {
      if (generation !== state.groupMemberLoad
          || !state.groups.some((item) => item.group_id === groupId)
          || (background && (state.activePage !== "groups" || state.chatManagementTab !== "groups"))) return null;
      return api("get_group_members", groupId);
    });
    groupMemberRequestQueue = request.catch(() => undefined);
    return request;
  }

  async function loadGroupMembers(groupId, options = {}) {
    const background = options?.background === true;
    const generation = options?.generation ?? state.groupMemberLoad;
    if (generation !== state.groupMemberLoad || !state.groups.some((item) => item.group_id === groupId)) return;
    const requestKey = `${generation}:${groupId}`;
    if (groupMemberLoadsInFlight.has(requestKey)) return;
    groupMemberLoadsInFlight.add(requestKey);
    try {
      const previous = state.groupMembers[groupId] || {};
      state.groupMembers[groupId] = {
        ...previous,
        loading: true,
        error: "",
        items: Array.isArray(previous.items) ? previous.items : [],
      };
      if (!background) renderGroups();
      let lastError = null;
      for (let attempt = 0; attempt <= GROUP_MEMBER_RETRY_DELAYS_MS.length; attempt += 1) {
        if (attempt) {
          await new Promise((resolve) => window.setTimeout(resolve, GROUP_MEMBER_RETRY_DELAYS_MS[attempt - 1]));
        }
        if (generation !== state.groupMemberLoad
            || !state.groups.some((item) => item.group_id === groupId)
            || (background && (state.activePage !== "groups" || state.chatManagementTab !== "groups"))) return;
        try {
          const data = await queueGroupMemberRequest(groupId, generation, background);
          if (!data) return;
          if (generation !== state.groupMemberLoad || !state.groups.some((item) => item.group_id === groupId)) return;
          const items = Array.isArray(data.items) ? data.items : [];
          const total = Number.isInteger(data.total) ? data.total : items.length;
          state.groupMembers[groupId] = {
            loading: false,
            items,
            total,
            truncated: Boolean(data.truncated),
          };
          const group = state.groups.find((item) => item.group_id === groupId);
          if (group) group.member_count = total;
          lastError = null;
          break;
        } catch (error) {
          lastError = error;
        }
      }
      if (lastError) {
        if (generation !== state.groupMemberLoad || !state.groups.some((item) => item.group_id === groupId)) return;
        state.groupMembers[groupId] = {loading: false, items: [], error: lastError?.message || "成员暂时无法读取，请稍后重试。"};
        if (!background) reportError(lastError);
      }
      renderGroups({preserveInspector: background});
    } finally {
      groupMemberLoadsInFlight.delete(requestKey);
    }
  }

  function contactDisplayName(contact) {
    return contact?.remark || contact?.nickname || contact?.name || contact?.alias || contact?.user_id || "未命名联系人";
  }

  function contactRegion(contact) {
    return [contact?.country, contact?.province, contact?.city].filter(Boolean).join(" · ") || "未提供";
  }

  function contactDirectoryStatus(data, failed = false) {
    const panel = $("#contactDirectoryStatus");
    const stale = Boolean(data?.stale || failed);
    panel.className = `directory-status ${stale ? "warning" : "success"}`;
    const title = stale
      ? (state.contacts.length ? "本次未更新，正在显示上次结果" : "联系人暂时无法读取")
      : "联系人列表已更新";
    const message = data?.message || (failed
      ? "请确认机器人已连接微信，再重新刷新。"
      : "查看和刷新不会向任何联系人发送消息。");
    panel.innerHTML = `<div><strong>${escapeHtml(title)}</strong><span>${escapeHtml(message)}</span></div>`;
  }

  async function loadContacts(refresh = false) {
    if (state.contactsBusy) return;
    state.contactsBusy = true;
    const generation = ++state.contactLoad;
    const button = $("#refreshContacts");
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.innerHTML = `${icon("i-refresh")}<span>${refresh ? "正在刷新…" : "正在读取…"}</span>`;
    if (!state.contacts.length) $("#contactRows").innerHTML = '<tr><td colspan="4" class="table-empty">正在读取当前微信通讯录…</td></tr>';
    try {
      const data = await api("get_contacts", refresh);
      if (generation !== state.contactLoad) return;
      const previousAccount = state.contactAccount;
      state.contacts = Array.isArray(data.items) ? data.items : [];
      state.contactAccount = data.account_id || "";
      if (previousAccount && previousAccount !== state.contactAccount) {
        state.selectedContact = null;
        state.contactDetails = {};
        state.privatePolicies = {};
      }
      state.contactUpdatedAt = data.updated_at || "";
      state.contactStale = Boolean(data.stale);
      state.contactTruncated = Boolean(data.truncated);
      state.contactsLoaded = true;
      state.contactPage = 1;
      if (state.selectedContact && !state.contacts.some((item) => item.user_id === state.selectedContact)) {
        state.selectedContact = null;
      }
      contactDirectoryStatus(data);
      renderContacts();
    } catch (error) {
      if (generation !== state.contactLoad) return;
      state.contactStale = true;
      contactDirectoryStatus({message: error?.message}, true);
      renderContacts();
      reportError(error);
    } finally {
      if (generation === state.contactLoad) {
        state.contactsBusy = false;
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.innerHTML = `${icon("i-refresh")}<span>刷新联系人</span>`;
      }
    }
  }

  function filteredContacts() {
    const keyword = $("#contactSearch").value.trim().toLocaleLowerCase("zh-CN");
    return state.contacts.filter((item) => {
      const haystack = [
        item.name, item.nickname, item.remark, item.alias, item.user_id,
        item.country, item.province, item.city, item.contact_type,
      ].filter(Boolean).join("\n").toLocaleLowerCase("zh-CN");
      return !keyword || haystack.includes(keyword);
    });
  }

  function renderContacts() {
    const scroll = $(".contact-table-scroll");
    const previousScroll = scroll?.scrollTop || 0;
    const items = filteredContacts();
    const pageCount = Math.max(1, Math.ceil(items.length / CONTACT_PAGE_SIZE));
    state.contactPage = Math.min(Math.max(1, state.contactPage), pageCount);
    const start = (state.contactPage - 1) * CONTACT_PAGE_SIZE;
    const visible = items.slice(start, start + CONTACT_PAGE_SIZE);
    const keyword = Boolean($("#contactSearch").value.trim());
    const countUnavailable = state.contactStale && !state.contactUpdatedAt && !state.contacts.length;
    $("#contactCount").textContent = countUnavailable
      ? "联系人数量暂不可用"
      : keyword ? `${items.length} / ${state.contacts.length} 位联系人` : `${state.contacts.length} 位联系人`;
    $("#contactUpdated").textContent = state.contactUpdatedAt
      ? `${state.contactStale ? "上次成功" : "更新于"} ${fullTime(state.contactUpdatedAt)}`
      : "尚无可用结果";
    const empty = state.contacts.length
      ? "没有符合条件的联系人"
      : (state.contactStale ? "尚未读取到联系人，请连接微信后重试" : "当前账号没有可显示的联系人");
    $("#contactRows").innerHTML = visible.length ? visible.map((item) => {
      const selected = item.user_id === state.selectedContact;
      const displayName = contactDisplayName(item);
      const account = item.alias || "未设置微信号";
      return `<tr class="contact-row ${selected ? "selected" : ""}" data-contact-id="${escapeHtml(item.user_id)}" tabindex="0" aria-selected="${selected}">
        <td><span class="group-name-cell">${avatarMarkup("contact-avatar", displayName, item.avatar)}<span><strong class="group-name">${escapeHtml(displayName)}</strong><small>${escapeHtml(item.contact_type || item.nickname || "微信联系人")}</small></span></span></td>
        <td><span class="contact-account">${escapeHtml(account)}</span><code class="contact-id">${escapeHtml(item.user_id)}</code></td>
        <td>${escapeHtml(contactRegion(item))}</td>
        <td><button class="row-action inspect-contact ${selected ? "active" : ""}" type="button" data-contact-id="${escapeHtml(item.user_id)}">${selected ? "正在查看" : "查看"}</button></td>
      </tr>`;
    }).join("") : `<tr><td colspan="4" class="table-empty">${escapeHtml(empty)}</td></tr>`;
    const first = items.length ? start + 1 : 0;
    const last = Math.min(start + visible.length, items.length);
    $("#contactRange").textContent = `第 ${first}–${last} 项，共 ${items.length} 项${state.contactTruncated ? " · 接口结果已截断" : ""}`;
    $("#contactPrevious").disabled = state.contactPage <= 1;
    $("#contactNext").disabled = state.contactPage >= pageCount;
    $$(".contact-row").forEach((row) => {
      const select = () => selectContact(row.dataset.contactId);
      row.addEventListener("click", (event) => { if (!event.target.closest("button")) select(); });
      row.addEventListener("keydown", (event) => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
    });
    $$(".inspect-contact").forEach((button) => button.addEventListener("click", () => selectContact(button.dataset.contactId)));
    bindAvatarFallbacks($("#contactRows"));
    renderContactInspector();
    if (scroll) scroll.scrollTop = previousScroll;
  }

  async function selectContact(contactId) {
    state.selectedContact = contactId;
    renderContacts();
    $("#contactInspector").scrollIntoView({block: "nearest", behavior: "smooth"});
    await Promise.all([loadContactDetail(contactId), loadPrivatePolicy(contactId)]);
  }

  async function loadContactDetail(contactId) {
    state.contactDetails[contactId] = {loading: true};
    renderContactInspector();
    try {
      const data = await api("get_contact_detail", contactId);
      if (state.selectedContact !== contactId) return;
      const detail = data?.item || {};
      state.contactDetails[contactId] = detail;
      const index = state.contacts.findIndex((item) => item.user_id === contactId);
      if (index >= 0) state.contacts[index] = {...state.contacts[index], ...detail};
    } catch (error) {
      if (state.selectedContact !== contactId) return;
      state.contactDetails[contactId] = {error: error?.message || "联系人详情暂时无法读取。"};
      reportError(error);
    }
    if (state.selectedContact === contactId) renderContactInspector();
  }

  async function loadPrivatePolicy(contactId) {
    if (!state.contactAccount || !contactId) return;
    state.privatePolicies[contactId] = {loading: true};
    renderContactInspector();
    try {
      state.privatePolicies[contactId] = await api("get_private_policy", state.contactAccount, contactId);
    } catch (error) {
      state.privatePolicies[contactId] = {error: error?.message || "联系人可用功能暂时无法读取。"};
      reportError(error);
    }
    if (state.selectedContact === contactId) renderContactInspector();
  }

  function contactFact(label, value, options = {}) {
    const text = String(value || "").trim();
    if (!text) return "";
    const rendered = options.code ? `<code>${escapeHtml(text)}</code>` : escapeHtml(text);
    return `<div><dt>${escapeHtml(label)}</dt><dd>${rendered}</dd></div>`;
  }

  function privatePolicyMarkup(data) {
    if (!data || data.loading) return '<section class="private-feature-policy"><h3>该联系人聊天规则</h3><div class="loading-row">正在读取聊天规则…</div></section>';
    if (data.error) return `<section class="private-feature-policy"><h3>该联系人聊天规则</h3><div class="member-empty error">${escapeHtml(data.error)}</div></section>`;
    const policy = data.policy || {};
    const quiet = policy.quiet_hours || {};
    const choices = (data.features || []).map((feature) => `<label class="option-check"><input name="allowed_features" type="checkbox" value="${escapeHtml(feature.id)}" ${feature.allowed ? "checked" : ""}><span><strong>${escapeHtml(feature.name)}</strong></span></label>`).join("");
    return `<form class="private-feature-policy" id="privatePolicyForm" data-revision="${escapeHtml(policy.revision || "")}" novalidate>
      <div class="toggle-line"><span><strong>在此私聊使用机器人</strong><small>关闭后不在这段私聊中回复或发送定时任务</small></span><span class="toggle"><input id="privateBotEnabled" name="bot_enabled" type="checkbox" ${policy.bot_enabled !== false ? "checked" : ""}><label class="toggle-track" for="privateBotEnabled" aria-label="切换此私聊机器人"></label><span>${policy.bot_enabled !== false ? "已启用" : "已停用"}</span></span></div>
      <div class="toggle-line"><span><strong>接收已配置的定时任务</strong><small>关闭后暂停该联系人的全部定时推送</small></span><span class="toggle"><input id="privateScheduledEnabled" name="scheduled_enabled" type="checkbox" ${policy.scheduled_enabled !== false ? "checked" : ""}><label class="toggle-track" for="privateScheduledEnabled" aria-label="切换此私聊定时任务"></label><span>${policy.scheduled_enabled !== false ? "已允许" : "已暂停"}</span></span></div>
      <div class="toggle-line"><span><strong>安静时段</strong><small>此时段不发送定时消息</small></span><span class="toggle"><input id="privateQuietEnabled" name="quiet_enabled" type="checkbox" ${quiet.enabled ? "checked" : ""}><label class="toggle-track" for="privateQuietEnabled" aria-label="切换此私聊安静时段"></label><span>${quiet.enabled ? "已启用" : "已关闭"}</span></span></div>
      <div class="field-pair"><label><span>开始</span><input name="quiet_start" type="time" value="${escapeHtml(quiet.start || "22:00")}"></label><label><span>结束</span><input name="quiet_end" type="time" value="${escapeHtml(quiet.end || "08:00")}"></label></div>
      <label class="private-scheduled-limit"><span>每日定时消息上限</span><input name="daily_limit" type="number" min="0" max="100" value="${escapeHtml(policy.daily_limit ?? 3)}"><small>填 0 表示暂停该联系人的定时推送。</small></label>
      <h3>可用功能</h3>
      <p>勾选机器人在这段私聊中可以使用的功能。未单独配置的联系人沿用新安装默认启用范围。</p>
      <div class="option-grid compact-options private-feature-options">${choices || "<span class='subtle'>当前没有可分配的功能</span>"}</div>
    </form>`;
  }

  function renderContactInspector() {
    const panel = $("#contactInspector");
    const contact = state.contacts.find((item) => item.user_id === state.selectedContact);
    panel.classList.toggle("empty", !contact);
    if (!contact) {
      const message = state.contacts.length
        ? "选择左侧联系人后，可查看微信资料并配置可用功能。"
        : "刷新并读取到联系人后，可在这里查看个人资料和可用功能。";
      panel.innerHTML = `<div class="group-inspector-empty"><div class="empty-symbol">友</div><span class="eyebrow">个人详情</span><h2>${state.contacts.length ? "选择一个联系人" : "暂时没有联系人"}</h2><p>${message}</p></div>`;
      return;
    }
    const detailState = state.contactDetails[contact.user_id];
    const detail = detailState && !detailState.loading && !detailState.error ? {...contact, ...detailState} : contact;
    const displayName = contactDisplayName(detail);
    const facts = [
      contactFact("昵称", detail.nickname),
      contactFact("备注", detail.remark),
      contactFact("微信号", detail.alias, {code: true}),
      contactFact("微信标识", detail.user_id, {code: true}),
      contactFact("性别", detail.gender),
      contactFact("地区", contactRegion(detail) === "未提供" ? "" : contactRegion(detail)),
      contactFact("联系人类型", detail.contact_type),
      contactFact("个性签名", detail.signature),
    ].filter(Boolean).join("");
    const detailsMessage = detailState?.loading
      ? '<div class="profile-state">正在读取最新个人资料…</div>'
      : detailState?.error
        ? `<div class="profile-state error">${escapeHtml(detailState.error)}</div>`
        : facts ? `<dl class="contact-facts">${facts}</dl>` : '<div class="profile-state">当前接口没有返回更多个人资料。</div>';
    const policyData = state.privatePolicies[contact.user_id];
    const saveDisabled = !policyData || policyData.loading || policyData.error;
    panel.innerHTML = `<header class="inspector-hero"><span class="eyebrow">当前联系人</span><div class="inspector-group-title">${avatarMarkup("contact-avatar large", displayName, detail.avatar)}<div><h2>${escapeHtml(displayName)}</h2><code class="inspector-id">${escapeHtml(detail.alias || detail.user_id)}</code></div></div><button class="row-action chat-scheduled-entry" type="button">该联系人的定时任务</button></header>
      <div class="inspector-scroll contact-detail-scroll"><section class="contact-profile"><div class="contact-section-heading"><h3>个人详情</h3><button class="row-action copy-contact" type="button">复制微信标识</button></div>${detailsMessage}</section>${privatePolicyMarkup(policyData)}</div>
      <footer class="inspector-footer"><span>资料只在当前运行期间保存在内存中。</span><button class="button primary" type="submit" form="privatePolicyForm" ${saveDisabled ? "disabled" : ""}>保存私聊规则</button></footer>`;
    $(".copy-contact", panel)?.addEventListener("click", () => copyText(contact.user_id, "微信标识"));
    $(".chat-scheduled-entry", panel)?.addEventListener("click", () => openScheduledForChat({type: "private", id: contact.user_id, name: displayName}, state.contactAccount));
    bindAvatarFallbacks(panel);
    $("#privatePolicyForm")?.addEventListener("submit", savePrivatePolicy);
    $$(".toggle input", panel).forEach((input) => input.addEventListener("change", updateToggleCopy));
  }

  async function savePrivatePolicy(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $('button[form="privatePolicyForm"]');
    if (!button || !state.selectedContact) return;
    const contactId = state.selectedContact;
    const accountId = state.contactAccount;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "正在保存…";
    try {
      await api("save_private_policy",
        accountId,
        contactId,
        {allowed_features: $$('input[name="allowed_features"]:checked', form).map((input) => input.value), bot_enabled: form.elements.bot_enabled.checked, scheduled_enabled: form.elements.scheduled_enabled.checked, quiet_hours: {enabled: form.elements.quiet_enabled.checked, start: form.elements.quiet_start.value, end: form.elements.quiet_end.value, timezone: "Asia/Shanghai"}, daily_limit: Number(form.elements.daily_limit.value)},
        form.dataset.revision || "",
      );
      if (state.selectedContact === contactId && state.contactAccount === accountId) {
        await loadPrivatePolicy(contactId);
      }
    } catch (error) { reportError(error); }
    finally {
      if (button.isConnected) {
        button.disabled = false;
        button.removeAttribute("aria-busy");
        button.textContent = "保存私聊规则";
      }
    }
  }

  async function loadConversations(options = {}) {
    const silent = options?.silent === true;
    rollHistoryDatesForward();
    const load = ++state.historyLoad;
    try {
      const data = await api("get_conversations", {
        kind: state.historyKind,
        search: $("#historySearch").value,
        start: $("#historyStart").value,
        end: $("#historyEnd").value,
        refresh_groups: options?.refreshGroups === true,
      });
      if (load !== state.historyLoad) return;
      state.conversations = data.items || [];
      if (state.selectedConversation) {
        const refreshed = state.conversations.find((item) => item.conversation_id === state.selectedConversation.conversation_id);
        state.selectedConversation = refreshed || null;
      }
      const summary = data.summary || {};
      const visible = summary.visible_conversations ?? summary.conversations ?? 0;
      $("#historySummary").textContent = `${visible} 个聊天 · ${summary.messages || 0} 条消息 · 个人消息 ${summary.private_messages || 0} · 群聊消息 ${summary.group_messages || 0} · 群目录 ${summary.directory_groups || 0}`;
      renderConversations();
      if (state.selectedConversation) {
        await selectConversation(state.selectedConversation.conversation_id, {silent});
      } else if (state.conversations.length) {
        await selectConversation(state.conversations[0].conversation_id, {silent: true});
      } else {
        ++state.messageLoad;
        state.historyOlderLoad += 1;
        state.historyHasMore = false;
        state.historyLoadingOlder = false;
        state.historyMessageQuery = "";
        state.selectedMessages = [];
        state.selectedSenderKey = "";
        $("#conversationHeader").textContent = "没有匹配的聊天";
        $("#messageList").removeAttribute("aria-busy");
        $("#messageList").innerHTML = '<div class="empty-state compact"><h2>没有找到匹配消息</h2><p>调整昵称、微信号或日期后再试。</p></div>';
        $("#olderMessagesStatus").hidden = true;
        $("#conversationInspector").innerHTML = '<span class="eyebrow">聊天信息</span><p class="subtle">当前筛选没有匹配的聊天。</p>';
        $("#newMessageButton").hidden = true;
      }
    } catch (error) { if (!silent) reportError(error); }
  }

  function historyFilters() {
    return {
      search: $("#historySearch").value,
      start: $("#historyStart").value,
      end: $("#historyEnd").value,
    };
  }

  function pagedHistoryFilters(before = null) {
    return {
      ...historyFilters(),
      paged: true,
      page_size: HISTORY_PAGE_SIZE,
      ...(before ? {before: {id: before.id, timestamp: before.timestamp}} : {}),
    };
  }

  function normalizeMessagePage(value) {
    if (Array.isArray(value)) {
      return {items: value.slice(-HISTORY_PAGE_SIZE), has_more: value.length > HISTORY_PAGE_SIZE};
    }
    return {
      items: Array.isArray(value?.items) ? value.items : [],
      has_more: Boolean(value?.has_more),
    };
  }

  function mergeMessages(...pages) {
    const messages = new Map();
    pages.flat().forEach((message) => messages.set(String(message.id), message));
    return [...messages.values()].sort((left, right) => (
      Number(left.timestamp || 0) - Number(right.timestamp || 0)
      || Number(left.id || 0) - Number(right.id || 0)
    ));
  }

  function renderConversations() {
    const list = $("#conversationList");
    const previousScroll = list.scrollTop;
    list.innerHTML = state.conversations.length ? state.conversations.map((item) => `
      <button class="conversation-item ${state.selectedConversation?.conversation_id === item.conversation_id ? "active" : ""}" data-conversation="${escapeHtml(item.conversation_id)}" data-type="${escapeHtml(item.type)}">
        ${avatarMarkup("conversation-avatar", item.display_name, item.avatar)}
        <span class="conversation-main"><span class="conversation-line"><strong>${escapeHtml(item.display_name)}</strong><time>${item.last_timestamp ? escapeHtml(new Date(item.last_timestamp * 1000).toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", hour12: false})) : "—"}</time></span><small class="conversation-id">${escapeHtml(item.conversation_id)}</small><small class="conversation-preview">${escapeHtml(item.last_content ? displayMessageContent(item.last_content) : (item.message_count ? `${item.message_count} 条本机消息` : "暂无本机消息"))}</small></span>
      </button>`).join("") : '<div class="empty-state compact"><div class="empty-symbol">空</div><h2>没有找到聊天记录</h2><p>调整筛选条件或启动机器人接收新消息。</p></div>';
    $$(".conversation-item", list).forEach((button) => button.addEventListener("click", () => selectConversation(button.dataset.conversation)));
    bindAvatarFallbacks(list);
    list.scrollTop = previousScroll;
  }

  function renderConversationHeader(item) {
    $("#conversationHeader").innerHTML = `<div class="conversation-header-main">${avatarMarkup("conversation-avatar", item.display_name, item.avatar)}<div><strong>${escapeHtml(item.display_name)}</strong><span>${escapeHtml(item.conversation_id)}</span></div></div><span class="status-badge online"><span></span>${item.message_count} 条消息${item.message_count ? " · 自动更新中" : " · 暂无消息"}</span>`;
    bindAvatarFallbacks($("#conversationHeader"));
  }

  function messagesSignature(messages) {
    return JSON.stringify(messages.map((message) => [
      message.id, message.timestamp, message.role, message.sender_id,
      message.sender_name, message.group_nickname, message.wechat_name, message.wechat_id,
      message.avatar, message.content, message.message_kind,
      message.quote_sender, message.quote_content,
    ]));
  }

  function xmlFragment(content, tag) {
    const match = content.match(new RegExp(`<${tag}\\b[^>]*>([\\s\\S]*?)<\\/${tag}>`, "i"));
    return match ? match[1] : "";
  }

  function xmlText(content, tag, limit = 80) {
    let text = xmlFragment(content, tag).trim();
    if (text.startsWith("<![CDATA[") && text.endsWith("]]>")) text = text.slice(9, -3);
    if (/[<>]/.test(text)) return "";
    const decoded = text
      .replace(/&amp;/gi, "&")
      .replace(/&quot;/gi, '"')
      .replace(/&apos;/gi, "'");
    return /[<>]/.test(decoded) ? "" : decoded.replace(/\s+/g, " ").trim().slice(0, limit);
  }

  function quotedMessageContent(refermsg) {
    const labels = {3: "[图片]", 34: "[语音]", 42: "[名片]", 43: "[视频]", 47: "[表情]", 48: "[位置]", 49: "[卡片消息]"};
    const quoteType = xmlText(refermsg, "type");
    return labels[quoteType] || xmlText(refermsg, "content", 160) || "原消息不可用";
  }

  function rawMessagePresentation(value) {
    const content = String(value ?? "");
    const structuredXml = /^\s*(?:<\?xml[^>]*>\s*)?<(?:msg|appmsg|sysmsg)\b/i;
    const plain = {content, messageKind: "", quoteSender: "", quoteContent: "", preview: content};
    if (!structuredXml.test(content)) {
      if (/^\s*\[链接卡片\]/.test(content)) {
        const readable = content.replace(/https?:\/\/\S+/gi, "").replace(/\s*[·|]\s*$/g, "").replace(/\s+/g, " ").trim();
        return {...plain, content: readable || "[链接卡片]", preview: readable || "[链接卡片]"};
      }
      return plain;
    }
    const folded = content.toLowerCase();
    const summary = (text) => ({...plain, content: text, preview: text});
    if (/<revokemsg\b/i.test(content)) {
      const notice = xmlText(content, "replacemsg", 160) || "消息已撤回";
      return {...plain, content: notice, messageKind: "revoke", preview: `[撤回] ${notice}`};
    }
    if (/<appmsg\b/i.test(content)) {
      const withTitle = (label) => {
        const title = xmlText(content, "title");
        return summary(title ? `${label} ${title}` : label);
      };
      const appType = xmlText(content, "type");
      if (appType === "57" || folded.includes("<refermsg")) {
        const reply = xmlText(content, "title", 160) || "回复了一条消息";
        const refermsg = xmlFragment(content, "refermsg");
        const quoteSender = xmlText(refermsg, "displayname");
        const quoteContent = quotedMessageContent(refermsg);
        const attribution = quoteSender ? `引用 ${quoteSender}` : "引用消息";
        return {content: reply, messageKind: "quote", quoteSender, quoteContent, preview: `${reply} · ${attribution}：${quoteContent}`};
      }
      if (appType === "6") return withTitle("[文件]");
      if (appType === "5") return withTitle("[链接卡片]");
      if (["33", "36"].includes(appType)) return withTitle("[小程序]");
      if (appType === "19" || folded.includes("<recorditem")) return withTitle("[聊天记录]");
      return withTitle("[卡片消息]");
    }
    if (/<emoji\b/i.test(content)) return summary("[表情]");
    if (/<img\b/i.test(content)) return summary("[图片]");
    if (/<voicemsg\b/i.test(content)) return summary("[语音]");
    if (/<videomsg\b/i.test(content)) return summary("[视频]");
    if (/<location\b/i.test(content)) return summary("[位置]");
    return summary("[特殊消息]");
  }

  function messagePresentation(message) {
    if (message?.message_kind === "quote") {
      const content = String(message.content || "").trim() || "回复了一条消息";
      const quoteSender = String(message.quote_sender || "").trim();
      const quoteContent = String(message.quote_content || "").trim() || "原消息不可用";
      const attribution = quoteSender ? `引用 ${quoteSender}` : "引用消息";
      return {content, messageKind: "quote", quoteSender, quoteContent, preview: `${content} · ${attribution}：${quoteContent}`};
    }
    if (message?.message_kind === "revoke") {
      const content = String(message.content || "").trim() || "消息已撤回";
      return {content, messageKind: "revoke", quoteSender: "", quoteContent: "", preview: `[撤回] ${content}`};
    }
    return rawMessagePresentation(message?.content);
  }

  function displayMessageContent(value) {
    return rawMessagePresentation(value).preview;
  }

  function messageSenderKey(message) {
    if (!message || message.role === "assistant") return "";
    return String(message.sender_id || message.wechat_id || message.sender_name || message.group_nickname || "").trim();
  }

  function messageSenderName(message) {
    return message?.group_nickname || message?.wechat_name || message?.sender_name || message?.sender_id || "用户";
  }

  function messageSenderIdentity(message) {
    return message?.wechat_id || message?.sender_id || "";
  }

  function messageBubbleMarkup(message) {
    const presentation = messagePresentation(message);
    if (presentation.messageKind === "quote") {
      const attribution = presentation.quoteSender ? `引用 · ${presentation.quoteSender}` : "引用消息";
      return `<div class="message-bubble message-bubble-quote"><p>${escapeHtml(presentation.content)}</p><blockquote><strong>${escapeHtml(attribution)}</strong><span>${escapeHtml(presentation.quoteContent)}</span></blockquote></div>`;
    }
    if (presentation.messageKind === "revoke") {
      const detail = presentation.content === "消息已撤回" ? "" : `<span>${escapeHtml(presentation.content)}</span>`;
      return `<div class="message-bubble message-bubble-notice" role="note"><strong>消息已撤回</strong>${detail}</div>`;
    }
    return `<div class="message-bubble">${escapeHtml(presentation.content)}</div>`;
  }

  function messageMarkup(message) {
    const assistant = message.role === "assistant";
    const name = assistant ? "机器人" : messageSenderName(message);
    const identity = !assistant ? messageSenderIdentity(message) : "";
    const senderKey = messageSenderKey(message);
    const selected = !assistant && senderKey && senderKey === state.selectedSenderKey;
    const avatar = assistant || !senderKey
      ? avatarMarkup("message-avatar", name, message.avatar)
      : `<button class="message-avatar-button${selected ? " selected" : ""}" type="button" data-sender-key="${escapeHtml(senderKey)}" aria-label="选中消息发送者：${escapeHtml(name)}" aria-pressed="${selected ? "true" : "false"}">${avatarMarkup("message-avatar", name, message.avatar)}</button>`;
    return `<article class="message ${assistant ? "assistant" : ""}">${avatar}<div class="message-content"><div class="message-meta"><span class="message-author"><span class="message-author-name">${escapeHtml(name)}</span>${identity ? `<small class="message-author-id">${escapeHtml(identity)}</small>` : ""}</span><time>${escapeHtml(fullTime(message.timestamp))}</time></div>${messageBubbleMarkup(message)}</div></article>`;
  }

  function updateSelectedSenderButtons() {
    $$(".message-avatar-button", $("#messageList")).forEach((button) => {
      const selected = Boolean(state.selectedSenderKey && button.dataset.senderKey === state.selectedSenderKey);
      button.classList.toggle("selected", selected);
      button.setAttribute("aria-pressed", String(selected));
    });
  }

  function selectMessageSender(senderKey) {
    if (!senderKey) return;
    const sender = [...state.selectedMessages].reverse().find((message) => messageSenderKey(message) === senderKey);
    if (!sender) return;
    state.selectedSenderKey = senderKey;
    updateSelectedSenderButtons();
    if (state.selectedConversation) renderConversationInspector(state.selectedConversation);
  }

  function bindMessageSenderSelection(root = $("#messageList")) {
    $$(".message-avatar-button", root).forEach((button) => {
      if (button.dataset.senderSelectionBound === "true") return;
      button.dataset.senderSelectionBound = "true";
      button.addEventListener("click", () => selectMessageSender(button.dataset.senderKey || ""));
    });
  }

  function renderMessages(messages, {forceBottom = false, nearBottom = true} = {}) {
    const list = $("#messageList");
    const previousIds = new Set(state.selectedMessages.map((message) => message.id));
    const added = messages.filter((message) => !previousIds.has(message.id)).length;
    const previousScroll = list.scrollTop;
    list.innerHTML = messages.length ? messages.map(messageMarkup).join("") : '<div class="empty-state compact"><p>这个聊天暂无消息；群聊仍会保留在左侧目录中。</p></div>';
    bindAvatarFallbacks(list);
    state.selectedMessages = messages;
    bindMessageSenderSelection(list);
    updateSelectedSenderButtons();
    const newButton = $("#newMessageButton");
    if (forceBottom || nearBottom) {
      list.scrollTop = list.scrollHeight;
      newButton.hidden = true;
    } else {
      list.scrollTop = previousScroll;
      if (added > 0) {
        newButton.textContent = `${added} 条新消息 · 查看`;
        newButton.hidden = false;
      }
    }
  }

  function prependOlderMessages(messages) {
    const existing = new Set(state.selectedMessages.map((message) => String(message.id)));
    const older = messages.filter((message) => !existing.has(String(message.id)));
    if (!older.length) return;
    const list = $("#messageList");
    const previousHeight = list.scrollHeight;
    const previousScroll = list.scrollTop;
    list.insertAdjacentHTML("afterbegin", older.map(messageMarkup).join(""));
    bindAvatarFallbacks(list);
    state.selectedMessages = mergeMessages(older, state.selectedMessages);
    bindMessageSenderSelection(list);
    updateSelectedSenderButtons();
    list.scrollTop = previousScroll + (list.scrollHeight - previousHeight);
  }

  async function selectConversation(conversationId, options = {}) {
    const item = state.conversations.find((value) => value.conversation_id === conversationId);
    if (!item) return;
    const silent = options?.silent === true;
    const query = JSON.stringify(historyFilters());
    const changed = state.selectedConversation?.conversation_id !== conversationId || state.historyMessageQuery !== query;
    const list = $("#messageList");
    const nearBottom = changed || list.scrollHeight - list.scrollTop - list.clientHeight < 80;
    state.selectedConversation = item;
    if (changed) {
      state.selectedMessages = [];
      state.selectedSenderKey = "";
      state.historyHasMore = false;
      state.historyLoadingOlder = false;
      state.historyMessageQuery = query;
      state.historyOlderLoad += 1;
      list.removeAttribute("aria-busy");
      $("#olderMessagesStatus").hidden = true;
    }
    renderConversations();
    renderConversationHeader(item);
    if (changed) list.innerHTML = '<div class="loading-row">正在读取消息…</div>';
    const load = ++state.messageLoad;
    try {
      const page = normalizeMessagePage(await api("get_messages", conversationId, pagedHistoryFilters()));
      if (load !== state.messageLoad || state.selectedConversation?.conversation_id !== conversationId) return;
      const messages = changed ? page.items : mergeMessages(state.selectedMessages, page.items);
      if (changed) state.historyHasMore = page.has_more;
      if (changed || messagesSignature(messages) !== messagesSignature(state.selectedMessages)) {
        renderMessages(messages, {forceBottom: changed, nearBottom});
      }
      renderConversationInspector(item);
    } catch (error) { if (!silent) reportError(error); }
  }

  async function loadOlderMessages() {
    const list = $("#messageList");
    const selected = state.selectedConversation;
    const oldest = state.selectedMessages[0];
    if (!selected || !oldest || !state.historyHasMore || state.historyLoadingOlder || list.scrollTop > HISTORY_PRELOAD_THRESHOLD) return;
    state.historyLoadingOlder = true;
    const request = ++state.historyOlderLoad;
    const messageGeneration = state.messageLoad;
    const status = $("#olderMessagesStatus");
    list.setAttribute("aria-busy", "true");
    const feedbackTimer = window.setTimeout(() => {
      if (request === state.historyOlderLoad) status.hidden = false;
    }, 300);
    try {
      const page = normalizeMessagePage(await api("get_messages", selected.conversation_id, pagedHistoryFilters(oldest)));
      if (request !== state.historyOlderLoad || messageGeneration !== state.messageLoad || state.selectedConversation?.conversation_id !== selected.conversation_id) return;
      state.historyHasMore = page.has_more;
      prependOlderMessages(page.items);
    } catch (error) {
      if (request === state.historyOlderLoad) reportError(error);
    } finally {
      window.clearTimeout(feedbackTimer);
      if (request === state.historyOlderLoad) {
        state.historyLoadingOlder = false;
        status.hidden = true;
        list.removeAttribute("aria-busy");
      }
    }
  }

  async function pollHistory() {
    if (state.activePage !== "history" || state.historyPolling || state.historyLoadingOlder) return;
    state.historyPolling = true;
    try {
      await loadConversations({silent: true});
    } finally { state.historyPolling = false; }
  }

  async function pollGroups() {
    if (state.activePage !== "groups" || state.chatManagementTab !== "groups" || !state.groupsLoaded) return;
    await loadGroups(false, {background: true});
  }

  async function refreshVisibleChatData() {
    if (document.visibilityState === "hidden") return;
    if (state.activePage === "groups" && state.chatManagementTab === "groups") {
      await loadGroups(true, {background: true});
      return;
    }
    if (state.activePage !== "history" || state.historyPolling || state.historyLoadingOlder) return;
    state.historyPolling = true;
    try {
      await loadConversations({silent: true, refreshGroups: true});
    } finally {
      state.historyPolling = false;
    }
  }

  function renderConversationInspector(item) {
    const latestSender = [...state.selectedMessages].reverse().find((message) => message.role !== "assistant");
    const selectedSender = state.selectedSenderKey
      ? [...state.selectedMessages].reverse().find((message) => messageSenderKey(message) === state.selectedSenderKey)
      : null;
    const currentSender = selectedSender || latestSender;
    const senderName = currentSender ? messageSenderName(currentSender) : "尚无发送者";
    const senderId = currentSender ? (messageSenderIdentity(currentSender) || "标识未提供") : "标识未提供";
    const identityFacts = item.type === "group"
      ? `<div><dt>群昵称</dt><dd>${escapeHtml(senderName)}</dd></div><div><dt>微信号</dt><dd>${escapeHtml(senderId)}</dd></div>`
      : `<div><dt>微信号</dt><dd>${escapeHtml(senderId)}</dd></div>`;
    $("#conversationInspector").innerHTML = `<span class="eyebrow">聊天信息</span><div class="inspector-conversation-title">${avatarMarkup("conversation-avatar", item.display_name, item.avatar)}<div><h2>${escapeHtml(item.display_name)}</h2><p>${escapeHtml(item.conversation_id)}</p></div></div><section class="current-sender"><span>当前消息发送者</span><div>${avatarMarkup("message-avatar", senderName, currentSender?.avatar)}<div><strong>${escapeHtml(senderName)}</strong><small>${escapeHtml(senderId)}</small></div><button class="row-action" type="button" id="copySenderId">复制${currentSender?.wechat_id && currentSender.wechat_id !== currentSender.sender_id ? "微信号" : "微信标识"}</button></div></section><dl class="inspector-list">${identityFacts}<div><dt>消息数量</dt><dd>${item.message_count} 条</dd></div><div><dt>最近消息</dt><dd>${escapeHtml(fullTime(item.last_timestamp))}</dd></div></dl>${item.message_count ? '<div class="inspector-actions"><button class="button danger-ghost small" id="deleteConversation">删除这段记录</button></div>' : '<p class="subtle">群聊当前没有本机消息副本，因此无需清理。</p>'}`;
    $("#copySenderId")?.addEventListener("click", () => copyText(senderId, currentSender?.wechat_id && currentSender.wechat_id !== currentSender.sender_id ? "微信号" : "微信标识"));
    bindAvatarFallbacks($("#conversationInspector"));
    $("#deleteConversation")?.addEventListener("click", () => confirmAction({
      title: "删除这段聊天记录？", message: "仅删除管理软件中的本机副本，不会影响微信原始聊天，也不会损伤机器人的记忆数据。", confirmText: "删除记录",
      onConfirm: async () => {
        await api("delete_conversation", item.conversation_id);
        state.selectedConversation = null;
        state.selectedSenderKey = "";
        $("#messageList").innerHTML = '<div class="empty-state compact"><p>请选择聊天记录</p></div>';
        $("#conversationInspector").innerHTML = '<span class="eyebrow">聊天信息</span><h2>尚未选择</h2>';
        await loadConversations(); toast("聊天记录副本已删除。 ");
      },
    }));
  }

  async function loadSchedules() {
    const generation = ++state.scheduledLoad;
    const button = $("#refreshSchedules");
    button.disabled = true;
    try {
      const [context, legacy] = await Promise.allSettled([api("get_scheduled_task_context"), api("get_schedules")]);
      if (generation !== state.scheduledLoad) return;
      if (context.status === "rejected") throw context.reason;
      state.scheduledContext = context.value;
      state.schedules = legacy.status === "fulfilled" ? (legacy.value.items || []).filter((item) => !item.action) : [];
      const message = $("#scheduleContextMessage");
      message.textContent = context.value.message || (!context.value.account_id ? "请先登录机器人微信账号，再选择发送对象。可以先填写任务草稿。" : "");
      if (legacy.status === "rejected") message.textContent += " 已有功能任务暂时无法读取，请刷新重试。";
      message.hidden = !message.textContent;
      renderSchedules();
      renderScheduledTasks();
      if (state.scheduledEditor && !$("#scheduledTaskEditor").hidden) updateScheduledSummary();
    } catch (error) {
      $("#scheduleContextMessage").hidden = false;
      $("#scheduleContextMessage").textContent = error.message || "任务暂时无法读取，请刷新重试。";
      reportError(error);
    } finally { if (generation === state.scheduledLoad) button.disabled = false; }
  }

  const SCHEDULED_ACTIONS = {weather: "天气预报", news: "热点资讯", oilprice: "油价提醒", kfc: "星期四文案", short_drama: "影视推荐", text: "文字提醒"};
  const SCHEDULED_DEFAULTS = {weather: {city: "", day: "today", output: "image"}, news: {count: 10}, oilprice: {province: ""}, kfc: {}, short_drama: {media_type: "短剧", limit: 12}, text: {text: ""}};
  const SCHEDULED_DAYS = ["一", "二", "三", "四", "五", "六", "日"];
  const SCHEDULED_WEEK_PRESETS = {thursday: [3], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6]};
  const SCHEDULED_RESULTS = {submitted: "已提交微信", failed: "发送失败", unknown: "结果待确认", skipped: "已跳过", pending: "等待发送", running: "正在执行", completed: "执行完成", partial: "部分失败"};

  function scheduledTime(value) {
    if (!value) return "—";
    const date = new Date(value);
    if (!Number.isFinite(date.getTime())) return "时间待确认";
    return new Intl.DateTimeFormat("zh-CN", {timeZone: "Asia/Shanghai", month: "2-digit", day: "2-digit", weekday: "short", hour: "2-digit", minute: "2-digit", hour12: false}).format(date);
  }

  function scheduledTimeText(schedule = {}) {
    const time = schedule.time || "待选择时间";
    if (schedule.kind === "once") return `${schedule.date || "待选择日期"} ${time}，仅一次`;
    if (schedule.kind === "weekly") return `每周${(schedule.weekdays || []).map((day) => SCHEDULED_DAYS[day]).join("、") || "待选择"} ${time}`;
    if (schedule.kind === "monthly") return `每月${[...(schedule.monthdays || []).map((day) => `${day}日`), ...(schedule.last_day ? ["最后一天"] : [])].join("、") || "待选择日期"} ${time}`;
    return `每天 ${time}`;
  }

  function scheduledKindValue(schedule) {
    if (schedule.kind !== "weekly") return schedule.kind;
    return Object.keys(SCHEDULED_WEEK_PRESETS).find((key) => {
      const days = schedule.weekdays || [];
      return days.length === SCHEDULED_WEEK_PRESETS[key].length && SCHEDULED_WEEK_PRESETS[key].every((day) => days.includes(day));
    }) || "weekly";
  }

  function scheduledTimingMarkup(schedule) {
    const kinds = {daily: "每天", thursday: "每周四", weekdays: "每周一至周五", weekends: "每周末（周六、周日）", weekly: "每周（自选星期，可多选）", monthly: "每月（自选日期，可多选）", once: "仅一次"};
    return `<div class="scheduled-time-fields"><div class="form-field"><label for="scheduledKind">重复方式</label><select id="scheduledKind">${Object.entries(kinds).map(([value, label]) => `<option value="${value}" ${scheduledKindValue(schedule) === value ? "selected" : ""}>${label}</option>`).join("")}</select></div>
      <div class="form-field"><label for="scheduledTime">北京时间</label><input id="scheduledTime" type="time" value="${escapeHtml(schedule.time || "08:00")}"></div>
      <div class="form-field" id="scheduledDateField" ${schedule.kind !== "once" ? "hidden" : ""}><label for="scheduledDate">执行日期</label><input id="scheduledDate" type="date" value="${escapeHtml(schedule.date || "")}"></div></div>
      <div id="scheduledWeekdays" ${schedule.kind !== "weekly" ? "hidden" : ""}><p class="subtle">选择星期（可多选）；只选周四即为每周四发送。</p><div class="schedule-weekdays" role="group" aria-label="每周执行星期">${SCHEDULED_DAYS.map((name, day) => `<label><input type="checkbox" value="${day}" ${(schedule.weekdays || []).includes(day) ? "checked" : ""}>周${name}</label>`).join("")}</div><button id="scheduledThursdayShortcut" class="row-action" type="button">只选周四</button><button id="scheduledWeekdayShortcut" class="row-action" type="button">选择周一至周五</button></div>
      <div id="scheduledMonthdays" ${schedule.kind !== "monthly" ? "hidden" : ""}><p class="subtle">选择每月日期（可多选）</p><div class="schedule-weekdays schedule-monthdays" role="group" aria-label="每月执行日期">${Array.from({length: 31}, (_, index) => index + 1).map((day) => `<label><input type="checkbox" value="${day}" ${(schedule.monthdays || []).includes(day) ? "checked" : ""}>${day}日</label>`).join("")}<label class="schedule-month-end"><input id="scheduledLastDay" type="checkbox" ${schedule.last_day ? "checked" : ""}>月末最后一天</label></div><p class="subtle">当月没有所选日期时跳过；选择“月末最后一天”会自动按当月实际天数执行。同一天只发送一次。</p></div>
      <p class="subtle">电脑离线错过的时间会跳过，恢复后按下一次计划执行。</p>`;
  }

  function scheduledTargetText(targets = []) {
    const groups = targets.filter((item) => item.type === "group").length;
    const contacts = targets.filter((item) => item.type === "private").length;
    return [groups ? `${groups} 个群` : "", contacts ? `${contacts} 位联系人` : ""].filter(Boolean).join("、") || "尚未选择对象";
  }

  function scheduledName(item) {
    const args = item.arguments || {};
    return item.action === "weather" ? `${args.city || "城市待填写"}${args.day === "tomorrow" ? "明天" : "当天"}天气` : item.action === "oilprice" ? `${args.province || "省份待填写"}油价` : item.action === "short_drama" ? `${args.media_type || "短剧"}推荐` : SCHEDULED_ACTIONS[item.action] || "定时任务";
  }

  function scheduledStatus(item) {
    if (item.draft || item.status === "draft") return "草稿";
    return ({enabled: "已启用", paused: "已暂停", completed: "已完成", running: "正在执行", blocked: "待处理", waiting_account: "等待账号登录", waiting_runtime: "等待机器人就绪"})[item.status] || item.status || (item.enabled ? "已启用" : "已暂停");
  }

  function scheduledResultText(value) {
    if (!value) return "尚无执行记录";
    if (typeof value === "string") return SCHEDULED_RESULTS[value] || value;
    return value.message || SCHEDULED_RESULTS[value.status] || "查看执行记录";
  }

  function renderScheduledTasks() {
    const all = state.scheduledContext?.items || [];
    const total = all.length + state.schedules.length;
    $("#scheduleTotal").textContent = total;
    $("#scheduleTableTotal").textContent = total;
    $("#scheduleRunning").textContent = all.filter((item) => item.enabled).length + state.schedules.filter((item) => item.enabled).length;
    $("#schedulePaused").textContent = all.filter((item) => !item.enabled && item.status !== "completed").length + state.schedules.filter((item) => !item.enabled).length;
    const action = $("#scheduledActionFilter").value;
    const target = $("#scheduledTargetFilter").value;
    const status = $("#scheduledStatusFilter").value;
    const chat = state.scheduledChatFilter;
    const clear = $("#clearScheduledChatFilter");
    clear.hidden = !chat;
    clear.disabled = !chat;
    clear.textContent = chat ? `${chat.name} · 清除筛选` : "";
    clear.setAttribute("aria-label", chat ? `清除聊天筛选：${chat.name}` : "清除聊天筛选");
    const items = all.filter((item) => (action === "all" || action === item.action)
      && (target === "all" || (item.targets || []).some((entry) => entry.type === target))
      && (!chat || (item.targets || []).some((entry) => entry.id === chat.id && entry.type === chat.type))
      && (status === "all" || (status === "enabled" && item.enabled) || (status === "draft" && item.draft) || (status === "paused" && !item.enabled && !item.draft && item.status !== "completed") || (status === "attention" && ["blocked", "failed", "waiting_account", "waiting_runtime"].includes(item.status))));
    $("#scheduledTaskHead").hidden = items.length === 0;
    $("#scheduledTaskTable").classList.toggle("is-empty", items.length === 0);
    $("#scheduledTaskRows").innerHTML = items.length ? items.map((item) => `<tr>
      <td><button class="row-action scheduled-name" data-task-action="edit" data-id="${escapeHtml(item.id)}">${escapeHtml(item.name || scheduledName(item))}</button><small>${escapeHtml(SCHEDULED_ACTIONS[item.action] || "定时任务")}</small></td>
      <td><span>${escapeHtml(scheduledTargetText(item.targets))}</span><small class="schedule-target-names">${escapeHtml((item.targets || []).map((entry) => entry.name || "待确认对象").join("、"))}</small></td>
      <td>${escapeHtml(item.schedule_text || scheduledTimeText(item.schedule))}<small>下次：${escapeHtml(scheduledTime(item.next_run_time))}</small></td>
      <td><span class="task-status ${["enabled", "running"].includes(item.status) ? "running" : ["blocked", "waiting_account", "waiting_runtime"].includes(item.status) ? "warning" : ""}">${escapeHtml(scheduledStatus(item))}</span><small>${escapeHtml(item.status_message || scheduledResultText(item.last_result))}</small></td>
      <td><div class="row-actions"><button class="row-action" data-task-action="${item.draft ? "edit" : "toggle"}" data-id="${escapeHtml(item.id)}">${item.draft ? "继续编辑" : item.enabled ? "暂停" : "启用"}</button><button class="row-action" data-task-action="runs" data-id="${escapeHtml(item.id)}">记录</button><button class="row-action" data-task-action="copy" data-id="${escapeHtml(item.id)}">复制</button><button class="row-action delete" data-task-action="delete" data-id="${escapeHtml(item.id)}">删除</button></div></td></tr>`).join("") : `<tr><td colspan="5" class="schedule-empty-cell"><div class="schedule-empty-state"><div class="schedule-empty-icon" aria-hidden="true">${icon("i-clock")}</div><h2>${all.length ? "没有符合条件的任务" : "暂无定时任务"}</h2><p>${all.length ? "调整内容、发送对象或状态筛选，查看其他任务。" : "可创建每日天气预报、定时资讯或一句提醒，选择发送对象后自动触发。"}</p><button class="button secondary schedule-empty-cta" id="emptyNewScheduledTask">${all.length ? "+ 新建任务" : "+ 创建第一个定时任务"}</button></div></td></tr>`;
    $$('[data-task-action]', $("#scheduledTaskRows")).forEach((button) => button.addEventListener("click", () => scheduledTaskAction(button.dataset.taskAction, button.dataset.id)));
    $("#emptyNewScheduledTask")?.addEventListener("click", () => openScheduledEditor());
  }

  function guardScheduledDraft(next) {
    if (!state.scheduledDirty || !state.scheduledEditor) return false;
    confirmAction({title: "放弃任务修改？", message: "内容、发送对象或时间还没有保存。留在此页可以保存草稿。", confirmText: "放弃修改并继续", onConfirm: async () => { state.scheduledDirty = false; state.scheduledEditor = null; showScheduledList(); await next(); }});
    return true;
  }

  function showScheduledList() {
    $("#scheduleListView").hidden = false;
    $("#scheduledTaskEditor").hidden = true;
    $("#scheduledTaskRuns").hidden = true;
    $("#newScheduledTask").hidden = false;
    state.scheduledTimeLoad += 1;
  }

  async function openScheduledEditor(item = null, copy = false, action = "weather") {
    if (state.scheduledBusy || guardScheduledDraft(() => openScheduledEditor(item, copy, action))) return;
    if (!state.scheduledContext) await loadSchedules();
    const editor = item ? structuredClone(item) : {id: crypto.randomUUID(), account_id: state.scheduledContext?.account_id || "", action, arguments: structuredClone(SCHEDULED_DEFAULTS[action] || {}), targets: state.scheduledChatFilter ? [structuredClone(state.scheduledChatFilter)] : [], schedule: {kind: "daily", time: "08:00", timezone: "Asia/Shanghai"}, enabled: false, draft: true, name: ""};
    if (copy) { editor.id = crypto.randomUUID(); delete editor.revision; editor.name = `${item.name || scheduledName(item)}（副本）`; editor.enabled = false; editor.draft = true; editor.status = "draft"; }
    editor.targets ||= [];
    editor.arguments ||= {};
    editor.schedule ||= {kind: "daily", time: "08:00", timezone: "Asia/Shanghai"};
    state.scheduledEditor = editor;
    state.scheduledDirty = Boolean(copy);
    state.scheduledTargetTab = "group";
    renderScheduledEditor();
    $("#scheduledEditorHeading")?.focus();
  }

  function scheduledArgumentsMarkup(editor) {
    const args = editor.arguments;
    const field = (key, label, control) => `<div class="form-field"><label for="scheduledArg-${key}">${label}</label>${control}</div>`;
    if (editor.action === "weather") return field("city", "城市", `<input id="scheduledArg-city" data-task-arg="city" value="${escapeHtml(args.city || "")}" placeholder="例如：青岛" maxlength="80">`) + field("day", "天气日期", `<select id="scheduledArg-day" data-task-arg="day"><option value="today" ${args.day !== "tomorrow" ? "selected" : ""}>执行当天</option><option value="tomorrow" ${args.day === "tomorrow" ? "selected" : ""}>执行日的明天</option></select><small>以图片卡片发送，与手动查询的样式一致。</small>`);
    if (editor.action === "news") return field("count", "资讯条数", `<input id="scheduledArg-count" data-task-arg="count" type="number" min="1" max="30" value="${escapeHtml(args.count ?? 10)}"><small>每次执行获取最新资讯。</small>`);
    if (editor.action === "oilprice") return field("province", "省份", `<input id="scheduledArg-province" data-task-arg="province" value="${escapeHtml(args.province || "")}" maxlength="80" placeholder="例如：山东">`);
    if (editor.action === "short_drama") return field("media_type", "推荐分类", `<select id="scheduledArg-media_type" data-task-arg="media_type">${["短剧", "电视剧", "电影", "动漫", "综艺", "体育", "全部"].map((value) => `<option value="${value}" ${args.media_type === value ? "selected" : ""}>${value === "体育" ? "体育回放" : value === "全部" ? "全部分类" : value}</option>`).join("")}</select>`) + field("limit", "每次推荐", `<input id="scheduledArg-limit" data-task-arg="limit" type="number" min="1" max="20" value="${escapeHtml(args.limit ?? 12)}"><small>每次执行实时获取最新内容，并发送可点击播放的微信卡片。</small>`);
    if (editor.action === "text") return field("text", "提醒文字", `<textarea id="scheduledArg-text" data-task-arg="text" rows="4" maxlength="4000" placeholder="填写希望按时发送的文字">${escapeHtml(args.text || "")}</textarea><small>按原文发送，不会临时改写。</small>`);
    return '<p class="subtle">每次使用最新的星期四文案。可在下方选择每周四发送。</p>';
  }

  function renderScheduledEditor() {
    const editor = state.scheduledEditor;
    if (!editor) return;
    $("#scheduleListView").hidden = true;
    $("#scheduledTaskRuns").hidden = true;
    $("#newScheduledTask").hidden = true;
    const root = $("#scheduledTaskEditor");
    root.hidden = false;
    root.innerHTML = `<div class="schedule-editor-heading"><button class="button ghost small" id="scheduledBack" type="button">返回任务列表</button><h2 id="scheduledEditorHeading" tabindex="-1">${editor.revision ? "编辑任务" : "新建任务"}</h2></div>
      <div class="schedule-editor-layout"><form id="scheduledTaskForm" novalidate>
        <section class="panel scheduled-form-section"><h3><span>1</span>做什么</h3><div class="form-field"><label for="scheduledName">任务名称</label><input id="scheduledName" value="${escapeHtml(editor.name || "")}" placeholder="自动使用内容名称，也可以自己命名" maxlength="80"></div>
        <div class="form-field"><label for="scheduledAction">发送内容</label><select id="scheduledAction">${Object.entries(SCHEDULED_ACTIONS).map(([value, name]) => `<option value="${value}" ${value === editor.action ? "selected" : ""}>${name}</option>`).join("")}</select></div><div id="scheduledArguments" class="scheduled-arguments">${scheduledArgumentsMarkup(editor)}</div>
        <div id="scheduledReadiness" class="schedule-notice"></div><button class="row-action" id="scheduledPrepareFeature" type="button" hidden>去准备功能，保留当前草稿</button>
        <details class="schedule-command"><summary>从已有命令填写</summary><label for="scheduledCommand">命令</label><div><input id="scheduledCommand" placeholder="例如：/青岛天气"><button class="button secondary small" id="scheduledParseCommand" type="button">识别并填写</button></div><small>只识别已支持的内容；不会执行命令或发送消息。</small></details></section>
        <section class="panel scheduled-form-section"><h3><span>2</span>发给谁</h3><p class="subtle">固定选择当前账号的聊天，可同时选择群聊和私聊。</p><div class="scheduled-selected" id="scheduledSelected"></div><div class="inspector-tabs" aria-label="发送对象类型"><button type="button" data-scheduled-target-tab="group" aria-pressed="true" class="active">群聊</button><button type="button" data-scheduled-target-tab="private" aria-pressed="false">私聊</button></div><div class="form-field"><label for="scheduledTargetSearch">搜索聊天</label><label class="search-box wide" id="scheduledTargetSearchBox"><svg><use href="#i-search"/></svg><input id="scheduledTargetSearch" type="search" placeholder="搜索群名、昵称或备注" autocomplete="off"><button type="button" class="clear-search" aria-label="清空聊天搜索">×</button></label></div><div id="scheduledTargetChoices" class="scheduled-target-choices"></div></section>
        <section class="panel scheduled-form-section"><h3><span>3</span>什么时候</h3>${scheduledTimingMarkup(editor.schedule)}</section>
      </form><aside class="schedule-summary panel"><span class="eyebrow">确认你的安排</span><h3 id="scheduledSummaryName"></h3><p id="scheduledSummaryText" aria-live="polite"></p><div class="schedule-next-times"><strong>未来执行时间 · 北京时间</strong><ol id="scheduledNextTimes"><li>正在计算…</li></ol></div><p class="subtle">启用只授权本任务向所选聊天定时发送，不会允许 AI 主动聊天。聊天总开关、安静时段及每日上限仍然有效。</p><div id="scheduledPreview" class="scheduled-preview" hidden></div><div id="scheduledTaskError" class="modal-error" role="alert" hidden></div><div class="scheduled-editor-actions"><button class="button secondary" id="scheduledPreviewButton" type="button">预览内容</button><button class="button secondary" id="scheduledTestButton" type="button">发送测试消息</button><button class="button ghost" id="scheduledSaveDraft" type="button">保存草稿</button><button class="button primary" id="scheduledSaveEnabled" type="button">${editor.revision ? "保存并启用" : "创建并启用"}</button></div><small>预览只在这里显示；测试发送必须再明确选择一个聊天。</small></aside></div>`;
    $("#scheduledBack").addEventListener("click", () => { if (!guardScheduledDraft(showScheduledList)) { state.scheduledEditor = null; showScheduledList(); } });
    $("#scheduledTaskForm").addEventListener("submit", (event) => event.preventDefault());
    $("#scheduledName").addEventListener("input", (event) => { editor.name = event.target.value; scheduledChanged(false); });
    $("#scheduledAction").addEventListener("change", (event) => { editor.action = event.target.value; editor.arguments = structuredClone(SCHEDULED_DEFAULTS[editor.action] || {}); state.scheduledDirty = true; renderScheduledEditor(); $("#scheduledAction").focus(); });
    $$('[data-task-arg]', root).forEach((input) => input.addEventListener("input", () => { editor.arguments[input.dataset.taskArg] = input.type === "number" ? Number(input.value) : input.value; scheduledChanged(false); }));
    $$('[data-scheduled-target-tab]', root).forEach((button) => button.addEventListener("click", () => { state.scheduledTargetTab = button.dataset.scheduledTargetTab; $$('[data-scheduled-target-tab]', root).forEach((tab) => { tab.classList.toggle("active", tab === button); tab.setAttribute("aria-pressed", String(tab === button)); }); renderScheduledTargets(); }));
    const search = $("#scheduledTargetSearch");
    const searchTargets = debounce(renderScheduledTargets);
    search.addEventListener("compositionstart", () => { search.dataset.composing = "true"; });
    search.addEventListener("compositionend", () => { search.dataset.composing = "false"; searchTargets(); });
    search.addEventListener("input", () => { $("#scheduledTargetSearchBox").classList.toggle("has-value", Boolean(search.value)); if (search.dataset.composing !== "true") searchTargets(); });
    $(".clear-search", $("#scheduledTargetSearchBox")).addEventListener("click", () => { search.value = ""; $("#scheduledTargetSearchBox").classList.remove("has-value"); searchTargets(); search.focus(); });
    const syncScheduleFields = () => {
      $("#scheduledDateField").hidden = editor.schedule.kind !== "once";
      $("#scheduledWeekdays").hidden = editor.schedule.kind !== "weekly";
      $("#scheduledMonthdays").hidden = editor.schedule.kind !== "monthly";
      $$('#scheduledWeekdays input').forEach((input) => { input.checked = (editor.schedule.weekdays || []).includes(Number(input.value)); });
    };
    $("#scheduledKind").addEventListener("change", (event) => {
      const preset = SCHEDULED_WEEK_PRESETS[event.target.value];
      editor.schedule.kind = preset ? "weekly" : event.target.value;
      if (preset) editor.schedule.weekdays = [...preset];
      if (editor.schedule.kind === "weekly") editor.schedule.weekdays ||= [];
      if (editor.schedule.kind === "monthly") editor.schedule.monthdays ||= [];
      syncScheduleFields(); scheduledChanged();
    });
    $("#scheduledTime").addEventListener("input", (event) => { editor.schedule.time = event.target.value; scheduledChanged(); });
    $("#scheduledDate").addEventListener("input", (event) => { editor.schedule.date = event.target.value; scheduledChanged(); });
    $$('#scheduledWeekdays input').forEach((input) => input.addEventListener("change", () => { editor.schedule.weekdays = $$('#scheduledWeekdays input:checked').map((entry) => Number(entry.value)); $("#scheduledKind").value = scheduledKindValue(editor.schedule); scheduledChanged(); }));
    const selectWeekdays = (days) => { editor.schedule.weekdays = [...days]; $("#scheduledKind").value = scheduledKindValue(editor.schedule); syncScheduleFields(); scheduledChanged(); };
    $("#scheduledThursdayShortcut").addEventListener("click", () => selectWeekdays([3]));
    $("#scheduledWeekdayShortcut").addEventListener("click", () => selectWeekdays([0, 1, 2, 3, 4]));
    $$('#scheduledMonthdays input').forEach((input) => input.addEventListener("change", () => {
      editor.schedule.monthdays = $$('#scheduledMonthdays input[value]:checked').map((entry) => Number(entry.value));
      editor.schedule.last_day = $("#scheduledLastDay").checked;
      scheduledChanged();
    }));
    $("#scheduledParseCommand").addEventListener("click", parseScheduledCommand);
    $("#scheduledPrepareFeature").addEventListener("click", prepareScheduledFeature);
    $("#scheduledPreviewButton").addEventListener("click", previewScheduledContent);
    $("#scheduledTestButton").addEventListener("click", testScheduledContent);
    $("#scheduledSaveDraft").addEventListener("click", () => saveScheduledTask(true));
    $("#scheduledSaveEnabled").addEventListener("click", () => saveScheduledTask(false));
    renderScheduledTargets(); updateScheduledSummary(); void previewScheduledTimes();
  }

  function scheduledChanged(times = true) {
    state.scheduledDirty = true;
    $("#scheduledPreview").hidden = true;
    $("#scheduledTaskError").hidden = true;
    updateScheduledSummary();
    if (times) void previewScheduledTimes();
  }

  function renderScheduledTargets() {
    const editor = state.scheduledEditor;
    if (!editor || !$("#scheduledTargetChoices")) return;
    const type = state.scheduledTargetTab;
    const search = $("#scheduledTargetSearch").value.trim().toLocaleLowerCase();
    const context = state.scheduledContext || {};
    const sameAccount = editor.account_id === context.account_id;
    const source = sameAccount ? (type === "group" ? context.groups : context.contacts) || [] : [];
    const items = source.filter((entry) => `${entry.name || ""} ${entry.id || ""}`.toLocaleLowerCase().includes(search));
    $("#scheduledTargetChoices").innerHTML = items.length ? items.map((entry) => `<label class="scheduled-target-option"><input type="checkbox" value="${escapeHtml(entry.id)}" ${editor.targets.some((target) => target.type === type && target.id === entry.id) ? "checked" : ""}><span><strong>${escapeHtml(entry.name || "未命名聊天")}</strong><small>${escapeHtml(entry.id)}</small>${entry.message ? `<small class="scheduled-target-warning">${escapeHtml(entry.message)}</small>` : ""}</span></label>`).join("") : `<p class="member-empty">${!sameAccount ? "请登录此任务绑定的微信账号后选择对象。" : search ? "没有匹配的聊天，试试其他名称。" : "暂时没有可选聊天。请在群聊&私聊管理读取当前账号的目录后刷新。"}</p>`;
    $$('#scheduledTargetChoices input').forEach((input) => input.addEventListener("change", () => { const found = source.find((entry) => entry.id === input.value); editor.targets = editor.targets.filter((entry) => !(entry.type === type && entry.id === input.value)); if (input.checked && found) editor.targets.push({type, id: found.id, name: found.name}); renderScheduledSelected(); scheduledChanged(false); }));
    renderScheduledSelected();
  }

  function renderScheduledSelected() {
    const editor = state.scheduledEditor;
    const context = state.scheduledContext || {};
    $("#scheduledSelected").innerHTML = `<strong>${escapeHtml(scheduledTargetText(editor.targets))}</strong><div class="schedule-target-chips">${editor.targets.map((entry, index) => { const known = editor.account_id === context.account_id && ((entry.type === "group" ? context.groups : context.contacts) || []).some((item) => item.id === entry.id); return `<button class="schedule-target-chip" type="button" data-remove-target="${index}" aria-label="移除${escapeHtml(entry.name || "对象")}" disabled>${escapeHtml(entry.name || "未命名聊天")}${known ? "" : "（待确认）"} ×</button>`; }).join("")}</div>`;
    $$('[data-remove-target]').forEach((button) => { button.disabled = false; button.addEventListener("click", () => { editor.targets.splice(Number(button.dataset.removeTarget), 1); renderScheduledTargets(); scheduledChanged(false); }); });
  }

  function scheduledReadiness() {
    const editor = state.scheduledEditor;
    const context = state.scheduledContext || {};
    const capability = (context.capabilities || []).find((entry) => entry.id === editor.action);
    if (!editor.account_id || context.account_id !== editor.account_id) return {ready: false, message: "尚未确认任务绑定的微信账号。请登录对应账号后再保存；当前填写内容会保留。"};
    if (!capability || !capability.ready) return {ready: false, message: capability?.message || "所需功能尚未就绪，可以先保存草稿。", feature: true};
    if (!context.runtime_ready) return {ready: false, message: "定时发送能力尚未就绪，可以先保存草稿。"};
    return {ready: true, message: context.online ? "内容能力已就绪。启用后按所选时间执行。" : "可以保存计划，等待机器人上线后执行；错过的时间不会补发。"};
  }

  function updateScheduledSummary() {
    const editor = state.scheduledEditor;
    if (!editor || !$("#scheduledSummaryText")) return;
    const ready = scheduledReadiness();
    $("#scheduledSummaryName").textContent = editor.name || scheduledName(editor);
    $("#scheduledSummaryText").textContent = `${scheduledTimeText(editor.schedule)}，发送${scheduledName(editor)}${editor.action === "weather" ? "图片卡片" : editor.action === "short_drama" ? "播放卡片" : ""}，给${scheduledTargetText(editor.targets)}。`;
    $("#scheduledReadiness").textContent = ready.message;
    $("#scheduledReadiness").classList.toggle("warning", !ready.ready);
    $("#scheduledPrepareFeature").hidden = !ready.feature;
    $("#scheduledSaveEnabled").disabled = !ready.ready || state.scheduledBusy;
    $("#scheduledSaveDraft").disabled = !editor.account_id || state.scheduledBusy;
    $("#scheduledPreviewButton").disabled = state.scheduledBusy;
    $("#scheduledTestButton").disabled = !ready.ready || !editor.targets.length || state.scheduledBusy;
  }

  async function previewScheduledTimes() {
    const editor = state.scheduledEditor;
    const generation = ++state.scheduledTimeLoad;
    if (!editor || !$("#scheduledNextTimes")) return;
    try {
      const data = await api("preview_scheduled_times", structuredClone(editor.schedule));
      if (generation !== state.scheduledTimeLoad || state.scheduledEditor !== editor) return;
      $("#scheduledNextTimes").innerHTML = data.items?.length ? data.items.map((value) => `<li>${escapeHtml(scheduledTime(value))}</li>`).join("") : `<li>${escapeHtml(data.text || "请补齐日期、星期和时间。")}</li>`;
    } catch (error) { if (generation === state.scheduledTimeLoad && $("#scheduledNextTimes")) $("#scheduledNextTimes").innerHTML = `<li>${escapeHtml(error.message)}</li>`; }
  }

  function scheduledError(error) {
    const box = $("#scheduledTaskError");
    if (box) { box.textContent = error.product?.message || error.message || "操作未完成，已保留表单。"; box.hidden = false; }
  }

  function scheduledPayload(draft) {
    const payload = structuredClone(state.scheduledEditor);
    payload.name = payload.name.trim() || scheduledName(payload);
    payload.draft = draft;
    payload.enabled = !draft;
    payload.schedule.timezone = "Asia/Shanghai";
    if (payload.action === "weather") payload.arguments.output = "image";
    if (!draft && !payload.targets.length) throw new Error("请至少选择一个群聊或联系人。 ");
    return payload;
  }

  async function scheduledBusyAction(action) {
    if (state.scheduledBusy) return;
    state.scheduledBusy = true;
    const controls = $$("input, select, textarea, button", $("#scheduledTaskEditor"));
    const disabled = controls.map((input) => input.disabled);
    controls.forEach((input) => { input.disabled = true; });
    $("#scheduledTaskEditor").setAttribute("aria-busy", "true");
    try { await action(); } catch (error) { scheduledError(error); }
    finally { state.scheduledBusy = false; controls.forEach((input, index) => { if (input.isConnected) input.disabled = disabled[index]; }); $("#scheduledTaskEditor").removeAttribute("aria-busy"); updateScheduledSummary(); }
  }

  async function saveScheduledTask(draft) {
    await scheduledBusyAction(async () => {
      if (!draft && !scheduledReadiness().ready) throw new Error(scheduledReadiness().message);
      const payload = scheduledPayload(draft);
      const data = await api("save_scheduled_task", payload);
      if (!data.item) throw new Error("未收到任务保存确认，请刷新核对。当前草稿已保留。 ");
      state.scheduledDirty = false; state.scheduledEditor = null;
      showScheduledList(); await loadSchedules();
      toast(data.message || (data.item.draft ? "任务草稿已保存。" : data.item.enabled ? "任务已保存并启用。" : "任务已保存。"));
    });
  }

  async function previewScheduledContent() {
    await scheduledBusyAction(async () => {
      const data = await api("preview_scheduled_task", scheduledPayload(true));
      const root = $("#scheduledPreview");
      if (data.kind === "image" && /^data:image\/(png|jpeg|webp|gif);base64,[a-zA-Z0-9+/=\s]+$/.test(data.image_data_url || "")) root.innerHTML = `<strong>内容预览</strong><img src="${escapeHtml(data.image_data_url)}" alt="定时内容预览">`;
      else if (data.kind === "text") root.innerHTML = `<strong>内容预览</strong><p>${escapeHtml(data.text || "没有返回可预览的内容。")}</p>`;
      else if (data.kind === "link_card") root.innerHTML = `<strong>播放卡片预览</strong><div class="scheduled-link-card"><div><b>${escapeHtml(data.title || "影视推荐")}</b><p>${escapeHtml(data.description || "点击后打开播放页")}</p></div><span>发送到微信后可点击查看</span></div>`;
      else throw new Error("预览内容暂时无法显示，请稍后重试。 ");
      root.hidden = false;
    });
  }

  function testScheduledContent() {
    const editor = state.scheduledEditor;
    if (!editor?.targets?.length || !scheduledReadiness().ready) return;
    const options = editor.targets.map((target) => ({
      value: `${target.type}:${target.id}`,
      label: `${target.type === "group" ? "群聊" : "私聊"} · ${target.name || target.id}`,
    }));
    confirmAction({
      title: "发送一条测试消息？",
      message: "测试只发送给下面选择的一个聊天，并计入该聊天的每日推送上限。发送结果不确定时不会自动重试。",
      confirmText: "发送测试消息",
      pendingText: "正在测试发送…",
      intent: "primary",
      field: {label: "测试发送到", type: "select", value: options[0].value, options},
      onConfirm: async (value) => {
        const target = editor.targets.find((item) => `${item.type}:${item.id}` === value);
        if (!target) throw new Error("测试发送对象已经变化，请重新选择。 ");
        const result = await api("test_scheduled_task", {...scheduledPayload(false), test_target: {type: target.type, id: target.id}});
        toast(result.message || "测试发送已结束。", result.status === "failed" ? "error" : "");
      },
    });
  }

  async function parseScheduledCommand() {
    await scheduledBusyAction(async () => {
      const command = $("#scheduledCommand").value.trim();
      if (!command) throw new Error("请先输入需要识别的命令。 ");
      const parsed = await api("parse_scheduled_command", command);
      if (!SCHEDULED_ACTIONS[parsed.action]) throw new Error("这个命令暂不支持定时发送。 ");
      state.scheduledEditor.action = parsed.action; state.scheduledEditor.arguments = parsed.arguments || {}; state.scheduledDirty = true;
      renderScheduledEditor();
      toast("已填写内容参数，请确认发送对象和时间。 ");
    });
  }

  async function prepareScheduledFeature() {
    state.scheduledReturnDraft = true;
    const dirty = state.scheduledDirty;
    state.scheduledDirty = false;
    await navigate("features");
    state.scheduledDirty = dirty;
    const action = state.scheduledEditor?.action || "weather";
    const feature = state.features.find((item) => item.schedule_provider && item.scheduled_action === (action === "short_drama" ? "short_drama" : "weather"));
    if (feature) await openFeature(feature.id);
    toast("当前任务草稿已保留，完成准备后回到定时任务继续。 ");
  }

  async function openScheduledFromFeature() {
    if (state.featureDirty) { discardFeatureChanges(openScheduledFromFeature); return; }
    const action = state.featureDetail?.scheduled_action || "weather";
    if (action === "group_analysis") {
      state.featureTab = "basic";
      renderFeatureDetail();
      const section = $("#featureForm .config-section[data-config-section='auto_analysis']");
      section?.scrollIntoView({behavior: "smooth", block: "start"});
      const target = section?.querySelector("textarea, input, select");
      target?.focus({preventScroll: true});
      toast("已定位到自动日报设置；选择群聊和时间后保存即可。 ");
      return;
    }
    await navigate("schedules");
    if (state.activePage === "schedules") await openScheduledEditor(null, false, action);
  }

  async function openScheduledForChat(target, accountId) {
    await navigate("schedules");
    if (state.activePage !== "schedules") return;
    if (accountId !== state.scheduledContext?.account_id) { toast("微信账号已变化，请重新读取聊天目录。", "error"); return; }
    if (state.scheduledEditor && state.scheduledDirty) return;
    state.scheduledChatFilter = target; showScheduledList(); renderScheduledTasks();
  }

  async function scheduledTaskAction(action, id) {
    if (state.scheduledBusy) return;
    const item = (state.scheduledContext?.items || []).find((entry) => entry.id === id);
    if (!item) return;
    if (action === "edit" || action === "copy") { await openScheduledEditor(item, action === "copy"); return; }
    if (action === "runs") { await showScheduledRuns(item); return; }
    confirmAction({title: action === "delete" ? "永久删除定时任务？" : item.enabled ? "暂停定时任务？" : "启用定时任务？", message: action === "delete" ? `“${item.name}”将永久删除，重启或升级后也不会恢复。已提交微信的消息无法撤回。` : `${item.name}：${item.schedule_text || scheduledTimeText(item.schedule)}，发送给${scheduledTargetText(item.targets)}。`, confirmText: action === "delete" ? "永久删除" : item.enabled ? "暂停任务" : "启用任务", intent: action === "delete" ? "danger" : action === "toggle" && !item.enabled ? "primary" : "secondary", onConfirm: async () => {
      const data = action === "delete" ? await api("delete_scheduled_task", item.account_id, item.id, item.revision) : await api("set_scheduled_task_enabled", item.account_id, item.id, !item.enabled, item.revision);
      await loadSchedules(); toast(data.message || (action === "delete" ? "任务已删除。" : "任务状态已更新。"));
    }});
  }

  async function showScheduledRuns(item) {
    const root = $("#scheduledTaskRuns");
    $("#scheduleListView").hidden = true; $("#scheduledTaskEditor").hidden = true; root.hidden = false;
    root.innerHTML = `<div class="schedule-runs-heading"><button class="button ghost small" id="scheduledRunsBack">返回任务列表</button><h2>${escapeHtml(item.name)} · 执行记录</h2><p>已提交微信表示发送请求已接受，不代表对方已读。所有时间为北京时间。</p></div><div id="scheduledRunsBody" class="loading-row">正在读取…</div>`;
    $("#scheduledRunsBack").addEventListener("click", showScheduledList);
    try {
      const data = await api("get_scheduled_task_runs", item.account_id, item.id);
      const body = $("#scheduledRunsBody");
      if (!body) return;
      body.className = "schedule-runs-body";
      body.innerHTML = data.items?.length ? data.items.map((run) => `<article class="schedule-run"><header><strong>${escapeHtml(scheduledTime(run.scheduled_at || run.started_at || run.created_at || run.time))}</strong><span>${escapeHtml(scheduledResultText(run))}</span></header>${run.message ? `<p>${escapeHtml(run.message)}</p>` : ""}<ul>${(run.deliveries || run.targets || run.results || []).map((result) => `<li><strong>${escapeHtml(result.name || result.target?.name || item.targets?.find((target) => target.id === result.id && target.type === result.type)?.name || "发送对象")}</strong><span>${escapeHtml(SCHEDULED_RESULTS[result.status] || result.status || "等待发送")}</span>${result.message || result.error ? `<small>${escapeHtml(result.message || result.error)}</small>` : ""}</li>`).join("")}</ul></article>`).join("") : '<div class="empty-state compact"><h3>尚无执行记录</h3><p>任务到时执行后，可在这里查看每个对象的发送结果。</p></div>';
    } catch (error) { if ($("#scheduledRunsBody")) $("#scheduledRunsBody").textContent = error.message; }
  }

  function renderSchedules() {
    $("#legacyScheduleSection").hidden = !state.schedules.length;
    $("#scheduleRows").innerHTML = state.schedules.length ? state.schedules.map((item) => `
      <tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.source)}</td><td>${escapeHtml(item.target)}</td><td>${escapeHtml(item.schedule_text)}</td><td>${escapeHtml(shortTime(item.next_run_time))}</td><td><span class="task-status ${item.enabled ? "running" : ""}">${item.status}</span></td><td><div class="row-actions"><button class="row-action" data-action="toggle" data-id="${escapeHtml(item.id)}">${item.enabled ? "暂停" : "开启"}</button><button class="row-action" data-action="edit" data-id="${escapeHtml(item.id)}">改时间</button><button class="row-action delete" data-action="delete" data-id="${escapeHtml(item.id)}">删除</button></div></td></tr>`).join("") : '<tr><td colspan="7" class="table-empty">当前没有业务定时任务</td></tr>';
    $$(".row-action", $("#scheduleRows")).forEach((button) => button.addEventListener("click", () => scheduleAction(button.dataset.action, button.dataset.id)));
  }

  async function scheduleAction(action, id) {
    const item = state.schedules.find((value) => value.id === id);
    if (!item) return;
    if (action === "toggle") {
      try { const result = await api("set_schedule_enabled", id, !item.enabled); await loadSchedules(); toast(result.message || (item.enabled ? "任务已暂停。" : "任务已开启。")); }
      catch (error) { reportError(error); }
      return;
    }
    if (action === "edit") {
      const parts = item.cron_expression.split(" ");
      const clock = parts.length === 5 && Number.isFinite(Number(parts[0])) && Number.isFinite(Number(parts[1])) ? `${String(parts[1]).padStart(2, "0")}:${String(parts[0]).padStart(2, "0")}` : "08:00";
      confirmAction({ title: "修改执行时间", message: `${item.name}当前安排为“${item.schedule_text}”。`, confirmText: "保存时间", field: {label: "每天执行时间", type: "time", value: clock}, onConfirm: async (value) => { const result = await api("update_schedule_time", id, value); await loadSchedules(); toast(result.message || "执行时间已更新。"); } });
      return;
    }
    confirmAction({ title: "删除定时任务？", message: `“${item.name}”删除后将停止执行，相关功能的其他设置不受影响。`, confirmText: "删除任务", onConfirm: async () => { await api("delete_schedule", id); await loadSchedules(); toast("定时任务已删除。 "); } });
  }

  async function loadActivity() {
    try {
      const [items, traces] = await Promise.all([
        api("get_activity"),
        api("get_response_traces").catch(() => []),
      ]);
      state.responseTraces = Array.isArray(traces) ? traces : (traces?.items || []);
      renderResponseTraces();
      $("#activityList").innerHTML = items.length ? items.map((item) => `<article class="activity-item ${escapeHtml(item.status)}"><time class="activity-time">${escapeHtml(fullTime(item.time))}</time><span class="activity-node" aria-hidden="true"></span><div class="activity-title"><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.status === "failed" ? "操作未完成，请按错误码处理" : "状态已记录")}</small></div><span class="activity-result ${item.status === "failed" ? "failed" : ""}"><i></i>${item.status === "failed" ? "已恢复" : "成功"}</span><span class="activity-code">${escapeHtml(item.code || "")}</span></article>`).join("") : '<div class="empty-state compact"><p>当前还没有运行记录</p></div>';
    } catch (error) { reportError(error); }
  }

  function renderResponseTraces() {
    const list = $("#responseTraceList");
    const traces = state.responseTraces || [];
    const latest = traces[0];
    const rawStages = Array.isArray(latest?.stages) ? latest.stages : [];
    const hasFailure = rawStages.some((stage) => stage.status === "failed");
    const hasPending = rawStages.some((stage) => stage.status === "pending");
    const badge = $("#traceStatus");
    badge.className = `status-badge ${hasFailure ? "error" : hasPending ? "warning" : traces.length ? "online" : "checking"}`;
    badge.innerHTML = `<span></span>${hasFailure ? "发送失败" : hasPending ? "正在发送" : traces.length ? "发送完成" : "等待消息"}`;
    if (!latest) {
      list.innerHTML = '<div class="empty-state compact"><p>收到下一条消息后，这里会按时间显示回复经过的步骤，不展示聊天正文和原始群标识。</p></div>';
      return;
    }

    const numericDuration = (stage) => {
      const raw = Number(stage.duration_seconds);
      if (Number.isFinite(raw)) return Math.max(0, raw);
      const text = String(stage.duration_text || "").trim();
      const match = text.match(/^(<)?\s*(\d+(?:\.\d+)?)\s*(毫秒|秒)$/);
      if (!match) return 0;
      const value = Number(match[2]) * (match[3] === "毫秒" ? .001 : 1);
      return match[1] ? value / 2 : value;
    };
    let fallbackOffset = 0;
    const stages = rawStages.map((stage) => {
      const duration = numericDuration(stage);
      const parsedOffset = Number(stage.offset_seconds);
      const offset = Number.isFinite(parsedOffset) ? Math.max(0, parsedOffset) : fallbackOffset;
      fallbackOffset = Math.max(fallbackOffset, offset + duration);
      return {...stage, duration, offset};
    });
    const reportedTotal = Number(latest.duration_seconds);
    const total = Math.max(
      Number.isFinite(reportedTotal) ? reportedTotal : 0,
      ...stages.map((stage) => stage.offset + stage.duration),
      0.1,
    );
    const conciseDuration = (seconds) => seconds <= 0
      ? "0 秒"
      : seconds < 0.1
      ? `${Math.max(1, Math.round(seconds * 1000))} 毫秒`
      : `${seconds.toFixed(seconds < 10 ? 1 : 0)} 秒`;
    const statusLabel = {success: "完成", skipped: "已跳过", pending: "发送中", failed: "失败"};
    const ticks = [0, .25, .5, .75, 1].map((ratio) => (
      `<span style="left:${ratio * 100}%">${escapeHtml(conciseDuration(total * ratio))}</span>`
    )).join("");
    const rows = stages.map((stage, index) => {
      const status = ["success", "skipped", "pending", "failed"].includes(stage.status) ? stage.status : "success";
      const start = Math.min(98.5, (stage.offset / total) * 100);
      const proportionalWidth = (stage.duration / total) * 100;
      const width = Math.max(1.5, Math.min(100 - start, proportionalWidth));
      const durationText = stage.duration_text || (stage.duration ? conciseDuration(stage.duration) : "<0.1 秒");
      const detail = stage.detail || statusLabel[status];
      const label = stage.label || stage.code || "处理阶段";
      return `<div class="gantt-row ${escapeHtml(status)}">
        <div class="gantt-label"><span>${index + 1}</span><div><strong>${escapeHtml(label)}</strong><small>${escapeHtml(detail)}</small></div></div>
        <div class="gantt-track"><span class="gantt-bar ${escapeHtml(status)}" style="left:${start.toFixed(3)}%;width:${width.toFixed(3)}%" role="img" aria-label="${escapeHtml(`${label}，${statusLabel[status]}，耗时 ${durationText}`)}"></span></div>
        <strong class="gantt-duration">${escapeHtml(durationText)}</strong>
      </div>`;
    }).join("");
    const resultLabel = hasFailure ? "未完成" : hasPending ? "发送处理中" : "已发送到微信";
    list.innerHTML = `<article class="trace-gantt-card">
      <header class="trace-summary">
        <div><span>最近一次发送</span><strong>${escapeHtml(latest.title || "机器人回复")}</strong><time>${escapeHtml(fullTime(latest.time))}</time></div>
        <dl><div><dt>总耗时</dt><dd>${escapeHtml(latest.duration_text || conciseDuration(total))}</dd></div><div><dt>结果</dt><dd>${escapeHtml(resultLabel)}</dd></div></dl>
      </header>
      <div class="gantt-legend" aria-label="阶段状态说明"><span class="success">完成</span><span class="skipped">已跳过</span><span class="pending">发送中</span><span class="failed">失败</span></div>
      <div class="gantt-chart" aria-label="机器人消息发送阶段甘特图">
        <div class="gantt-axis"><span>处理阶段</span><div>${ticks}</div><span>耗时</span></div>
        ${rows || '<div class="empty-state compact">等待处理详情</div>'}
      </div>
      <footer>横条位置表示阶段发生时间，长度表示耗时；仅展示脱敏后的处理步骤。</footer>
    </article>`;
  }

  let modalResolve = null;
  function confirmAction({title, message, confirmText = "确认", cancelText = "取消", pendingText = "处理中…", pendingMessage = "", field = null, intent = "danger", onConfirm}) {
    const backdrop = $("#modalBackdrop");
    const cancel = $("#modalCancel");
    const confirm = $("#modalConfirm");
    $("#modalTitle").textContent = title;
    $("#modalMessage").textContent = message;
    confirm.textContent = confirmText;
    cancel.textContent = cancelText;
    confirm.className = `button ${["primary", "secondary", "danger"].includes(intent) ? intent : "danger"}`;
    const fieldControl = field?.type === "select"
      ? `<select id="modalInput">${(field.options || []).map((option) => `<option value="${escapeHtml(option.value)}" ${String(option.value) === String(field.value ?? "") ? "selected" : ""}>${escapeHtml(option.label)}</option>`).join("")}</select>`
      : (field ? `<input id="modalInput" type="${escapeHtml(field.type || "text")}" value="${escapeHtml(field.value || "")}">` : "");
    $("#modalField").innerHTML = field ? `<label class="modal-field"><span>${escapeHtml(field.label)}</span>${fieldControl}</label>` : "";
    $("#modalProgress").hidden = true;
    $("#modalProgressBar").style.width = "0%";
    $("#modalError").hidden = true;
    $("#modalError").textContent = "";
    backdrop.dataset.busy = "false";
    backdrop.hidden = false;
    cancel.disabled = false;
    const restoreFocus = document.activeElement;
    const shell = $(".app-shell");
    if (shell) shell.inert = true;
    cancel.focus();
    const close = () => { backdrop.hidden = true; if (shell) shell.inert = false; restoreFocus?.focus(); };
    modalResolve = async (confirmed) => {
      if (!confirmed) {
        if (backdrop.dataset.busy === "true") return;
        close();
        return;
      }
      backdrop.dataset.busy = "true";
      confirm.disabled = true;
      confirm.textContent = pendingText;
      cancel.disabled = true;
      $("#modalError").hidden = true;
      if (pendingMessage) $("#modalMessage").textContent = pendingMessage;
      try {
        await onConfirm(field ? $("#modalInput").value : undefined);
        close();
      } catch (error) {
        const product = error?.product;
        const detail = product
          ? `${product.message || product.title || "操作未完成"}${product.code ? `（错误码 ${product.code}）` : ""}`
          : (error?.message || "操作未完成，请稍后重试。 ");
        $("#modalMessage").textContent = message;
        $("#modalError").textContent = detail;
        $("#modalError").hidden = false;
        reportError(error);
      } finally {
        backdrop.dataset.busy = "false";
        confirm.disabled = false;
        confirm.textContent = confirmText;
        cancel.disabled = false;
      }
    };
  }

  function requiredValues(form) {
    const missing = $$('[required]', form).find((input) => !input.value.trim());
    if (missing) { missing.focus(); toast("请填写所有必填项。 ", "error"); return false; }
    return true;
  }

  async function saveProvider(event) {
    event.preventDefault();
    const form = event.currentTarget;
    if (!requiredValues(form)) return;
    const button = $('button[type="submit"]', form);
    const originalText = button.textContent;
    button.disabled = true;
    button.textContent = "正在保存并应用…";
    $("#providerNote").textContent = "正在更新统一模型配置…";
    try {
      const values = Object.fromEntries(new FormData(form));
      values.thinking_enabled = form.elements.thinking_enabled.checked;
      const data = await api("save_provider", values);
      state.bootstrap.providers = await api("get_providers");
      state.bootstrap.provider = state.bootstrap.providers.find((item) => item.is_default) || data;
      state.featureDetails = {};
      coreSettings.invalidate();
      $("#apiKeyHint").textContent = "已安全保存；留空不会覆盖";
      form.elements.api_key.value = "";
      renderProviderServices();
      markProviderClean();
      showProviderOverview(true);
      toast(data.is_default
        ? (data.apply_state === "applied"
          ? "模型服务已保存，并已设为当前回复模型。"
          : "模型服务已保存，机器人启动时会使用它。")
        : "模型服务已保存，当前回复模型保持不变。");
    } catch (error) { reportError(error); $("#providerNote").textContent = error.message; }
    finally { button.disabled = false; button.textContent = originalText; }
  }

  function providerPresetFor(service) {
    if (!service) return "free_grok";
    if (service.is_recommended) return "free_grok";
    if (String(service.model || "").toLowerCase().startsWith("deepseek")) return "deepseek";
    return "custom";
  }

  function providerCardIdentity(service) {
    const preset = providerPresetFor(service);
    if (preset === "free_grok") return {mark: "X", label: "xAI · Grok", className: "grok"};
    if (preset === "deepseek") return {mark: "D", label: "DeepSeek", className: "deepseek"};
    return {mark: "AI", label: "兼容服务", className: "custom"};
  }

  function providerThinkingSupported(apiBase, model) {
    try {
      const endpoint = new URL(String(apiBase || ""));
      const host = endpoint.hostname.toLowerCase();
      const name = String(model || "").trim().toLowerCase();
      const leaf = name.split("/").pop();
      const on = (...domains) => domains.some((domain) => host === domain || host.endsWith(`.${domain}`));
      const deepseek = !/(reasoner|speciale|-r1|r1-)/.test(leaf) && /^(deepseek-chat|deepseek-flash|deepseek-v3|deepseek-v4)/.test(leaf);
      const qwen = /^(qwen3|qwen-3)/.test(leaf) && !/(thinking|qwq)/.test(leaf);
      const glm = /^glm-(?:4\.(?:5|6|7)|5(?:[.\-]|$))/.test(leaf);
      const kimi = /^(kimi-k2\.5|kimi-k2\.6)/.test(leaf);
      const openai = /^gpt-5\.(?:[1-9]|\d{2,})/.test(leaf) && !leaf.includes("pro");
      const claude = /^claude-(?:3\.7|(?:opus|sonnet|haiku)-4(?:[.\-]|$))/.test(leaf);
      const ollama = endpoint.port === "11434" || ["localhost", "127.0.0.1", "[::1]", "::1"].includes(host);
      return (on("xiaomimimo.com") && ["mimo-v2.5", "mimo-v2.5-pro"].includes(leaf))
        || (on("deepseek.com") && deepseek)
        || (on("dashscope.aliyuncs.com") && !leaf.includes("thinking") && (/^qwen-plus/.test(leaf) || qwen || deepseek || kimi || glm))
        || (on("volces.com") && /^(doubao-seed-1-6|doubao-seed-2)/.test(leaf))
        || (on("bigmodel.cn", "z.ai") && glm)
        || (on("siliconflow.cn", "siliconflow.com") && (qwen || deepseek || glm || /^hunyuan-a13b/.test(leaf)))
        || (on("moonshot.cn", "moonshot.ai") && kimi)
        || (on("openrouter.ai") && (qwen || deepseek || glm || kimi || openai || /^(gemini-2\.5-flash|gemini-2\.5-flash-lite)/.test(leaf) || claude))
        || (on("together.xyz", "together.ai") && /^(qwen3\.5|qwen-3\.5|deepseek-v4)/.test(leaf))
        || (on("fireworks.ai") && (qwen || deepseek || glm))
        || (on("groq.com") && /^(qwen3\.6-27b|qwen3\.8-27b)/.test(leaf))
        || (on("cerebras.ai") && leaf === "zai-glm-4.7")
        || (ollama && !leaf.includes("gpt-oss") && /^(qwen3|qwen-3|deepseek-v3\.1|deepseek-r1|glm-4\.7)/.test(leaf))
        || (on("x.ai", "xiuxianjyj.xin") && /^(grok-4\.5|grok-4\.6)/.test(leaf))
        || (on("googleapis.com") && /^(gemini-2\.5-flash|gemini-2\.5-flash-lite)/.test(leaf))
        || (on("openai.com", "openai.azure.com", "services.ai.azure.com", "models.inference.ai.azure.com") && openai);
    } catch (_) { return false; }
  }

  function syncProviderThinkingControl(initialValue = null) {
    const form = $("#providerForm");
    const input = form.elements.thinking_enabled;
    if (initialValue !== null) input.checked = Boolean(initialValue);
    input.disabled = false;
    $("#providerThinkingHint").textContent = "关闭后普通聊天通常更快；模型不支持切换时会自动忽略，不影响正常调用。";
    $("#providerThinkingValue").textContent = input.checked ? "已开启" : "已关闭";
  }

  function applyProviderPreset(presetId) {
    const form = $("#providerForm");
    const preset = (state.bootstrap?.provider_presets || []).find((item) => item.id === presetId);
    if (!preset) return;
    form.elements.api_base.value = preset.api_base || "";
    form.elements.model.value = preset.model || "";
    clearProviderModels();
    $("#providerModelHint").textContent = "可手动填写，也可从服务端读取后选择";
    $("#providerPresetHint").textContent = preset.description || "填写模型服务信息";
    syncProviderThinkingControl();
  }

  function renderProviderSaveIntent(makeDefault) {
    $("#providerSaveIntentTitle").textContent = makeDefault ? "保存后直接设为回复模型" : "保存为备用模型服务";
    $("#providerSaveIntentDetail").textContent = makeDefault
      ? "首个模型服务会自动用于机器人回复。"
      : "不会更改当前回复模型；保存后可在服务列表中手动切换。";
    $("#saveProvider").textContent = makeDefault ? "保存并设为回复模型" : "保存模型服务";
  }

  function beginNewProvider(presetId = "free_grok") {
    const form = $("#providerForm");
    form.reset();
    form.elements.provider_id.value = "";
    form.elements.preset.value = presetId;
    applyProviderPreset(presetId);
    $("#providerEditorEyebrow").textContent = "新建服务";
    $("#providerEditorTitle").textContent = "新建模型服务";
    $("#providerEditorSubtitle").textContent = presetId === "deepseek" ? "添加到当前设备，不影响其他安装。" : "选择一种服务，然后确认连接信息。";
    $("#apiKeyHint").textContent = "密钥只保存在本机，不会回显";
    $("#testProvider").hidden = true;
    $("#deleteProvider").hidden = true;
    $("#providerNote").textContent = "";
    renderProviderSaveIntent(!(state.bootstrap?.providers || []).length);
    markProviderClean("尚未保存");
    showProviderEditor();
  }

  function editProvider(providerId) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service) { beginNewProvider(); return; }
    const form = $("#providerForm");
    form.elements.provider_id.value = service.id;
    form.elements.preset.value = providerPresetFor(service);
    form.elements.api_base.value = service.api_base || "";
    form.elements.model.value = service.model || "";
    form.elements.api_key.value = "";
    syncProviderThinkingControl(service.thinking_enabled !== false);
    clearProviderModels();
    $("#providerModelHint").textContent = "可手动填写，也可从服务端读取后选择";
    $("#providerEditorEyebrow").textContent = "编辑服务";
    $("#providerEditorTitle").textContent = service.name;
    $("#providerEditorSubtitle").textContent = service.is_default ? "当前回复模型" : "已配置，可切换为回复模型";
    $("#providerPresetHint").textContent = service.is_recommended ? "面向普通用户的默认回复服务" : "此服务仅保存在当前设备";
    $("#apiKeyHint").textContent = service.api_key_configured ? "已安全保存；留空不会覆盖" : "尚未配置服务密钥";
    $("#testProvider").hidden = !service.configured;
    $("#deleteProvider").hidden = false;
    $("#providerNote").textContent = "";
    renderProviderSaveIntent(Boolean(service.is_default));
    markProviderClean("已保存");
    showProviderEditor();
  }

  function renderProviderServices() {
    const items = state.bootstrap?.providers || [];
    const displayItems = [...items].sort((left, right) => Number(right.is_default) - Number(left.is_default));
    const status = $("#providerStatus");
    const defaultProvider = items.find((item) => item.is_default);
    status.className = `status-badge ${defaultProvider ? "success" : "checking"}`;
    status.textContent = defaultProvider ? `${items.length} 项服务` : "0 项服务";
    $("#providerServiceList").innerHTML = items.length ? `
      <div class="provider-list-toolbar"><div><strong>${escapeHtml(defaultProvider ? `${defaultProvider.name} 正在回复` : "请选择回复模型")}</strong><span>机器人和相关功能共用这些配置，服务密钥仅保存在当前设备。</span></div></div>
      <div class="provider-service-cards">${displayItems.map((item) => {
        const identity = providerCardIdentity(item);
        const statusText = item.is_default ? "使用中" : item.configured ? "备用" : "待完善";
        const description = item.is_default ? "当前回复模型" : item.configured ? "可供相关功能选择" : "补充服务密钥后即可使用";
        return `
        <article class="provider-service ${item.is_default ? "active" : ""}">
          <header class="provider-card-header">
            <div class="provider-card-title">
              <span class="provider-logo ${identity.className}" aria-hidden="true">${identity.mark}</span>
              <div class="provider-service-copy"><strong>${escapeHtml(item.name)}</strong><span>${escapeHtml(identity.label)}</span></div>
            </div>
            <div class="provider-card-status-row">
              <span class="provider-card-status ${item.is_default ? "active" : item.configured ? "ready" : "incomplete"}">${statusText}</span>
              <details class="provider-card-menu">
                <summary aria-label="打开 ${escapeHtml(item.name)} 操作菜单" title="更多操作">${icon("i-more")}</summary>
                <div class="provider-card-menu-popover" role="menu">
                  <button type="button" role="menuitem" data-action-bound disabled data-provider-edit="${escapeHtml(item.id)}">编辑服务</button>
                  ${item.is_default ? "" : `<button type="button" role="menuitem" data-action-bound disabled data-provider-default="${escapeHtml(item.id)}">设为回复模型</button>`}
                  <button class="danger" type="button" role="menuitem" data-action-bound disabled data-provider-delete="${escapeHtml(item.id)}">删除服务</button>
                </div>
              </details>
            </div>
          </header>
          <div class="provider-service-body">
            <div class="provider-card-tags">
              ${item.is_recommended ? '<span>免费</span>' : '<span>自定义</span>'}
              <span>${item.configured ? "配置已统一共享" : "缺少服务密钥"}</span>
            </div>
            <p>${escapeHtml(description)}</p>
            <div class="provider-thinking-control">
              <span><strong>思考模式</strong><small data-provider-thinking-status>${item.thinking_enabled ? "已开启 · 模型支持时生效" : "已关闭 · 模型支持时生效"}</small></span>
              <button class="provider-thinking-switch" type="button" role="switch" aria-label="${escapeHtml(item.name)}思考模式" aria-checked="${Boolean(item.thinking_enabled)}" data-action-bound disabled data-provider-thinking="${escapeHtml(item.id)}"><span aria-hidden="true"></span></button>
            </div>
          </div>
          <footer class="provider-service-actions">
            <button class="provider-card-action" type="button" data-action-bound disabled data-provider-edit="${escapeHtml(item.id)}">编辑</button>
            ${item.is_default ? '<span class="provider-card-current">正在用于回复</span>' : `<button class="provider-card-action primary" type="button" data-action-bound disabled data-provider-default="${escapeHtml(item.id)}">切换使用</button>`}
            <button class="provider-card-action danger" type="button" data-action-bound disabled data-provider-delete="${escapeHtml(item.id)}">删除</button>
          </footer>
        </article>`;
      }).join("")}
        <button class="provider-add-card" type="button" data-action-bound disabled data-provider-create>
          <span class="provider-add-icon" aria-hidden="true">${icon("i-plus")}</span>
          <strong>添加模型服务</strong>
          <small>连接其他兼容模型</small>
        </button>
      </div>` : `
      <div class="provider-empty-state">
        <div class="provider-empty-visual" aria-hidden="true"><span></span><i></i><span></span></div>
        <span class="eyebrow">从这里开始</span>
        <h3>还没有模型服务</h3>
        <p>新建一个服务后，机器人才能生成回复。第一次使用建议选择免费 Grok 4.6。</p>
        <button class="button primary" type="button" data-action-bound disabled data-provider-create>新建模型服务</button>
        <div class="provider-empty-assurances"><span>密钥仅存本机</span><span>保存后再启用</span><span>可随时更换</span></div>
      </div>`;
    $$('[data-action-bound]', $("#providerServiceList")).forEach((button) => { button.disabled = false; });
  }

  async function setProviderDefault(providerId, button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "切换中…";
    try {
      const result = await api("set_default_provider", providerId);
      state.bootstrap.providers = await api("get_providers");
      state.featureDetails = {};
      coreSettings.invalidate();
      renderProviderServices();
      toast(result.apply_state === "applied" ? "回复模型已切换并立即生效。" : "回复模型已切换，将在启动时生效。");
    } catch (error) { reportError(error); }
    finally { button.disabled = false; button.textContent = original; }
  }

  async function testSelectedProvider() {
    const providerId = $("#providerForm").elements.provider_id.value;
    if (!providerId) return;
    const button = $("#testProvider");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "测试中…";
    try { await api("test_provider", providerId); $("#providerNote").textContent = "连接正常，可以使用。"; }
    catch (error) { reportError(error); $("#providerNote").textContent = error.message; }
    finally { button.disabled = false; button.textContent = original; }
  }

  async function discoverProviderModels() {
    const form = $("#providerForm");
    const button = $("#discoverProviderModels");
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "读取中…";
    $("#providerModelHint").textContent = "正在验证地址和密钥，并读取模型列表…";
    try {
      const data = await api("discover_provider_models", Object.fromEntries(new FormData(form)));
      const items = Array.isArray(data.items) ? data.items : [];
      state.providerModels = [...new Set(items.map((model) => String(model).trim()).filter(Boolean))];
      state.providerModelFiltering = false;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
      $("#providerModelHint").textContent = `已读取 ${state.providerModels.length} 个模型；可从下方完整列表选择，也可以继续手动填写。`;
    } catch (error) {
      reportError(error);
      $("#providerModelHint").textContent = error.message;
    } finally {
      button.disabled = false;
      button.textContent = original;
    }
  }

  function clearProviderModels() {
    state.providerModels = [];
    state.providerModelFiltering = false;
    state.providerModelFocus = -1;
    const options = $("#providerModelOptions");
    const input = $("#providerModelInput");
    if (options) {
      options.innerHTML = "";
      options.hidden = true;
    }
    $("#providerModelCombobox")?.classList.remove("open-above");
    input?.setAttribute("aria-expanded", "false");
  }

  function providerModelMatches() {
    const keyword = state.providerModelFiltering
      ? $("#providerModelInput")?.value.trim().toLocaleLowerCase("zh-CN") || ""
      : "";
    return state.providerModels.filter((model) => !keyword || model.toLocaleLowerCase("zh-CN").includes(keyword));
  }

  function renderProviderModelOptions(open = false) {
    const input = $("#providerModelInput");
    const options = $("#providerModelOptions");
    const combobox = $("#providerModelCombobox");
    if (!input || !options) return;
    const wasExpanded = input.getAttribute("aria-expanded") === "true";
    const items = providerModelMatches();
    state.providerModelFocus = Math.min(state.providerModelFocus, items.length - 1);
    options.innerHTML = items.length ? items.map((model, index) => `
      <button type="button" role="option" aria-selected="${input.value === model}" class="model-option ${state.providerModelFocus === index ? "focused" : ""}" data-model="${escapeHtml(model)}">${escapeHtml(model)}</button>
    `).join("") : '<span class="model-option-empty">没有匹配的模型，可继续手动填写</span>';
    const expanded = open && state.providerModels.length > 0;
    options.hidden = !expanded;
    input.setAttribute("aria-expanded", String(expanded));
    if (!expanded) {
      combobox?.classList.remove("open-above");
    } else if (!wasExpanded && combobox) {
      const triggerRect = input.getBoundingClientRect();
      const scrollOwnerRect = input.closest(".content-scroll")?.getBoundingClientRect();
      const boundaryTop = Math.max(0, scrollOwnerRect?.top ?? 0);
      const boundaryBottom = Math.min(window.innerHeight, scrollOwnerRect?.bottom ?? window.innerHeight);
      const listHeight = Math.min(options.scrollHeight, 252);
      const spaceAbove = triggerRect.top - boundaryTop - 6;
      const spaceBelow = boundaryBottom - triggerRect.bottom - 6;
      combobox.classList.toggle("open-above", spaceBelow < listHeight && spaceAbove > spaceBelow);
    }
    if (expanded && state.providerModelFocus >= 0) {
      $(".model-option.focused", options)?.scrollIntoView({block: "nearest"});
    }
  }

  function chooseProviderModel(model) {
    const input = $("#providerModelInput");
    if (!input) return;
    input.value = model;
    input.dispatchEvent(new Event("input", {bubbles: true}));
    state.providerModelFiltering = false;
    state.providerModelFocus = -1;
    renderProviderModelOptions(false);
    input.focus();
  }

  function bindProviderModelCombobox() {
    const input = $("#providerModelInput");
    const options = $("#providerModelOptions");
    if (!input || !options) return;
    input.addEventListener("focus", () => {
      state.providerModelFiltering = false;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
    });
    input.addEventListener("click", () => {
      if (!options.hidden) return;
      state.providerModelFiltering = false;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
    });
    input.addEventListener("input", () => {
      state.providerModelFiltering = true;
      state.providerModelFocus = -1;
      renderProviderModelOptions(true);
    });
    input.addEventListener("keydown", (event) => {
      if (event.isComposing) return;
      if (!["ArrowDown", "ArrowUp", "Enter", "Escape"].includes(event.key)) return;
      if (event.key === "Escape") {
        renderProviderModelOptions(false);
        return;
      }
      const items = providerModelMatches();
      if (!items.length) return;
      if (event.key === "Enter") {
        if (state.providerModelFocus < 0) return;
        event.preventDefault();
        chooseProviderModel(items[state.providerModelFocus]);
        return;
      }
      event.preventDefault();
      const direction = event.key === "ArrowDown" ? 1 : -1;
      state.providerModelFocus = (state.providerModelFocus + direction + items.length) % items.length;
      renderProviderModelOptions(true);
    });
    options.addEventListener("mousedown", (event) => event.preventDefault());
    options.addEventListener("click", (event) => {
      const option = event.target.closest("[data-model]");
      if (option) chooseProviderModel(option.dataset.model);
    });
    document.addEventListener("click", (event) => {
      if (!event.target.closest("#providerModelCombobox")) renderProviderModelOptions(false);
    });
  }

  function deleteProvider(providerId) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service) return;
    const alternatives = (state.bootstrap?.providers || []).filter((item) => item.id !== providerId && item.configured);
    const references = Array.isArray(service.references) ? service.references : [];
    const needsReplacement = service.is_default || references.length > 0;
    if (needsReplacement && !alternatives.length) {
      toast("当前模型仍在被机器人或功能使用。请先添加另一个可用模型，再删除它。", "error");
      return;
    }
    const referenceCopy = references.length
      ? `使用它的功能：${references.join("、")}。删除前会自动切换到你选择的模型。`
      : "";
    confirmAction({
      title: `删除 ${service.name}？`,
      message: referenceCopy || (service.is_default
        ? "删除前会先把机器人回复切换到你选择的模型；功能和聊天记录不会受影响。"
        : "仅删除这项模型服务，不会删除功能或聊天记录。"),
      confirmText: "删除模型服务",
      field: needsReplacement ? {
        label: references.length ? "接替机器人及相关功能的模型" : "接替机器人回复的模型",
        type: "select",
        value: alternatives[0]?.id || "",
        options: alternatives.map((item) => ({value: item.id, label: item.name})),
      } : null,
      onConfirm: async (replacementId) => {
        await api("delete_provider", providerId, replacementId || "");
        state.bootstrap.providers = await api("get_providers");
        state.featureDetails = {};
        coreSettings.invalidate();
        renderProviderServices();
        markProviderClean();
        showProviderOverview(true);
        toast("模型服务已删除。");
      },
    });
  }

  async function setProviderThinking(providerId, button) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service || button.disabled) return;
    button.disabled = true;
    const status = button.closest(".provider-thinking-control")?.querySelector("[data-provider-thinking-status]");
    if (status) status.textContent = "正在应用…";
    try {
      await api("set_provider_thinking", providerId, !service.thinking_enabled);
      state.bootstrap.providers = await api("get_providers");
      renderProviderServices();
    } catch (error) {
      reportError(error);
      renderProviderServices();
    }
  }

  function deleteSelectedProvider() {
    deleteProvider($("#providerForm").elements.provider_id.value);
  }

  function applyBrand(brand) {
    $("#brandName").textContent = brand.display_name;
    const mark = $("#brandMark");
    if (mark && !mark.querySelector("img")) mark.innerHTML = '<img src="assets/app-icon.png" alt="">';
    document.title = `${brand.display_name} · 机器人管理`;
  }

  function updateToggleCopy(event) {
    const input = event.currentTarget;
    const label = input.closest(".toggle")?.querySelector("span:last-child");
    if (!label) return;
    if (input.name === "scheduled_enabled") label.textContent = input.checked ? "已允许" : "已暂停";
    else if (input.name === "bot_enabled") label.textContent = input.checked ? "已启用" : "已停用";
    else label.textContent = input.checked ? "已启用" : "已关闭";
  }

  function fillExperience(experience) {
    state.experience = experience || {};
    const capabilities = state.experience.capabilities || {};
    const memory = state.experience.memory || {};
    const readiness = state.experience.readiness || {};
    const memoryForm = $("#memoryForm");
    memoryForm.elements.enabled.checked = Boolean(memory.enabled);
    memoryForm.elements.recall_mode.value = memory.recall_mode || "smart";
    memoryForm.elements.casual_query_max_chars.value = memory.casual_query_max_chars || 24;
    memoryForm.elements.top_k.value = memory.top_k || 3;
    memoryForm.elements.summary_trigger_rounds.value = memory.summary_trigger_rounds || 10;
    $("#memoryExplanation").textContent = state.experience.memory_explanation || "明确问起上次内容时优先回忆，普通寒暄不查询。";
    $("#memoryStatus").textContent = capabilities.memory
      ? (readiness.memory ? "主记忆 · 保存后热更新" : "主记忆 · 还需文本向量模型")
      : "需要完整更新";
    memoryForm.querySelector("button[type='submit']").disabled = !capabilities.memory;

    const emotion = state.experience.emotion || {};
    const emotionForm = $("#emotionForm");
    for (const key of ["enabled", "daily_mood", "include_affection", "include_mood"]) emotionForm.elements[key].checked = Boolean(emotion[key]);
    $("#emotionStatus").textContent = capabilities.emotion ? "精简上下文" : "需要完整更新";
    emotionForm.querySelector("button[type='submit']").disabled = !capabilities.emotion;
    $$(".toggle input", $("#page-settings")).forEach((input) => {
      input.removeEventListener("change", updateToggleCopy);
      input.addEventListener("change", updateToggleCopy);
    });
  }

  function renderWebSearch(settings) {
    state.webSearch = settings || {enabled: false, provider: "tavily", link_enabled: false, providers: {}};
    state.webSearchDirty = false;
    state.webSearchClearKey = false;
    const form = $("#webSearchForm");
    if (!form) return;
    const providerSelect = form.elements.provider;
    const providers = state.webSearch.providers || {};
    providerSelect.innerHTML = Object.values(providers).map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(item.name)}${item.configured ? " · 已配置" : ""}</option>`).join("");
    const requestedProvider = state.webSearch.provider || "tavily";
    providerSelect.value = providers[requestedProvider] ? requestedProvider : (Object.keys(providers)[0] || "");
    form.elements.enabled.checked = Boolean(state.webSearch.enabled);
    form.elements.link_enabled.value = String(Boolean(state.webSearch.link_enabled));
    updateToggleCopy({currentTarget: form.elements.enabled});
    updateWebSearchProviderCopy();
    $("#webSearchStatus").textContent = state.webSearch.enabled ? "已启用" : "未启用";
    $("#webSearchStatus").classList.toggle("online", Boolean(state.webSearch.enabled));
    $("#webSearchNote").textContent = state.webSearch.enabled
      ? "服务商已选择；保存后会在需要时调用。"
      : "联网搜索当前关闭。保存后机器人不会发起搜索请求。";
  }

  function updateWebSearchProviderCopy() {
    const form = $("#webSearchForm");
    if (!form || !state.webSearch) return;
    const id = form.elements.provider.value;
    const item = state.webSearch.providers?.[id] || {};
    $("#webSearchProviderHint").textContent = item.description || "选择后使用对应服务商的密钥。";
    $("#webSearchKeyHint").textContent = item.configured
      ? `当前服务商已保存 ${item.key_count || 1} 个密钥；输入新密钥会替换当前服务商密钥。`
      : "当前服务商尚未配置密钥；密钥仅保存在本机。";
    $("#webSearchProviderSummary").innerHTML = `<div class="web-search-provider-summary-copy"><span class="eyebrow">当前服务商</span><strong>${escapeHtml(item.name || "未选择")}</strong><small>${escapeHtml(item.description || "请先选择一个搜索服务商。")}</small></div><span class="status-badge ${item.configured ? "online" : ""}">${item.configured ? `已配置 ${item.key_count || 1} 个密钥` : "尚未配置"}</span>`;
  }

  async function loadWebSearchSettings() {
    try {
      renderWebSearch(await api("get_web_search_settings"));
    } catch (error) {
      $("#webSearchNote").textContent = error.message || "联网搜索设置读取失败。";
      reportError(error);
    }
  }

  async function saveWebSearchSettings(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector("button[type='submit']");
    const note = $("#webSearchNote");
    button.disabled = true;
    button.textContent = "正在保存…";
    note.textContent = "正在保存并确认联网搜索设置…";
    try {
      const values = {
        enabled: form.elements.enabled.checked,
        provider: form.elements.provider.value,
        link_enabled: form.elements.link_enabled.value === "true",
        key: form.elements.key.value.trim(),
        clear_key: Boolean(state.webSearchClearKey),
      };
      const saved = await api("save_web_search_settings", values);
      renderWebSearch(saved);
      note.textContent = saved.apply_state === "applied" ? "已热更新，下一条消息使用新配置。" : "已保存，机器人下次启动时使用。";
      toast(note.textContent);
    } catch (error) {
      note.textContent = error.message || "联网搜索设置保存失败。";
      reportError(error);
    } finally {
      button.disabled = false;
      button.textContent = "保存联网搜索";
    }
  }

  async function saveExperience(event, save, section, noteSelector) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type='submit']", form);
    const note = $(noteSelector);
    button.disabled = true;
    button.textContent = "正在保存…";
    note.textContent = "正在更新机器人设置…";
    try {
      const values = Object.fromEntries(new FormData(form));
      $$("input[type='checkbox']", form).forEach((input) => { values[input.name] = input.checked; });
      for (const key of ["top_k", "summary_trigger_rounds", "casual_query_max_chars"]) if (key in values) values[key] = Number(values[key]);
      const saved = await save(values);
      state.experience[section] = saved;
      note.textContent = saved.apply_state === "applied" ? "已热更新，下一条消息生效。" : "已保存，机器人下次启动时生效。";
    } catch (error) {
      note.textContent = error.message || "保存失败";
      reportError(error);
    } finally {
      button.disabled = false;
      button.textContent = section === "memory" ? "保存记忆设置" : "保存情感设置";
    }
  }

  const saveMemoryExperience = (event) => saveExperience(event, (values) => api("save_memory_settings", values), "memory", "#memoryNote");
  const saveEmotionExperience = (event) => saveExperience(event, (values) => api("save_emotion_settings", values), "emotion", "#emotionNote");

  async function openMemoryManager() {
    try {
      if (!state.featuresLoaded) await loadFeatures();
      const memory = state.features.find((item) => item.display_name === "长期记忆");
      if (!memory) throw new Error("长期记忆管理页尚未安装，请先安装完整软件更新。 ");
      await api("open_feature_center", memory.id);
    } catch (error) { reportError(error); }
  }

  function formatBytes(value) {
    const size = Number(value) || 0;
    if (size < 1024) return `${size} B`;
    if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
    if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
    return `${(size / 1024 ** 3).toFixed(1)} GB`;
  }

  async function loadMaintenance() {
    try {
      state.maintenance = await api("get_maintenance");
      const preview = state.maintenance.preview || {};
      const backups = state.maintenance.backups || [];
      const summary = state.maintenance.diagnostics?.summary || {};
      $("#backupSummary").textContent = `${preview.file_count || 0} 个设置与记忆文件，约 ${formatBytes(preview.uncompressed_size)}；${preview.can_create ? "现在可以备份" : "请先停止机器人"}`;
      $("#createBackup").disabled = !preview.can_create;
      $("#restoreSummary").textContent = backups.length ? `${backups.length} 个可恢复备份，最近一次 ${fullTime(backups[0].modified_at)}` : "尚无可恢复备份";
      $("#restoreBackup").disabled = !backups.length || !preview.can_create;
      $("#diagnosticSummary").textContent = summary.error_count ? `${summary.error_count} 项需要处理，导出内容已自动脱敏` : `已检查 ${summary.check_count || 0} 项，不包含密钥和聊天正文`;
    } catch (error) {
      $("#backupSummary").textContent = "维护信息暂时无法读取";
      reportError(error);
    }
  }

  function createBackup() {
    confirmAction({
      title: "创建设置与记忆备份？",
      message: "备份包含机器人设置、人格、情感数据和长期记忆，请像保管账号密码一样妥善保管。",
      confirmText: "创建备份",
      pendingText: "正在备份…",
      onConfirm: async () => { await api("create_backup"); state.maintenance = null; await loadMaintenance(); },
    });
  }

  function chooseRestoreBackup() {
    const backups = state.maintenance?.backups || [];
    if (!backups.length) { toast("当前没有可恢复的备份。", "error"); return; }
    confirmAction({
      title: "恢复设置与记忆？",
      message: "恢复会覆盖备份中包含的设置和记忆，并先自动保留恢复前副本。机器人必须保持停止。",
      confirmText: "校验并恢复",
      pendingText: "正在校验…",
      field: {label: "选择备份", type: "select", value: backups[0].id, options: backups.map((item) => ({value: item.id, label: `${fullTime(item.modified_at)} · ${formatBytes(item.size)}`}))},
      onConfirm: async (backupId) => { await api("validate_restore", backupId); await api("restore_backup", backupId); state.maintenance = null; await loadMaintenance(); },
    });
  }

  async function exportDiagnostics() {
    try {
      const result = await api("export_diagnostics");
      toast(`安全诊断摘要已导出：${result.file_name || "诊断文件"}`);
      await api("get_diagnostics");
    } catch (error) { reportError(error); }
  }

  async function closeOnboarding(goToSettings) {
    try {
      await api("complete_onboarding");
      state.bootstrap.experience.onboarding_complete = true;
      $("#onboardingBackdrop").hidden = true;
      $(".app-shell").inert = false;
      if (goToSettings) await navigate("settings");
      else $("#primaryAction").focus();
    } catch (error) { reportError(error); }
  }

  function showOnboarding(force = false) {
    if (!force && state.bootstrap?.experience?.onboarding_complete) return;
    const backdrop = $("#onboardingBackdrop");
    backdrop.hidden = false;
    $(".app-shell").inert = true;
    $("#onboardingRecommended").focus();
  }

  function fillSettings(data) {
    applyBrand(data.brand);
    fillExperience(data.experience);
    renderProviderServices();
    showProviderOverview();
    $("#versionText").textContent = `v${data.version}`;
    $("#settingsVersion").textContent = `v${data.version}`;
    $("#installPath").textContent = data.install_root;
    const update = data.update || {};
    $("#updateStatus").textContent = update.enabled ? "稳定通道" : "在线更新未启用";
    $("#checkUpdate").disabled = !update.enabled;
    if (update.result) {
      const succeeded = update.result.ok === true || update.result.status === "success";
      const failureDetail = String(update.result.message || "").trim();
      window.setTimeout(() => toast(
        succeeded
          ? `已更新到 v${update.result.version || data.version}`
          : failureDetail
            ? `上次更新未能完成：${failureDetail}`
            : "上次更新未能完成，软件已恢复到原版本。",
        succeeded ? "success" : "error",
      ), 350);
    }
  }

  async function refreshUpdate(manual = true) {
    if (state.updateChecking) return;
    state.updateChecking = true;
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
        const packageSize = formatBytes(release.package_size);
        confirmAction({
          title: `发现新版本 v${release.version}`,
          message: `${release.title || "新版本已就绪"}${release.notes?.length ? `：${release.notes.join("；")}` : ""}。更新包约 ${packageSize}，安装前请先停止机器人；更新失败会自动恢复原版本。`,
          confirmText: "下载并安装",
          cancelText: "稍后提醒",
          intent: "primary",
          pendingText: "正在下载…",
          pendingMessage: `正在下载并校验 v${release.version}，完成后软件会自动重启。请勿关闭软件。`,
          onConfirm: async () => {
            const accepted = await api("install_update", release.version);
            if (!accepted?.accepted) toast("更新任务已经在处理中。 ");
            $("#modalProgress").hidden = false;
            while (true) {
              const progress = await api("get_update_status");
              const percent = Number.isFinite(Number(progress.percent)) ? Number(progress.percent) : 0;
              $("#modalProgressBar").style.width = `${Math.min(100, Math.max(0, percent))}%`;
              $("#modalMessage").textContent = `${progress.message || "正在准备更新"}${["download", "extract", "verify"].includes(progress.phase) ? ` · ${percent}%` : ""}。请勿关闭软件。`;
              if (progress.state === "failed") {
                const error = new Error(progress.error?.message || progress.message || "更新未能完成。 ");
                error.product = progress.error || null;
                throw error;
              }
              if (progress.state === "restarting") return;
              await new Promise((resolve) => window.setTimeout(resolve, 400));
            }
          },
        });
      }
    } catch (error) {
      $("#updateStatus").textContent = "检查失败";
      if (manual) reportError(error);
    } finally {
      state.updateChecking = false;
      button.disabled = !(state.bootstrap?.update?.enabled);
      button.textContent = state.availableUpdate ? "查看更新" : "检查更新";
    }
  }

  function automaticUpdateIntervalMs() {
    const configured = Number(state.bootstrap?.update?.check_interval_seconds);
    const seconds = Number.isFinite(configured) && configured >= 300 ? configured : 600;
    return seconds * 1000;
  }

  function scheduleAutomaticUpdateCheck(delayMs) {
    window.clearTimeout(state.updateCheckTimer);
    state.updateCheckTimer = window.setTimeout(async () => {
      await refreshUpdate(false);
      scheduleAutomaticUpdateCheck(automaticUpdateIntervalMs());
    }, delayMs);
  }

  function bindSearch(boxSelector, inputSelector, callback) {
    const input = $(inputSelector);
    const box = $(boxSelector);
    if (!input.hasAttribute("aria-label")) input.setAttribute("aria-label", input.placeholder || "搜索");
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
    $$('[data-stage-stop]').forEach((button) => button.addEventListener("click", runForceStop));
    $("#retryEnvironment").addEventListener("click", checkEnvironment);
    $("#settingsCheckWechat").addEventListener("click", checkEnvironment);
    $("#chooseWechat").addEventListener("click", chooseWechat);
    $("#settingsChooseWechat").addEventListener("click", chooseWechat);
    $("#downloadWechat").addEventListener("click", async () => { try { await api("open_wechat_download"); } catch (error) { reportError(error); } });
    bindSearch("#featureSearchBox", "#featureSearch", renderFeatureList);
    $("#refreshFeatures").addEventListener("click", refreshFeatures);
    $$("#featureAudience button").forEach((button) => button.addEventListener("click", () => {
      state.featureAudience = button.dataset.audience;
      $$("#featureAudience button").forEach((item) => item.classList.toggle("active", item === button));
      renderFeatureList();
    }));
    bindSearch("#groupSearchBox", "#groupSearch", renderGroups);
    bindSearch("#contactSearchBox", "#contactSearch", () => { state.contactPage = 1; renderContacts(); });
    bindSearch("#historySearchBox", "#historySearch", loadConversations);
    bindSearch("#helpSearchBox", "#helpSearch", renderHelp);
    $$("#helpAudience button").forEach((button) => {
      button.addEventListener("click", () => {
        state.helpAudience = button.dataset.helpAudience;
        $$("#helpAudience button").forEach((item) => item.classList.toggle("active", item === button));
        renderHelp();
      });
      button.disabled = false;
    });
    $$("[data-help-target]").forEach((button) => {
      button.addEventListener("click", () => navigate(button.dataset.helpTarget));
      button.disabled = false;
    });
    $("#refreshGroups").addEventListener("click", () => loadGroups(true));
    $("#refreshContacts").addEventListener("click", () => loadContacts(true));
    $$('[data-chat-management-tab]').forEach((button, index, tabs) => {
      button.addEventListener("click", () => selectChatManagementTab(button.dataset.chatManagementTab));
      button.addEventListener("keydown", (event) => {
        if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
        event.preventDefault();
        let next = event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : index + (event.key === "ArrowRight" ? 1 : -1);
        next = (next + tabs.length) % tabs.length;
        selectChatManagementTab(tabs[next].dataset.chatManagementTab, true);
      });
    });
    $("#contactPrevious").addEventListener("click", () => { state.contactPage -= 1; renderContacts(); $(".contact-table-scroll").scrollTop = 0; });
    $("#contactNext").addEventListener("click", () => { state.contactPage += 1; renderContacts(); $(".contact-table-scroll").scrollTop = 0; });
    $$("#historyKinds button").forEach((button) => button.addEventListener("click", () => {
      state.historyKind = button.dataset.kind;
      $$("#historyKinds button").forEach((item) => item.classList.toggle("active", item === button));
      loadConversations();
    }));
    $("#historyFilter").addEventListener("click", loadConversations);
    $("#newMessageButton").addEventListener("click", () => {
      const list = $("#messageList");
      list.scrollTop = list.scrollHeight;
      $("#newMessageButton").hidden = true;
    });
    $("#messageList").addEventListener("scroll", () => {
      const list = $("#messageList");
      if (list.scrollTop <= HISTORY_PRELOAD_THRESHOLD) void loadOlderMessages();
      if (list.scrollHeight - list.scrollTop - list.clientHeight < 80) $("#newMessageButton").hidden = true;
    });
    $("#exportHistory").addEventListener("click", async () => { try { const result = await api("export_history", "csv"); if (!result?.cancelled) toast(`已导出到 ${result.path}`); } catch (error) { reportError(error); } });
    $("#refreshSchedules").addEventListener("click", loadSchedules);
    $("#newScheduledTask").addEventListener("click", () => openScheduledEditor());
    ["#scheduledActionFilter", "#scheduledTargetFilter", "#scheduledStatusFilter"].forEach((selector) => $(selector).addEventListener("change", renderScheduledTasks));
    $("#clearScheduledChatFilter").addEventListener("click", () => { state.scheduledChatFilter = null; renderScheduledTasks(); });
    $("#manageGroupPolicies").addEventListener("click", async () => { await selectChatManagementTab("groups"); await navigate("groups"); });
    $("#refreshActivity").addEventListener("click", loadActivity);
    $$("#settingsTabs [data-settings-tab]").forEach((button) => button.addEventListener("click", () => selectSettingsSection(button.dataset.settingsTab)));
    coreSettings.init();
    $$('[data-core-settings-tab]').forEach((button) => button.addEventListener("click", () => setCoreSettingsTab(button.dataset.coreSettingsTab, true)));
    $("#webSearchForm").addEventListener("submit", saveWebSearchSettings);
    $("#webSearchProvider").addEventListener("change", () => {
      $("#webSearchKey").value = "";
      state.webSearchClearKey = false;
      updateWebSearchProviderCopy();
    });
    $("#webSearchForm").addEventListener("change", (event) => {
      if (event.target.name === "enabled") updateToggleCopy(event);
    });
    $("#revealWebSearchKey").addEventListener("click", (event) => {
      const input = $("#webSearchKey");
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      event.currentTarget.textContent = showing ? "显示" : "隐藏";
      event.currentTarget.setAttribute("aria-pressed", String(!showing));
    });
    $("#clearWebSearchKey").addEventListener("click", () => {
      $("#webSearchKey").value = "";
      state.webSearchClearKey = true;
      $("#webSearchNote").textContent = "当前服务商密钥将在保存时清除。";
    });
    $("#memoryForm").addEventListener("submit", saveMemoryExperience);
    $("#emotionForm").addEventListener("submit", saveEmotionExperience);
    $("#manageMemories").addEventListener("click", openMemoryManager);
    $("#providerForm").addEventListener("submit", saveProvider);
    $("#providerForm").addEventListener("input", updateProviderDirty);
    $("#providerForm").addEventListener("change", updateProviderDirty);
    $("#providerForm").addEventListener("input", (event) => {
      if (["api_base", "model", "thinking_enabled"].includes(event.target.name)) syncProviderThinkingControl();
    });
    $("#providerForm").addEventListener("change", (event) => {
      if (["api_base", "model", "thinking_enabled"].includes(event.target.name)) syncProviderThinkingControl();
    });
    $("#providerPreset").addEventListener("change", (event) => { applyProviderPreset(event.currentTarget.value); updateProviderDirty(); });
    $("#closeProviderEditor").addEventListener("click", requestProviderOverview);
    $("#cancelProviderEdit").addEventListener("click", requestProviderOverview);
    $("#testProvider").addEventListener("click", testSelectedProvider);
    $("#discoverProviderModels").addEventListener("click", discoverProviderModels);
    bindProviderModelCombobox();
    $("#deleteProvider").addEventListener("click", deleteSelectedProvider);
    $("#revealProviderKey").addEventListener("click", (event) => {
      const input = $("#providerApiKey");
      const showing = input.type === "text";
      input.type = showing ? "password" : "text";
      event.currentTarget.textContent = showing ? "显示" : "隐藏";
      event.currentTarget.setAttribute("aria-pressed", String(!showing));
    });
    $("#providerServiceList").addEventListener("click", (event) => {
      const create = event.target.closest("[data-provider-create]");
      if (create) { beginNewProvider("free_grok"); return; }
      const edit = event.target.closest("[data-provider-edit]");
      if (edit) { editProvider(edit.dataset.providerEdit); return; }
      const makeDefault = event.target.closest("[data-provider-default]");
      if (makeDefault) { setProviderDefault(makeDefault.dataset.providerDefault, makeDefault); return; }
      const thinking = event.target.closest("[data-provider-thinking]");
      if (thinking) { setProviderThinking(thinking.dataset.providerThinking, thinking); return; }
      const remove = event.target.closest("[data-provider-delete]");
      if (remove) deleteProvider(remove.dataset.providerDelete);
    });
    document.addEventListener("click", (event) => {
      $$(".provider-card-menu[open]").forEach((menu) => {
        if (!menu.contains(event.target)) menu.removeAttribute("open");
      });
    });
    $("#openInstall").addEventListener("click", async () => { try { await api("open_install_folder"); } catch (error) { reportError(error); } });
    $("#openUninstaller").addEventListener("click", async () => { try { await api("open_uninstaller"); } catch (error) { reportError(error); } });
    $("#createBackup").addEventListener("click", createBackup);
    $("#restoreBackup").addEventListener("click", chooseRestoreBackup);
    $("#exportDiagnostics").addEventListener("click", exportDiagnostics);
    $("#openOnboarding").addEventListener("click", () => navigate("help"));
    $("#cleanHistory").addEventListener("click", () => confirmAction({ title: "按日期清理聊天记录", message: "将删除所选日期以前的本机记录副本，此操作无法撤销。", confirmText: "开始清理", field: {label: "保留此日期及之后的记录", type: "date", value: ""}, onConfirm: async (value) => { const result = await api("delete_history_before", value); toast(`已清理 ${result.deleted} 条记录。 `); if (state.activePage === "history") await loadConversations(); } }));
    $("#checkUpdate").addEventListener("click", () => refreshUpdate(true));
    $("#modalCancel").addEventListener("click", () => modalResolve?.(false));
    window.addEventListener("beforeunload", (event) => { if (state.featureDirty || state.featureSaving || state.providerDirty || state.scheduledDirty || coreSettings.dirty || coreSettings.busy) { event.preventDefault(); event.returnValue = ""; } });
    window.addEventListener("focus", () => { refreshVisibleFeatureState().catch(reportError); });
    window.addEventListener("focus", () => { refreshVisibleChatData().catch(reportError); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshVisibleChatData().catch(reportError);
    });
    $("#modalBackdrop").addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const targets = $$("button:not(:disabled), input:not(:disabled)", $("#modalBackdrop"));
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    });
    $("#modalConfirm").addEventListener("click", () => modalResolve?.(true));
    $("#modalBackdrop").addEventListener("click", (event) => { if (event.target === event.currentTarget) modalResolve?.(false); });
    document.addEventListener("keydown", (event) => { if (event.key === "Escape" && !$("#modalBackdrop").hidden) modalResolve?.(false); });
    $("#onboardingRecommended").addEventListener("click", () => closeOnboarding(false));
    $("#onboardingConfigure").addEventListener("click", () => closeOnboarding(true));
    $("#onboardingBackdrop").addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const targets = $$('button:not(:disabled)', $("#onboardingBackdrop"));
      const first = targets[0], last = targets[targets.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    });
    $$("[data-action-bound]", document).forEach((button) => { button.disabled = false; });
  }

  async function initialize() {
    bindEvents();
    initializeHistoryDates();
    setSettingsSection("model");
    try {
      state.bootstrap = await api("bootstrap");
      fillSettings(state.bootstrap);
      renderStatus(state.bootstrap.status);
      const previewParams = new URLSearchParams(window.location.search);
      if (previewParams.has("preview") && (previewParams.get("demo") === "model-services" || previewParams.get("demo") === "thinking-mode")) {
        state.bootstrap.experience.onboarding_complete = true;
        await navigate("settings");
      }
      window.setInterval(refreshStatus, 1500);
      window.setInterval(pollHistory, 1500);
      window.setInterval(pollGroups, 15000);
      if (state.bootstrap.update?.enabled) scheduleAutomaticUpdateCheck(1800);
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
      previewFeature("feature-twitter", "推特推送", "schema-and-page", "订阅 X/Twitter 推主，按会话管理订阅并定时推送新推文；支持链接解析、媒体和翻译配置。", {page: "plugin-settings", fieldCount: 8}),
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
      previewFeature("feature-video", "视频理解", "schema", "从微信短视频提取关键帧和音轨，并由 Grok 4.6 生成时间线摘要。", {modelFieldCount: 3}),
      previewFeature("feature-meme", "表情管理", "schema-and-page", "管理多套表情包、会话绑定、资源目录和同步设置。", {page: "meme-management"}),
    ];
    const groupAnalysisBasicSchema = {
      basic: {description: "基础设置", type: "object", items: {
        admin_ids: {description: "群聊分析管理员", type: "list", _ui_control: "line-list", _ui_tint: true, placeholder: "例如：wxid_admin", hint: "每行填写一个用户 ID，使用权限提示中显示的 ID；仅授予本插件的指令权限，机器人管理员仍可使用。留空时仅机器人管理员可用。"},
        group_list_mode: {description: "群聊权限模式", type: "string", options: ["whitelist", "blacklist", "none"], labels: ["白名单：仅允许列表中的群", "黑名单：屏蔽列表中的群", "不限制群聊"], hint: "白名单为空时没有群可用；黑名单为空时不屏蔽群；不限制群聊时忽略名单。此项不会授予管理员权限。"},
        group_list: {description: "群组白/黑名单列表", type: "list", _ui_control: "line-list", _group_selector: true, placeholder: "从群聊列表选择，或粘贴完整会话 ID", hint: "点击“从群聊列表添加”下拉选择群；多个群一行一个。也可粘贴 /sid 获取的完整会话 ID，或填写群标识（例如 123456@chatroom）。"},
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
      llm_provider_id: {description: "用于分析的模型", type: "string", options: ["", "jyjapi/grok-4.6"], labels: ["使用系统默认", "免费 Grok 4.6"], hint: "通常选择与智能回复相同的模型；不选择时使用当前群聊的默认模型。"},
    }}};
    const groupAnalysisConfig = {
      basic: {admin_ids: [], group_list_mode: "none", group_list: [], bot_self_ids: [], report_template: "scrapbook", analysis_days: 1, min_messages_threshold: 200},
      auto_analysis: {scheduled_group_list: ["18310516375@chatroom"], auto_analysis_time: ["23:00"]},
      analysis_features: {topic_analysis_enabled: true, user_title_analysis_enabled: true, golden_quote_analysis_enabled: true, chat_quality_analysis_enabled: true},
      llm: {llm_provider_id: "jyjapi/grok-4.6"},
      performance: {max_concurrent: 1},
    };
    const imageBasicSchema = {
      primary_base_url: {description: "主生图 API Base URL", type: "string", hint: "例如 https://api.hostcentral.cc/。", _ui_group: "主生图模型"},
      primary_model: {description: "主生图模型", type: "string", hint: "例如 gpt-image-2。", _ui_group: "主生图模型"},
      primary_api_key: {description: "主生图 API Key", type: "string", secret: true, hint: "密钥仅保存在本机；已保存时留空不会覆盖。", _ui_group: "主生图模型"},
      secondary_enabled: {description: "使用备用生图模型", type: "bool", hint: "主模型失败时才会使用；不需要时无需填写备用配置。", _ui_group: "备用生图模型", _ui_full: true, _ui_disclosure: "添加备用模型"},
      secondary_base_url: {description: "备用生图 API Base URL", type: "string", _ui_group: "备用生图模型"},
      secondary_model: {description: "备用生图模型", type: "string", _ui_group: "备用生图模型"},
      secondary_api_key: {description: "备用生图 API Key", type: "string", secret: true, hint: "密钥仅保存在本机；已保存时留空不会覆盖。", _ui_group: "备用生图模型"},
      use_system_proxy: {description: "使用本地代理", type: "bool", hint: "关闭时直连；端口留空时使用系统已有代理设置。", _ui_group: "本地代理", _ui_full: true},
      local_proxy_port: {description: "本地代理端口（可留空）", type: "string", hint: "例如 7890。仅连接本机 127.0.0.1；留空时使用系统已有代理设置。", _ui_group: "本地代理", _ui_control: "proxy-port", _ui_depends_on: "use_system_proxy", min: 1, max: 65535},
    };
    for (const [key, field] of Object.entries(imageBasicSchema)) {
      if (key.startsWith("secondary_") && key !== "secondary_enabled") field._ui_depends_on = "secondary_enabled";
    }
    const imageConfig = {
      primary_api_key: "", primary_base_url: "https://api.hostcentral.cc/", primary_model: "gpt-image-2",
      secondary_enabled: false, secondary_api_key: "", secondary_base_url: "https://api.gemai.cc/v1", secondary_model: "gpt-image-2-2K",
      use_system_proxy: true, local_proxy_port: "",
    };
    const previewGuides = {};
    const previewGuideOverrides = {
      "群聊上下文": {summary: "协调群聊接话和多人上下文", setup_steps: ["设置回复节奏和允许接管的会话。"], prerequisites: ["它不是长期记忆库。"], audience: "群聊常用", readiness: "可用但已纠正名称", recommendation: "保留。适合多人群聊的接话与上下文协调，但不应宣传为长期记忆。"},
      "群聊分析": {summary: "把一天群聊整理成日报", setup_steps: ["从管理器的群聊列表选择自动日报群。", "先让当前机器人账号在群里正常接收一段时间，再开启日报。"], prerequisites: ["只覆盖新桥接成功接收后的本机消息；安装前和离线期间的记录无法补拉。"], audience: "群管理常用", readiness: "已适配本机消息记录", recommendation: "建议保留并按需启用；数据按当前机器人账号和群聊双重隔离。"},
      "定时任务": {summary: "安排内容推送和签到", setup_steps: ["打开专属配置页创建计划，再到定时任务中心调整。"], prerequisites: ["写操作仅允许可靠管理员身份；共享账号签到仅允许机器人管理员。"], audience: "群管理常用", readiness: "权限已加固", recommendation: "建议保留；任务按当前会话隔离，微信无法确认群角色时默认拒绝修改。"},
      "自动改代码（实验）": {summary: "实验性的功能代码维护", setup_steps: ["仅由了解代码与回滚的维护人员使用。"], prerequisites: ["可读写并重新加载功能代码。"], audience: "不建议默认启用", readiness: "实验性高风险", recommendation: "不属于群主管理刚需，应从小白默认界面和新安装默认项移除。"},
      "群成员变动通知": {summary: "提示入群、退群和群昵称变化", setup_steps: ["设置提示文案后在测试群验证。"], prerequisites: ["不能发布或编辑微信群公告。"], audience: "群管理常用", readiness: "收到成员事件即可用", recommendation: "建议保留，并始终使用准确名称。"},
      "图片生成": {summary: "用文字或参考图生成图片", setup_steps: ["先填写一个主生图模型的地址、模型和密钥。", "确有需要时点击“添加备用模型”再填写第二个。", "连接失败时可开启本地代理；填写本机端口或留空使用已有代理设置。"], prerequisites: ["需要支持生图的服务；聊天模型不能自动替代生图模型。"], audience: "场景选配", readiness: "需要生图服务", recommendation: "有生图需求时启用；服务密钥只保存在本机。"},
      "洛克王国助手": {summary: "使用洛克王国游戏助手", setup_steps: ["通过二维码或已有凭据绑定游戏账号。"], prerequisites: ["账号凭据不要发到群里。"], audience: "兴趣选配", readiness: "需要游戏账号绑定", recommendation: "仅洛克王国用户群有必要，不是运势功能。"},
    };
    const previewSavedConfigs = new Map();
    const corePreviewSchema = {
      analyzer_model: {type: "string", description: "群聊分析模型", options: ["", "jyjapi/grok-4.6"], labels: ["请选择模型（完整使用所必需）", "免费 Grok 4.6"], default: "jyjapi/grok-4.6"},
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
      if (id === "feature-image") return {
        ...item,
        basic_schema: imageBasicSchema,
        model_schema: {},
        schema: imageBasicSchema,
        advanced_schema: {},
        config: structuredClone(previewSavedConfigs.get(id) || imageConfig),
        advanced_config: {},
        configuration_note: "生图服务配置已合并到常用设置，密钥保存后不会回显。",
        setup_steps: item.guide.setup_steps,
        revision: "preview",
        providers: [],
        personas: [],
        engine_online: true,
      };
      if (id !== "feature-group-analysis") return {...item, basic_schema: {}, model_schema: {}, schema: {}, config: {}, revision: "preview", providers: [], personas: [], engine_online: true};
      return {
        ...item,
        basic_schema: groupAnalysisBasicSchema,
        model_schema: groupAnalysisModelSchema,
        schema: {...groupAnalysisBasicSchema, ...groupAnalysisModelSchema},
        config: structuredClone(previewSavedConfigs.get(id) || groupAnalysisConfig),
        advanced_config: structuredClone(previewSavedConfigs.get(id) || groupAnalysisConfig),
        advanced_schema: {...groupAnalysisBasicSchema, ...groupAnalysisModelSchema, incremental: {type: "object", description: "增量分析设置", items: {
          incremental_group_list: {description: "增量分析群列表", type: "list", _ui_control: "line-list", _group_selector: true, placeholder: "从群聊列表选择，或粘贴完整会话 ID", hint: "从群聊列表下拉选择；多个群一行一个。也可粘贴 /sid 获取的完整会话 ID。目标群还须通过基础群聊权限和定时名单。"},
        }}, performance: {type: "object", description: "并发设置", items: {max_concurrent: {type: "int", description: "同时生成报告数量", min: 1, max: 5, default: 1}}}},
        configuration_note: "这里只展示日常会用到的设置。渲染、重试、并发和提示词等专业参数继续使用推荐值，保存时不会被重置。",
        setup_steps: ["在“自动日报群”旁点击“从群聊列表添加”，无需在群里发送命令", "选择需要生成日报的群；多个群可以继续添加", "确认生成时间和分析模型后保存"],
        revision: "preview",
        providers: [{value: "jyjapi/grok-4.6", label: "免费 Grok 4.6"}],
        personas: [],
        engine_online: true,
      };
    }
    let previewProviders = [];
    if (new URLSearchParams(window.location.search).get("demo") === "thinking-mode") {
      previewProviders = [{id: "custom_mimo/mimo-v2.5", name: "mimo-v2.5", api_base: "https://token-plan-cn.xiaomimimo.com/v1", model: "mimo-v2.5", configured: true, api_key_configured: true, is_default: true, is_recommended: false, thinking_supported: true, thinking_enabled: true}];
    }
    const previewPresets = [
      {id: "free_grok", name: "免费 Grok 4.6（推荐）", api_base: "https://api.xiuxianjyj.xin/v1", model: "grok-4.6", description: "面向普通用户的默认回复服务。"},
      {id: "deepseek", name: "DeepSeek（自用）", api_base: "https://api.deepseek.com/v1", model: "deepseek-v4-flash", description: "适合在你自己的设备上使用。"},
      {id: "custom", name: "其他兼容服务", api_base: "", model: "", description: "填写兼容接口地址、模型名称和密钥。"},
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
    let previewWebSearch = {
      enabled: false,
      provider: "bocha",
      link_enabled: false,
      providers: Object.fromEntries([
        ["tavily", "Tavily"], ["bocha", "博查 Bocha"], ["brave", "Brave Search"],
        ["baidu_app_builder", "百度 AI Search"], ["firecrawl", "Firecrawl"], ["exa", "Exa"],
      ].map(([id, name]) => [id, {id, name, description: "支持联网搜索与网页结果整理。", configured: id === "bocha", key_count: id === "bocha" ? 1 : 0}])),
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
      return {items: structuredClone(viewItems), summary: {messages: messages.filter((message) => items.some((item) => item.conversation_id === message.conversation_id)).length, conversations: items.length, visible_conversations: items.length, directory_groups: 6, private_messages: messages.filter((message) => items.some((item) => item.type === "private" && item.conversation_id === message.conversation_id)).length, group_messages: messages.filter((message) => items.some((item) => item.type === "group" && item.conversation_id === message.conversation_id)).length}};
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
    return {
      bootstrap: () => ok({version: "0.9.2", install_root: "D:\\小妹", status, brand: {display_name: "小妹", short_name: "小妹", persona_name: "小妹", welcome_text: "你好，我是小妹。", admin_id: "wxid_admin"}, provider: {configured: false, api_base: "", model: "", api_key_configured: false}, providers: structuredClone(previewProviders), provider_presets: previewPresets, experience: structuredClone(previewExperience), update: {enabled: true, channel: "stable", check_interval_seconds: 600, result: null}}),
      get_status: () => ok(status), check_environment: () => ok(status), start_robot: () => ok({accepted: true}), stop_robot: () => ok({accepted: true}), force_stop_robot: () => ok({accepted: true}), select_wechat: () => ok(status), open_wechat_download: () => ok({opened: true}),
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
      save_group_policy: (_accountId, groupId, values) => { previewGroupPolicy = {...previewGroupPolicy, ...structuredClone(values), revision: `preview-policy-${Date.now()}`}; return ok(previewPolicyOverview(groupId).policy, "群聊策略已保存；未向群内发送消息。"); },
      get_contacts: () => ok({account_id: "wxid_demo_account", source: "wechat_cache", stale: false, updated_at: new Date().toISOString(), total: previewContacts.length, truncated: false, items: structuredClone(previewContacts), message: "预览数据：正式版本从当前登录微信通讯录读取，刷新不会发送消息。"}),
      get_contact_detail: (contactId) => { const item = previewContacts.find((contact) => contact.user_id === contactId); return ok({account_id: "wxid_demo_account", item: structuredClone(item || {}), items: item ? [structuredClone(item)] : [], total: item ? 1 : 0}); },
      get_private_policy: (_accountId, contactId) => ok(previewPrivatePolicyOverview(contactId)),
      save_private_policy: (_accountId, contactId, values) => { previewPrivatePolicy = {...previewPrivatePolicy, ...structuredClone(values), configured: true, revision: `preview-private-${Date.now()}`}; return ok(previewPrivatePolicyOverview(contactId).policy, "联系人可用功能已保存。"); },
      save_feature: (id, config) => { if (id === "feature-95a906d4b9173155") { corePreviewConfig = structuredClone(config); corePreviewRevision += 1; } else previewSavedConfigs.set(id, structuredClone(config)); return ok({revision: "preview-2", apply_state: "applied", message: "预览：已保存并确认生效，后续请求使用新设置。"}); }, reload_feature: () => ok({reloaded: true, message: "功能已经重新载入。"}), open_feature_center: () => ok({opened: true, message: "高级配置窗口正在打开。"}),
      get_conversations: (filters) => ok(previewConversations(filters)),
      get_messages: (conversationId, filters = {}) => {
        const search = String(filters.search || "").trim().toLocaleLowerCase("zh-CN");
        const matching = previewHistoryMessages.filter((message) => {
          if (message.conversation_id !== conversationId) return false;
          if (!search) return true;
          if (search.startsWith("wxid_")) return previewHistoryMatches(message, search);
          return [message.sender_name, message.wechat_id, message.group_nickname, message.content]
            .some((value) => String(value || "").toLocaleLowerCase("zh-CN").includes(search));
        });
        if (filters.paged !== true) return ok(structuredClone(matching));
        const before = filters.before;
        const older = before ? matching.filter((message) => (
          Number(message.timestamp) < Number(before.timestamp)
          || (Number(message.timestamp) === Number(before.timestamp) && Number(message.id) < Number(before.id))
        )) : matching;
        const pageSize = Math.max(1, Math.min(Number(filters.page_size) || HISTORY_PAGE_SIZE, HISTORY_PAGE_SIZE));
        return ok({items: structuredClone(older.slice(-pageSize)), has_more: older.length > pageSize});
      },
      delete_conversation: () => ok({deleted: 34}), delete_history_before: () => ok({deleted: 120}), export_history: () => ok({path: "D:\\聊天记录.csv"}),
      get_schedules: () => ok({running: 0, paused: 0, items: []}),
      get_scheduled_task_context: () => ok({account_id: "wxid_demo_account", items: structuredClone([...previewTasks.values()]), capabilities: Object.entries(SCHEDULED_ACTIONS).map(([id, name]) => ({id, name, ready: true, message: "预览模式"})), groups: structuredClone(previewTaskGroups), contacts: previewContacts.filter((entry) => entry.contact_type === "微信好友").map((entry) => ({id: entry.user_id, name: entry.name})), online: true, runtime_ready: true, message: ""}),
      save_scheduled_task: (payload) => ok(previewTaskSave(payload)),
      delete_scheduled_task: (_account, id) => { previewTasks.delete(id); return ok({deleted: true}); },
      set_scheduled_task_enabled: (_account, id, enabled) => ok(previewTaskSave({...previewTasks.get(id), enabled, draft: false})),
      preview_scheduled_times: (schedule) => ok({items: previewTaskTimes(schedule), text: "请补齐有效的未来日期、星期和时间。"}),
      parse_scheduled_command: (command) => { const normalized = command.replace(/^\//, ""); const match = normalized.match(/^(.+?)(明天|今日|今天)?天气$/); if (match) return ok({action: "weather", arguments: {city: match[1], day: match[2] === "明天" ? "tomorrow" : "today", output: "image"}}); if (/^(新闻|资讯|热点)$/.test(normalized)) return ok({action: "news", arguments: {count: 10}}); if (normalized === "短剧推荐") return ok({action: "short_drama", arguments: {media_type: "短剧", limit: 12}}); return Promise.resolve({ok: false, error: {message: "这个命令暂不支持，请直接选择发送内容。"}}); },
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
      get_activity: () => ok([{time: new Date().toISOString(), title: "全部功能加载完成", event: "FUNCTION_LOADED", status: "success", code: ""}, {time: new Date(Date.now() - 1500).toISOString(), title: "微信连接成功", event: "CONNECTION_CONNECTED", status: "success", code: ""}, {time: new Date(Date.now() - 4000).toISOString(), title: "机器人功能已就绪", event: "CORE_READY", status: "success", code: ""}, {time: new Date(Date.now() - 7000).toISOString(), title: "微信环境检查通过", event: "WECHAT_READY", status: "success", code: ""}]),
      get_response_traces: () => ok([{time: new Date(Date.now() - 18000).toISOString(), title: "群聊点名", duration_text: "3.4 秒", stages: [{label: "点名快路径", status: "success", detail: "已跳过额外判断", duration_text: "0.4 秒"}, {label: "长期记忆", status: "skipped", detail: "普通寒暄无需回忆", duration_text: "<0.1 秒"}, {label: "精简情感", status: "success", detail: "已加入好感度与心情", duration_text: "0.1 秒"}, {label: "生成回复", status: "success", duration_text: "2.6 秒"}, {label: "文字发送", status: "success", duration_text: "0.3 秒"}]}, {time: new Date(Date.now() - 82000).toISOString(), title: "询问上次内容", duration_text: "5.1 秒", stages: [{label: "点名快路径", status: "success", duration_text: "0.4 秒"}, {label: "长期记忆 3 条", status: "success", detail: "命中明确回忆说法", duration_text: "0.8 秒"}, {label: "精简情感", status: "success", duration_text: "0.1 秒"}, {label: "生成回复", status: "success", duration_text: "3.5 秒"}, {label: "文字发送", status: "success", duration_text: "0.3 秒"}]}]),
      get_experience: () => ok(structuredClone(previewExperience)),
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
      complete_onboarding: () => { previewExperience.onboarding_complete = true; return ok({onboarding_complete: true}, "基础设置已完成。"); },
      get_providers: () => ok(structuredClone(previewProviders)),
      save_provider: (values) => {
        const id = `${values.preset === "deepseek" ? "deepseek" : values.preset === "free_grok" ? "jyjapi" : "custom"}/${values.model}`;
        const original = previewProviders.find((item) => item.id === values.provider_id);
        const makeDefault = !previewProviders.length || Boolean(original?.is_default);
        previewProviders = previewProviders
          .filter((item) => item.id !== values.provider_id && item.id !== id)
          .map((item) => makeDefault ? {...item, is_default: false} : item);
        const saved = {id, name: values.preset === "free_grok" ? "免费 Grok 4.6" : values.preset === "deepseek" ? `DeepSeek · ${String(values.model).replace("deepseek-", "")}` : values.model, api_base: values.api_base, model: values.model, configured: true, api_key_configured: true, is_default: makeDefault, is_recommended: values.preset === "free_grok", thinking_supported: providerThinkingSupported(values.api_base, values.model), thinking_enabled: values.thinking_enabled !== false, apply_state: "applied"};
        previewProviders.unshift(saved);
        return ok(saved, makeDefault ? "回复模型已热更新并立即生效。" : "模型服务已保存，当前回复模型保持不变。");
      },
      set_default_provider: (id) => { previewProviders = previewProviders.map((item) => ({...item, is_default: item.id === id})); return ok({id, apply_state: "applied"}, "默认回复模型已切换并立即生效。"); },
      set_provider_thinking: (id, enabled) => { previewProviders = previewProviders.map((item) => item.id === id ? {...item, thinking_enabled: Boolean(enabled)} : item); return ok({id, thinking_enabled: Boolean(enabled), apply_state: "applied"}, "思考模式已热更新，下一条消息立即生效。"); },
      test_provider: () => ok({ok: true}, "模型服务连接正常。"),
      discover_provider_models: () => ok({items: ["grok-4.6", "deepseek-chat", "gpt-5.4", "claude-sonnet-4.5", "gemini-2.5-pro", "qwen3-max"], total: 6}, "已读取 6 个可用模型。"),
      delete_provider: (id) => { previewProviders = previewProviders.filter((item) => item.id !== id); return ok({id}, "模型服务已删除并立即生效。"); },
      get_maintenance: () => ok({preview: {file_count: 18, uncompressed_size: 39636172, can_create: true}, backups: [{id: "robot-backup-preview.zip", file_name: "robot-backup-preview.zip", size: 12740121, modified_at: new Date(Date.now() - 86400000).toISOString()}], diagnostics: {summary: {error_count: 0, warning_count: 1, check_count: 14}}}),
      create_backup: () => ok({id: "robot-backup-new.zip"}, "设置、人格和长期记忆已备份。"),
      validate_restore: (id) => ok({valid: true, backup_id: id}), restore_backup: (id) => ok({backup_id: id}, "备份已经恢复，并已保留恢复前副本。"),
      get_diagnostics: () => ok({summary: {error_count: 0, warning_count: 1, check_count: 14}}), export_diagnostics: () => ok({file_name: "robot-diagnostics-preview.json"}, "安全诊断摘要已导出。"),
      open_install_folder: () => ok({opened: true}), open_uninstaller: () => ok({opened: true}),
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
