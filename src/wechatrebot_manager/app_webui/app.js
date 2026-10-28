(() => {
  "use strict";

  const state = {
    bootstrap: null,
    theme: "light",
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
    featureReferenceOpen: {},
    featureDirty: false,
    featureSaving: false,
    featureLoad: 0,
    featurePrefetch: 0,
    featureDetails: {},
    imageServiceChoicesStale: false,
    featureFeedback: "",
    featurePageLoad: 0,
    groupsLoaded: false,
    groupsBusy: false,
    groups: [],
    groupAccount: "",
    groupMemberCounts: {},
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
    historyAccount: "",
    historyReadyAccount: "",
    historyConversationSnapshot: null,
    conversations: [],
    selectedConversation: null,
    selectedMessages: [],
    selectedSenderKey: "",
    historyLoad: 0,
    historyConversationsLoading: false,
    messageLoad: 0,
    historyMessagesLoading: false,
    historyPolling: false,
    historyHasMore: false,
    historyHasNewer: false,
    historyLoadingOlder: false,
    historyManualRefresh: false,
    historyOlderLoad: 0,
    historyMessageQuery: "",
    historyMessageCache: new Map(),
    historyMessageRequests: new Map(),
    historyMessageCacheEpoch: 0,
    schedules: [],
    scheduledContext: null,
    legacyMigrations: [],
    legacyMigrationLoading: false,
    scheduledCommandCatalog: [],
    scheduledEditor: null,
    scheduledDirty: false,
    scheduledBusy: false,
    scheduledLoad: 0,
    scheduledTimeLoad: 0,
    scheduledChatFilter: null,
    scheduledTargetTab: "group",
    scheduledReturnDraft: false,
    experience: null,
    readiness: null,
    maintenance: null,
    responseTraces: [],
    activityItems: [],
    activityPage: 1,
    polling: false,
    startPending: false,
    composing: false,
    availableUpdate: null,
    mandatoryUpdate: false,
    mandatoryPromptVersion: "",
    updateChecking: false,
    updateCheckTimer: 0,
    settingsSection: "model",
    webSearch: null,
    webSearchDirty: false,
    providerScreen: "overview",
    providerDirty: false,
    providerBaseline: "",
    providerCleanStatus: "尚未保存",
    providerModels: [],
    providerModelFiltering: false,
    providerModelFocus: -1,
    dismissedHomeSetupAlerts: new Set(),
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
  const THEME_STORAGE_KEY = "wechatrebot-theme";
  const THEME_CHANNEL = "wechatrebot-theme";
  const HOME_SETUP_DISMISSALS_STORAGE_KEY = "wechatrebot-home-setup-dismissals";
  const GROUP_MEMBER_COUNTS_STORAGE_PREFIX = "wechatrebot-group-member-counts:";
  const CONTACT_PAGE_SIZE = 100;
  const HISTORY_PAGE_SIZE = 20;
  const HISTORY_PRELOAD_THRESHOLD = 120;
  const HISTORY_MESSAGE_CACHE_LIMIT = 32;
  const HISTORY_MESSAGE_WINDOW_LIMIT = 200;
  const HISTORY_MESSAGE_CACHE_TTL_MS = 30000;
  const GROUP_DIRECTORY_RETRY_DELAYS_MS = [1500, 4000];
  const GROUP_MEMBER_RETRY_DELAYS_MS = [800, 2000];
  const GROUP_MEMBER_REQUEST_CONCURRENCY = 4;
  const AVATAR_RETRY_DELAYS_MS = [1200, 4000];
  const escapeHtml = (value) => String(value ?? "")
    .replaceAll("&", "&amp;").replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");

  function normalizeTheme(value) {
    return value === "dark" ? "dark" : "light";
  }

  function storedTheme() {
    const saved = document.documentElement.dataset.savedTheme;
    if (saved === "dark" || saved === "light") return saved;
    try {
      return normalizeTheme(window.localStorage.getItem(THEME_STORAGE_KEY));
    } catch (_error) {
      return normalizeTheme(document.documentElement.dataset.theme);
    }
  }

  function syncEmbeddedTheme(frame = $("#featurePageFrame"), onStylesReady = null) {
    window.syncFeaturePageTheme(frame, state.theme, onStylesReady);
  }

  async function syncWindowTheme() {
    // Older manager bridges and browser previews may have no native caption.
    if (typeof window.pywebview?.api?.sync_window_theme !== "function") return;
    const palette = getComputedStyle(document.documentElement);
    try {
      await api("sync_window_theme", state.theme,
        palette.getPropertyValue("--surface").trim(), palette.getPropertyValue("--ink").trim());
    } catch (error) {
      window.managerErrorReporter?.capture(error, state.activePage);
    }
  }

  function applyTheme(value, persist = false) {
    const theme = normalizeTheme(value);
    state.theme = theme;
    document.documentElement.dataset.theme = theme;
    document.documentElement.style.colorScheme = theme === "dark" ? "dark" : "light";
    const colorScheme = document.querySelector('meta[name="color-scheme"]');
    if (colorScheme) colorScheme.content = theme === "dark" ? "dark light" : "light dark";
    if (persist) {
      try { window.localStorage.setItem(THEME_STORAGE_KEY, theme); } catch (_error) { /* storage is optional in WebView2 */ }
    }
    const button = $("#themeToggle");
    const iconNode = $("#themeToggleIcon");
    const textNode = $("#themeToggleText");
    const nextLabel = theme === "dark" ? "切换到浅色模式" : "切换到深色模式";
    if (button) {
      button.setAttribute("aria-pressed", String(theme === "dark"));
      button.setAttribute("aria-label", nextLabel);
      button.title = nextLabel;
      button.classList.toggle("is-dark", theme === "dark");
    }
    if (iconNode) iconNode.setAttribute("href", theme === "dark" ? "#i-sun" : "#i-moon");
    if (textNode) textNode.textContent = theme === "dark" ? "浅色模式" : "深色模式";
    syncEmbeddedTheme();
    void syncWindowTheme();
  }

  function initializeTheme() {
    applyTheme(storedTheme());
  }

  let coreSettings;
  let subscription;

  function initializeControllers() {
    coreSettings = window.createRobotCoreSettings({
      read: () => api("get_robot_core"),
      request: (method, endpoint, payload) => api("request_robot_core", method, endpoint, payload),
      save: (config, revision, identity) => api("save_robot_core", config, revision, identity),
      applyBrand: (brand) => {
        state.bootstrap.brand = brand;
        applyBrand(brand);
      },
      schemaMarkup, configFromForm, escapeHtml, confirmAction, toast, reportError, growTextarea,
    });

    subscription = window.createSubscriptionPage({
      api, escapeHtml, confirmAction, copyText, navigate, refreshRuntime: refreshStatus,
      openMaintenance: async () => { await navigate("settings"); setSettingsSection("system", true); },
    });
  }

  function safeImageUrl(value) {
    try {
      const parsed = new URL(String(value || "").trim());
      return ["http:", "https:"].includes(parsed.protocol) || /^data:image\/(?:png|jpeg|webp);base64,[A-Za-z0-9+/=]+$/.test(parsed.href) ? parsed.href : "";
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
    const title = product?.title || "操作未完成";
    const detail = (typeof product?.detail === "string" ? product.detail.trim() : "")
      || (typeof product?.message === "string" ? product.message.trim() : "");
    const message = product
      ? `${title}${detail && detail !== title ? `：${detail}` : ""}${product.code ? `（${product.code}）` : ""}`
      : (error?.message || "操作未完成，请稍后重试。 ");
    toast(message, "error");
    if (!product) window.managerErrorReporter?.capture(error, state.activePage);
    else if (product.recorded === false && product.code !== "11007") toast("错误记录未能保存，请截图联系技术支持。", "error");
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
    if (["STOPPED", "LICENSE_READY"].includes(current) && status?.environment?.ready && !status?.error) {
      return startingStatus(status);
    }
    if (!STARTUP_STATES.has(current)) state.startPending = false;
    return status;
  }

  function renderStatus(rawStatus) {
    const wasOnline = Boolean(state.status?.online);
    const status = reconcileStartPending(rawStatus);
    state.status = status;
    subscription.updateRuntime(status);
    const runtime = status.runtime || {};
    const environment = status.environment || {};
    const current = status.state || "STOPPED";
    const online = Boolean(status.online);
    const licenseWaiting = ["LICENSE_READY", "LICENSE_PAUSED"].includes(current);
    const waiting = licenseWaiting || ["WAITING_WECHAT", "VERIFYING_CONNECTION", "STOP_WAIT_WECHAT"].includes(current);
    const problem = current === "ERROR";
    const active = !licenseWaiting && !["STOPPED", "ERROR"].includes(current);
    const starting = state.startPending || ["CORE_STARTING", "CORE_READY", "CONNECTION_STARTING"].includes(current);
    const accountStatusAvailable = runtime.account_status_available !== false;
    const confirmingConnection = current === "RUNNING" && !online && !accountStatusAvailable && !status.error;
    contactProfile.updateAccount(accountStatusAvailable ? String(runtime.wechat_identity || "").trim() : "");
    const wechatLoggedIn = accountStatusAvailable && Boolean(runtime.wechat_identity);
    const offlineHistory = status.history_offline === true && !runtime.connection_ready && !runtime.engine_ready && !runtime.wechat_identity
      ? String(status.history_account || "").trim() : "";
    // A failed probe is not a logout. Keep the displayed account snapshot,
    // while the backend independently refuses unverified reads and mutations.
    const historyAccount = offlineHistory || (accountStatusAvailable
      ? String(runtime.wechat_identity || "").trim() : state.historyAccount);

    if (historyAccount !== state.historyAccount) {
      state.historyAccount = historyAccount;
      invalidateHistoryState();
      invalidateScheduledContext();
      if (state.activePage === "schedules") void loadSchedules();
      if (state.activePage === "history") {
        renderHistoryAccountGate(historyAccount);
        if (historyAccount) void loadConversations({silent: true, refreshGroups: true});
      }
    }
    setHistoryActionsEnabled(
      (wechatLoggedIn || Boolean(offlineHistory)) && state.historyReadyAccount === historyAccount
    );

    // Details prefetched while the engine is starting can carry a stale
    // offline flag. Once the run center confirms the robot is online, force
    // the visible feature to read its live state again without touching a
    // user's unsaved draft.
    if (
      !wasOnline
      && online
      && state.activePage === "features"
      && state.selectedFeature
      && !state.featureDirty
      && !state.featureSaving
      && !$("#featurePageFrame")?.getAttribute("src")
    ) {
      state.featurePrefetch += 1;
      state.featureDetails = {};
      state.featuresEngineOnline = true;
      void openFeature(state.selectedFeature, false, true);
    }

    $("#stateTitle").textContent = confirmingConnection ? "正在确认连接" : status.title;
    $("#stateMessage").textContent = current === "STOP_WAIT_WECHAT"
      ? "关闭微信后会自动完成停止，也可再次点击“停止运行”立即结束机器人服务。"
      : confirmingConnection ? "微信连接状态暂时无法确认，正在重新检测。" : status.message;
    $("#accountText").textContent = !accountStatusAvailable ? "正在确认微信连接…"
      : runtime.wechat_identity ? `已登录：${runtime.wechat_identity}` : "尚未连接";
    $("#lastCheckText").textContent = `检查于 ${new Date().toLocaleTimeString("zh-CN", {hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false})}`;

    const global = $("#globalStatus");
    global.className = `status-badge ${online ? "online" : problem ? "error" : waiting ? "warning" : "checking"}`;
    global.innerHTML = `<span></span>${escapeHtml(online ? "机器人可用" : problem ? "需要处理" : confirmingConnection ? "正在确认连接" : waiting ? status.title : active ? "正在启动" : environment.ready ? "等待启动" : "环境未就绪")}`;
    const sideDot = $("#sideHealthDot");
    sideDot.className = `health-dot ${online ? "online" : problem ? "error" : waiting || !environment.ready ? "warning" : ""}`;
    $("#sideHealthText").textContent = online ? "机器人在线" : problem ? "需要处理" : confirmingConnection ? "正在确认连接" : waiting ? "等待操作" : active ? (current === "STOPPING" ? "正在停止" : "正在启动") : "当前未运行";

    const orbit = $("#statusOrbit");
    orbit.dataset.state = online ? "online" : problem ? "error" : waiting || confirmingConnection ? "waiting" : status.busy || active ? "starting" : "stopped";
    const homeStatusIcon = $("#homeStatusIcon");
    if (homeStatusIcon) {
      homeStatusIcon.className = `home-status-icon ${problem ? "problem" : waiting ? "waiting" : online ? "online" : "checking"}`;
      homeStatusIcon.textContent = online ? "✓" : problem ? "!" : waiting ? "!" : "…";
    }
    const primary = $("#primaryAction");
    const stopMode = !status.can_start && (status.can_stop || online);
    const primaryLabel = starting ? "启动中…" : stopMode ? "停止运行" : "启动";
    primary.innerHTML = `${icon(starting ? "i-refresh" : stopMode ? "i-stop" : "i-play")}<span>${primaryLabel}</span>`;
    primary.dataset.action = stopMode ? "stop" : "start";
    primary.disabled = Boolean(status.busy)
      || (!status.can_start && !status.can_stop)
      || (state.mandatoryUpdate && !stopMode);
    primary.setAttribute("aria-busy", String(Boolean(status.busy)));
    primary.setAttribute("aria-label", primaryLabel);
    primary.title = current === "STOP_WAIT_WECHAT" ? "立即停止机器人，保留微信窗口"
      : stopMode ? "安全停止机器人" : primaryLabel;
    const stopRetainedConnection = $("#stopRetainedConnection");
    const canStopRetainedConnection = licenseWaiting && status.can_start && status.can_stop && !status.busy;
    stopRetainedConnection.hidden = !canStopRetainedConnection;
    stopRetainedConnection.disabled = !canStopRetainedConnection;

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
    setStage($("#stageCore"), coreStarted ? "done" : active ? "current" : "", coreStarted ? "已就绪" : confirmingConnection ? "状态待确认" : active ? "启动中" : "未启动");
    const connectionStarting = current === "CONNECTION_STARTING";
    const connectionMode = online || wechatLoggedIn ? "done" : confirmingConnection || waiting || connectionStarting ? "current" : problem && coreStarted ? "problem" : "";
    setStage($("#stageConnection"), connectionMode, online ? "已连接" : confirmingConnection ? "确认中" : wechatLoggedIn ? "已登录" : connectionStarting ? "正在打开" : waiting ? "连接中" : problem && coreStarted ? "未完成" : "未连接");

    $("#signalWechat").textContent = environment.ready ? "版本兼容" : "未就绪";
    const wechatConnection = !accountStatusAvailable ? "状态待确认" : runtime.hook_ready || wechatLoggedIn ? "已连接" : "待连接";
    $("#signalWechatDetail").textContent = environment.ready ? `${environment.version || "已检测"} · ${wechatConnection}` : "请安装或重新选择微信";
    $("#signalCore").textContent = runtime.engine_ready ? "运行正常" : confirmingConnection ? "状态待确认" : active ? "正在启动" : "未启动";
    $("#signalMessage").textContent = online ? "通信正常" : confirmingConnection ? "正在确认连接" : wechatLoggedIn ? "已登录，功能启动中" : runtime.connection_ready ? "正在确认" : "未连接";
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
    if (action === "stop" && ["STOP_WAIT_WECHAT", "LICENSE_READY", "LICENSE_PAUSED"].includes(state.status?.state)) {
      runForceStop();
      return;
    }
    button.disabled = true;
    if (action === "start" && !await subscription.beforeStart()) { renderStatus(state.status); return; }
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
      title: "停止运行？",
      message: "将立即结束机器人功能和消息连接，保留微信窗口。正在处理的消息可能会中断。",
      confirmText: "停止运行",
      pendingText: "正在停止…",
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
    home: "运行中心", features: "功能中心", groups: "群聊&私聊配置", history: "消息中心",
    schedules: "定时任务", activity: "运行记录", settings: "机器人总设置", help: "帮助", subscription: "订阅与授权",
  };

  const SETTINGS_SECTIONS = new Set(["model", "core", "search", "skin", "system"]);

  async function navigate(page) {
    if (!PAGE_TITLES[page] || state.featureSaving || state.scheduledBusy || coreSettings.busy) return;
    if (page !== state.activePage && coreSettings.guard(() => navigate(page))) return;
    if (page !== state.activePage && state.featureDirty) { discardFeatureChanges(() => navigate(page)); return; }
    if (page !== state.activePage && guardProviderDraft(() => navigate(page))) return;
    if (page !== state.activePage && state.activePage === "schedules" && guardScheduledDraft(() => navigate(page))) return;
    const leavingGroupManagement = state.activePage === "groups" && page !== "groups";
    if (state.activePage === "settings" && page !== "settings") clearProviderTest();
    state.activePage = page;
    if (leavingGroupManagement) {
      window.clearTimeout(state.groupRefreshRetryTimer);
      state.groupRefreshRetryTimer = 0;
      state.groupMemberLoad += 1;
      clearPendingGroupMemberLoading();
    }
    $$(".nav-item").forEach((item) => item.classList.toggle("active", item.dataset.page === page));
    $$(".page").forEach((item) => item.classList.toggle("active", item.id === `page-${page}`));
    $("#pageTitle").textContent = PAGE_TITLES[page];
    $("#defaultTopActions").hidden = page === "schedules";
    $("#scheduleTopActions").hidden = page !== "schedules";
    document.title = `${PAGE_TITLES[page]} · WxRebot`;
    const content = $(".content-scroll");
    if (content) {
      content.classList.toggle("history-active", page === "history");
      content.scrollTop = 0;
    }
    await subscription.onNavigate(page);
    if (page === "features" && !state.featuresLoaded) await loadFeatures();
    if (page === "features" && state.imageServiceChoicesStale) {
      state.imageServiceChoicesStale = false;
      if (state.selectedFeature) await openFeature(state.selectedFeature, false, true);
    }
    if (page === "groups") {
      if (state.chatManagementTab === "groups") {
        if (!state.groupsLoaded) await loadGroups(true);
        if (state.groupInspectorView === "members" && state.selectedGroup
            && !state.groupMembers[state.selectedGroup]) renderGroupInspector();
      }
      if (state.chatManagementTab === "private" && !state.contactsLoaded) await loadContacts(false);
    }
    if (page === "history") {
      // The first visit needs the account gate and initial loading state. Once
      // the account has been read, refresh the retained conversation silently
      // on every return so a long-hidden page catches up immediately without
      // replacing the current messages or scroll position with a loader.
      if (!state.historyAccount || state.historyReadyAccount !== state.historyAccount) {
        if (!state.historyConversationsLoading) {
          await loadConversations({refreshGroups: true});
        }
      } else if (!state.historyConversationsLoading) {
        void loadConversations({silent: true, refreshGroups: true});
      }
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
      let copied = false;
      try { copied = document.execCommand("copy"); } catch (_copyError) { copied = false; }
      finally { input.remove(); }
      if (!copied) { toast(`复制失败，请手动选择${label}复制。`, "error"); return false; }
    }
    toast(`${label}已复制。`);
    return true;
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
    if (state.activePage !== "history" || state.historyPolling || state.historyLoadingOlder
        || state.historyConversationsLoading || state.historyMessagesLoading) return;
    state.historyPolling = true;
    try {
      await loadConversations({silent: true, refreshGroups: true, forceRefresh: true});
    } finally {
      state.historyPolling = false;
    }
  }

  let modalResolve = null;
  function confirmAction({title, message, subject = null, confirmText = "确认", cancelText = "取消", pendingText = "处理中…", pendingMessage = "", field = null, intent = "danger", onConfirm}) {
    const backdrop = $("#modalBackdrop");
    const cancel = $("#modalCancel");
    const confirm = $("#modalConfirm");
    $("#modalTitle").textContent = title;
    $("#modalSubject").hidden = !subject;
    $("#modalSubjectLabel").textContent = subject?.label || "";
    $("#modalSubjectValue").textContent = subject?.value || "";
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
    $("#modalProgressBar").classList.remove("is-indeterminate");
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

  function readHomeSetupAlertDismissals() {
    try {
      const saved = JSON.parse(window.localStorage.getItem(HOME_SETUP_DISMISSALS_STORAGE_KEY) || "[]");
      return new Set(Array.isArray(saved) ? saved.filter((item) => typeof item === "string") : []);
    } catch (_error) {
      return new Set();
    }
  }

  function renderHomeSetupAlerts() {
    const container = $("#homeSetupAlerts");
    if (!container) return;
    const services = state.bootstrap?.providers;
    if (!Array.isArray(services)) {
      container.replaceChildren();
      container.hidden = true;
      return;
    }
    const configured = services.filter((service) => service?.configured);
    const chatServices = configured.filter((service) => (service.service_kind || "chat") === "chat");
    const hasReplyModel = chatServices.some((service) => service.is_default);
    const hasImageUnderstanding = chatServices.some((service) =>
      Array.isArray(service.modalities) && service.modalities.includes("image"));
    const hasImageModel = configured.some((service) => service.service_kind === "image");
    const hasVectorModel = configured.some((service) => service.service_kind === "embedding");
    const reminders = [
      !hasReplyModel && {
        id: "reply-model",
        priority: 1,
        title: "未配置回复模型",
        message: "配置一个可用的回复模型后，机器人才能回答消息。",
      },
      hasReplyModel && !hasImageUnderstanding && {
        id: "image-understanding",
        priority: 2,
        title: "未配置图片理解（多模态）模型",
        message: "当前没有支持图片输入的聊天模型，机器人无法理解收到的图片。",
      },
      !hasImageModel && {
        id: "image-generation",
        priority: 3,
        title: "未配置生图模型",
        message: "需要图片生成时，请在模型服务中添加图片模型。",
      },
      !hasVectorModel && {
        id: "vector-model",
        priority: 4,
        title: "未配置向量模型",
        message: "向量检索相关功能需要单独配置向量模型。",
      },
    ].filter((item) => item && !state.dismissedHomeSetupAlerts.has(item.id));
    container.innerHTML = reminders.map((item) => `
      <article class="home-setup-alert" data-priority="${item.priority}">
        <span class="home-setup-alert-icon" aria-hidden="true">!</span>
        <div class="home-setup-alert-copy"><strong>${escapeHtml(item.title)}</strong><p>${escapeHtml(item.message)}</p></div>
        <div class="home-setup-alert-actions">
          <button class="button secondary small" type="button" data-home-setup-configure>去配置</button>
          <button class="button ghost small" type="button" data-home-setup-dismiss="${escapeHtml(item.id)}">不再提示</button>
        </div>
      </article>`).join("");
    container.hidden = reminders.length === 0;
    $$('[data-home-setup-configure]', container).forEach((button) => button.addEventListener("click", async () => {
      await navigate("settings");
      if (state.activePage === "settings") selectSettingsSection("model");
    }));
    $$('[data-home-setup-dismiss]', container).forEach((button) => button.addEventListener("click", () => {
      state.dismissedHomeSetupAlerts.add(button.dataset.homeSetupDismiss);
      try {
        window.localStorage.setItem(
          HOME_SETUP_DISMISSALS_STORAGE_KEY,
          JSON.stringify([...state.dismissedHomeSetupAlerts]),
        );
      } catch (_error) { /* Keep the dismissal for this window if storage is unavailable. */ }
      renderHomeSetupAlerts();
    }));
  }

  function applyBrand(brand) {
    $("#brandName").textContent = "WxRebot";
    const mark = $("#brandMark");
    if (mark && !mark.querySelector("img")) mark.innerHTML = '<img src="assets/app-icon.png" alt="">';
    document.title = "WxRebot · 机器人管理";
  }

  function updateToggleCopy(event) {
    const input = event.currentTarget;
    const label = input.closest(".toggle")?.querySelector("span:last-child");
    if (!label) return;
    if (input.name === "scheduled_enabled") label.textContent = input.checked ? "已允许" : "已暂停";
    else if (input.name === "bot_enabled") label.textContent = input.checked ? "已启用" : "已停用";
    else label.textContent = input.checked ? "已启用" : "已关闭";
  }

  function updatePolicyToggle(event) {
    updateToggleCopy(event);
    const input = event.currentTarget;
    if (input.name !== "quiet_enabled") return;
    const fields = $(".quiet-hours-fields", input.form);
    if (fields) fields.hidden = !input.checked;
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
    initializeTheme();
    window.addEventListener("manager-skin-change", syncWindowTheme);
    window.initializePanelLayout();
    window.initializeHistoryDateRange(() => loadConversations({latest: true}));
    // Keep the browser-native calendar/clock as the single source of truth,
    // while making the whole field open it on a direct click.
    document.addEventListener("click", (event) => {
      const input = event.target.closest?.("input[type=date], input[type=time]");
      if (!input || input.disabled || typeof input.showPicker !== "function") return;
      try { input.showPicker(); } catch (_error) { /* browser may already have opened it */ }
    });
    $$(".nav-item").forEach((item) => item.addEventListener("click", () => navigate(item.dataset.page)));
    $$('[data-home-page]').forEach((item) => item.addEventListener("click", () => navigate(item.dataset.homePage)));
    $("#themeToggle").addEventListener("click", async (event) => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        const theme = state.theme === "dark" ? "light" : "dark";
        await api("save_theme", theme);
        applyTheme(theme, true);
      } catch (error) {
        reportError(error);
      } finally {
        button.disabled = false;
      }
    });
    $("#primaryAction").addEventListener("click", runPrimaryAction);
    $("#stopRetainedConnection").addEventListener("click", runForceStop);
    $("#retryEnvironment").addEventListener("click", checkEnvironment);
    $("#settingsCheckWechat").addEventListener("click", checkEnvironment);
    $("#chooseWechat").addEventListener("click", chooseWechat);
    $("#settingsChooseWechat").addEventListener("click", chooseWechat);
    $("#downloadWechat").addEventListener("click", async () => { try { await api("open_wechat_download"); } catch (error) { reportError(error); } });
    bindSearch("#featureSearchBox", "#featureSearch", renderFeatureList);
    $("#refreshFeatures").addEventListener("click", refreshFeatures);
    $$("#featureAudience button").forEach((button) => button.addEventListener("click", () => {
      const changed = state.featureAudience !== button.dataset.audience;
      state.featureAudience = button.dataset.audience;
      $$("#featureAudience button").forEach((item) => item.classList.toggle("active", item === button));
      renderFeatureList();
      if (changed) window.managerMotion?.enter($("#featureList"));
    }));
    bindSearch("#groupSearchBox", "#groupSearch", renderGroups);
    bindSearch("#contactSearchBox", "#contactSearch", () => { state.contactPage = 1; renderContacts(); });
    bindSearch("#historySearchBox", "#historySearch", loadConversations);
    bindSearch("#helpSearchBox", "#helpSearch", renderHelp);
    $$("#helpAudience button").forEach((button) => {
      button.addEventListener("click", () => {
        const changed = state.helpAudience !== button.dataset.helpAudience;
        state.helpAudience = button.dataset.helpAudience;
        $$("#helpAudience button").forEach((item) => item.classList.toggle("active", item === button));
        renderHelp();
        if (changed) window.managerMotion?.enter($("#helpFeatureGrid"));
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
      const changed = state.historyKind !== button.dataset.kind;
      state.historyKind = button.dataset.kind;
      $$("#historyKinds button").forEach((item) => item.classList.toggle("active", item === button));
      loadConversations({localFilter: true, animate: changed});
    }));
    $("#historyFilter").addEventListener("click", () => loadConversations({forceRefresh: true}));
    $("#refreshHistory").addEventListener("click", refreshHistoryRecords);
    $("#newMessageButton").addEventListener("click", () => {
      if (state.historyHasNewer && state.selectedConversation) {
        void selectConversation(state.selectedConversation.conversation_id, {latest: true});
        return;
      }
      const list = $("#messageList");
      list.scrollTop = list.scrollHeight;
      $("#newMessageButton").hidden = true;
    });
    $("#messageList").addEventListener("scroll", () => {
      const list = $("#messageList");
      updateHistoryMessageScrollPosition();
      if (list.scrollTop <= HISTORY_PRELOAD_THRESHOLD) void loadOlderMessages();
      if (!state.historyHasNewer && list.scrollHeight - list.scrollTop - list.clientHeight < 80) $("#newMessageButton").hidden = true;
    });
    $("#exportHistory").addEventListener("click", async () => {
      const accountId = state.historyAccount;
      try {
        const result = await api("export_history", "csv", accountId);
        if (result?.requires_login || result?.account_changed || accountId !== state.historyAccount) {
          invalidateHistoryState();
          renderHistoryAccountGate(state.historyAccount);
          return;
        }
        if (!result?.cancelled) toast(`已导出到 ${result.path}`);
      } catch (error) { reportError(error); }
    });
    $("#refreshSchedules").addEventListener("click", loadSchedules);
    $("#newScheduledTask").addEventListener("click", () => openScheduledEditor());
    ["#scheduledActionFilter", "#scheduledTargetFilter", "#scheduledStatusFilter"].forEach((selector) => $(selector).addEventListener("change", renderScheduledTasks));
    $("#clearScheduledChatFilter").addEventListener("click", () => { state.scheduledChatFilter = null; renderScheduledTasks(); });
    $("#manageGroupPolicies").addEventListener("click", async () => { await selectChatManagementTab("groups"); await navigate("groups"); });
    $("#refreshActivity").addEventListener("click", loadActivity);
    $("#generateResponseTrace").addEventListener("click", generateResponseTrace);
    $$("#settingsTabs [data-settings-tab]").forEach((button) => button.addEventListener("click", () => selectSettingsSection(button.dataset.settingsTab)));
    coreSettings.init();
    $("#openChatRules").addEventListener("click", () => navigate("groups"));
    $("#webSearchForm").addEventListener("submit", saveWebSearchSettings);
    $("#webSearchProvider").addEventListener("change", () => {
      $("#webSearchKey").value = "";
      state.webSearchClearKey = false;
      updateWebSearchProviderCopy();
    });
    $("#webSearchForm").addEventListener("input", () => { state.webSearchDirty = true; });
    $("#webSearchForm").addEventListener("change", (event) => {
      state.webSearchDirty = true;
      if (event.target.name === "enabled") {
        updateToggleCopy({currentTarget: event.target});
        $("#webSearchDetails").hidden = !event.target.checked;
        $("#clearWebSearchKey").hidden = !event.target.checked;
        $("#webSearchNote").textContent = "有修改尚未保存。";
      }
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
      state.webSearchDirty = true;
      $("#webSearchNote").textContent = "当前服务商密钥将在保存时清除。";
    });
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
    $("#providerServiceKind").addEventListener("change", (event) => {
      const preset = $("#providerPreset").value;
      const kind = event.currentTarget.value;
      if (kind === "chat" && ["embedding_custom", "image_custom"].includes(preset)) {
        $("#providerPreset").value = "custom";
        applyProviderPreset("custom");
        updateProviderDirty();
        return;
      }
      if (kind !== "chat") {
        const matching = kind === "embedding" ? "embedding_custom" : kind === "image" ? "image_custom" : "voice_custom";
        $("#providerPreset").value = matching;
        applyProviderPreset(matching);
      }
      updateProviderDirty();
    });
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
      if (create) { beginNewProvider("deepseek"); return; }
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
    $("#openOnboarding").addEventListener("click", () => { showOnboarding(true); });
    $("#cleanHistory").addEventListener("click", () => {
      const accountId = state.historyAccount;
      if (!canEditHistory(accountId)) return;
      confirmAction({
        title: "按日期清理聊天记录",
        message: "将删除所选日期以前的本机记录副本，此操作无法撤销。",
        confirmText: "开始清理",
        field: {label: "保留此日期及之后的记录", type: "date", value: ""},
        onConfirm: async (value) => {
          if (!canEditHistory(accountId)) { toast("请连接原微信账号后再清理记录。"); return; }
          const result = await api("delete_history_before", value, accountId);
          if (result.requires_login || result.account_changed || accountId !== state.historyAccount) {
            invalidateHistoryState();
            renderHistoryAccountGate(state.historyAccount);
            return;
          }
          toast(`已清理 ${result.deleted} 条记录。 `);
          // Cleanup is opened from settings: refresh the retained records even
          // while the history page is hidden, including its directory snapshot.
          invalidateHistoryState();
          $("#messageList").innerHTML = '<div class="loading-row message-read-status"><span>正在读取消息…</span><small>若长时间未读到记录，点击刷新。</small></div>';
          await loadConversations({latest: true, forceRefresh: true});
        },
      });
    });
    $("#checkUpdate").addEventListener("click", () => refreshUpdate(true));
    $("#modalCancel").addEventListener("click", () => modalResolve?.(false));
    window.addEventListener("beforeunload", (event) => { if (state.featureDirty || state.featureSaving || state.providerDirty || state.scheduledDirty || coreSettings.dirty || coreSettings.busy || hasGroupDrafts()) { event.preventDefault(); event.returnValue = ""; } });
    window.addEventListener("focus", () => { refreshVisibleFeatureState().catch(reportError); });
    window.addEventListener("focus", () => { refreshVisibleChatData().catch(reportError); });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState === "visible") refreshVisibleChatData().catch(reportError);
    });
    $("#modalBackdrop").addEventListener("keydown", (event) => {
      if (event.key !== "Tab") return;
      const targets = $$("button:not(:disabled), input:not(:disabled), select:not(:disabled)", $("#modalBackdrop"));
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

  function showStartupFailure() {
    $("#stateTitle").textContent = "软件未能完成初始化";
    $("#stateMessage").textContent = "请重新打开软件；若问题持续，请联系技术支持。 ";
    $("#globalStatus").className = "status-badge error";
    $("#globalStatus").innerHTML = "<span></span>初始化失败";
    $("#sideHealthDot").className = "health-dot error";
    $("#sideHealthText").textContent = "初始化失败";
    $("#homeStatusIcon").className = "home-status-icon problem";
    $("#homeStatusIcon").textContent = "!";
    $("#primaryAction").disabled = true;
  }

  let startupFailure = false;
  function createPageController(name, helpers) {
    try {
      if (typeof window[name] !== "function") throw new Error("主界面资源缺失，请修复安装。");
      return window[name](helpers);
    } catch (error) {
      startupFailure = true;
      reportError(error);
      showStartupFailure();
      return {};
    }
  }

  async function initialize() {
    try {
      initializeControllers();
      bindEvents();
      await initializeSkins();
      setSettingsSection("model");
      state.bootstrap = await api("bootstrap");
      fillSettings(state.bootstrap);
      renderStatus(state.bootstrap.status);
      await subscription.initialize();
      const previewParams = new URLSearchParams(window.location.search);
      if (subscription.needsEntry() || (previewParams.has("preview") && previewParams.get("demo") === "subscription")) {
        await navigate("subscription");
      } else if (previewParams.has("preview") && (previewParams.get("demo") === "model-services" || previewParams.get("demo") === "thinking-mode")) {
        state.bootstrap.experience.onboarding_complete = true;
        await navigate("settings");
      } else {
        await showOnboarding();
      }
      window.setInterval(refreshStatus, 1500);
      window.setInterval(pollUpdateStatus, 1200);
      window.setInterval(pollHistory, 1500);
      window.setInterval(pollGroups, 15000);
      if (state.bootstrap.update?.enabled) scheduleAutomaticUpdateCheck(1800);
    } catch (error) {
      reportError(error);
      showStartupFailure();
    }
  }

  // Assemble existing page controllers before attaching the application lifecycle.
  const { configFromForm, discardFeatureChanges, growTextarea, loadFeatures, openFeature, refreshFeatures, refreshVisibleFeatureState, renderFeatureList, renderHelp, schemaMarkup } = createPageController("createFeatureController", {
    $,
    $$,
    api: (...args) => api(...args),
    confirmAction: (...args) => confirmAction(...args),
    escapeHtml,
    guardProviderDraft: (...args) => guardProviderDraft(...args),
    icon: (...args) => icon(...args),
    loadGroups: (...args) => loadGroups(...args),
    navigate: (...args) => navigate(...args),
    openScheduledFromFeature: (...args) => openScheduledFromFeature(...args),
    reportError: (...args) => reportError(...args),
    setSettingsSection: (...args) => setSettingsSection(...args),
    showProviderOverview: (...args) => showProviderOverview(...args),
    state,
    syncEmbeddedTheme: (...args) => syncEmbeddedTheme(...args),
    toast: (...args) => toast(...args),
    getCoreSettings: () => coreSettings,
  });

  const contactProfile = createPageController("createContactProfile", {
    api: (...args) => api(...args), avatarMarkup, bindAvatarFallbacks, copyText, escapeHtml, state,
  });

  const { clearPendingGroupMemberLoading, hasGroupDrafts, loadContacts, loadGroups, renderContacts, renderGroupInspector, renderGroups, selectChatManagementTab } = createPageController("createChatManagement", {
    $,
    $$,
    CONTACT_PAGE_SIZE,
    GROUP_DIRECTORY_RETRY_DELAYS_MS,
    GROUP_MEMBER_COUNTS_STORAGE_PREFIX,
    GROUP_MEMBER_REQUEST_CONCURRENCY,
    GROUP_MEMBER_RETRY_DELAYS_MS,
    openContactProfile: (...args) => contactProfile.open(...args),
    contactFactsMarkup: (...args) => contactProfile.factsMarkup(...args),
    api: (...args) => api(...args),
    avatarMarkup: (...args) => avatarMarkup(...args),
    bindAvatarFallbacks: (...args) => bindAvatarFallbacks(...args),
    confirmAction: (...args) => confirmAction(...args),
    copyText: (...args) => copyText(...args),
    escapeHtml,
    fullTime: (...args) => fullTime(...args),
    icon: (...args) => icon(...args),
    openCoreTemplates: async () => {
      await navigate("settings");
      if (state.activePage !== "settings") return;
      selectSettingsSection("core");
      if (state.settingsSection !== "core") return;
      $("#coreTemplates").open = true;
      $("#coreTemplates").scrollIntoView({block: "nearest"});
    },
    openScheduledForChat: (...args) => openScheduledForChat(...args),
    readHomeSetupAlertDismissals: (...args) => readHomeSetupAlertDismissals(...args),
    reportError: (...args) => reportError(...args),
    state,
    toast: (...args) => toast(...args),
    updatePolicyToggle: (...args) => updatePolicyToggle(...args),
  });

  const { canEditHistory, invalidateHistoryState, loadConversations, loadOlderMessages, pollHistory, refreshHistoryRecords, renderHistoryAccountGate, selectConversation, setHistoryActionsEnabled, updateHistoryMessageScrollPosition } = createPageController("createMessageHistory", {
    $,
    $$,
    HISTORY_MESSAGE_CACHE_LIMIT,
    HISTORY_MESSAGE_CACHE_TTL_MS,
    HISTORY_MESSAGE_WINDOW_LIMIT,
    HISTORY_PAGE_SIZE,
    HISTORY_PRELOAD_THRESHOLD,
    openContactProfile: (...args) => contactProfile.open(...args),
    api: (...args) => api(...args),
    avatarMarkup: (...args) => avatarMarkup(...args),
    bindAvatarFallbacks: (...args) => bindAvatarFallbacks(...args),
    confirmAction: (...args) => confirmAction(...args),
    copyText: (...args) => copyText(...args),
    escapeHtml,
    fullTime: (...args) => fullTime(...args),
    reportError: (...args) => reportError(...args),
    state,
    toast: (...args) => toast(...args),
  });

  const { SCHEDULED_ACTIONS, guardScheduledDraft, invalidateScheduledContext, loadSchedules, openScheduledEditor, openScheduledForChat, openScheduledFromFeature, renderScheduledEditor, renderScheduledTasks, scheduledTimeText } = createPageController("createScheduledTasks", {
    $,
    $$,
    api: (...args) => api(...args),
    confirmAction: (...args) => confirmAction(...args),
    debounce: (...args) => debounce(...args),
    discardFeatureChanges: (...args) => discardFeatureChanges(...args),
    escapeHtml,
    icon: (...args) => icon(...args),
    navigate: (...args) => navigate(...args),
    openFeature: (...args) => openFeature(...args),
    reportError: (...args) => reportError(...args),
    shortTime: (...args) => shortTime(...args),
    state,
    toast: (...args) => toast(...args),
  });

  const { applyProviderPreset, beginNewProvider, bindProviderModelCombobox, clearProviderTest, deleteProvider, deleteSelectedProvider, discoverProviderModels, editProvider, guardProviderDraft, inferProviderModalities, providerCapabilityLabel, providerThinkingSupported, renderProviderServices, requestProviderOverview, saveProvider, setProviderDefault, setProviderThinking, showProviderOverview, syncProviderThinkingControl, testSelectedProvider, updateProviderDirty } = createPageController("createModelServices", {
    $,
    $$,
    api: (...args) => api(...args),
    confirmAction: (...args) => confirmAction(...args),
    escapeHtml,
    icon: (...args) => icon(...args),
    renderHomeSetupAlerts: (...args) => renderHomeSetupAlerts(...args),
    reportError: (...args) => reportError(...args),
    requiredValues: (...args) => requiredValues(...args),
    state,
    toast: (...args) => toast(...args),
    getCoreSettings: () => coreSettings,
  });

  const { chooseRestoreBackup, closeOnboarding, createBackup, exportDiagnostics, fillSettings, formatBytes, initializeSkins, loadMaintenance, openMemoryManager, saveWebSearchSettings, selectSettingsSection, setSettingsSection, showOnboarding, updateWebSearchProviderCopy } = createPageController("createManagerSettings", {
    $,
    $$,
    SETTINGS_SECTIONS,
    api: (...args) => api(...args),
    applyBrand: (...args) => applyBrand(...args),
    clearProviderTest: (...args) => clearProviderTest(...args),
    confirmAction: (...args) => confirmAction(...args),
    escapeHtml,
    fullTime: (...args) => fullTime(...args),
    guardProviderDraft: (...args) => guardProviderDraft(...args),
    loadFeatures: (...args) => loadFeatures(...args),
    navigate: (...args) => navigate(...args),
    openFeature: (...args) => openFeature(...args),
    renderProviderServices: (...args) => renderProviderServices(...args),
    reportError: (...args) => reportError(...args),
    safeImageUrl: (...args) => safeImageUrl(...args),
    showProviderOverview: (...args) => showProviderOverview(...args),
    state,
    toast: (...args) => toast(...args),
    updateToggleCopy: (...args) => updateToggleCopy(...args),
    getCoreSettings: () => coreSettings,
  });

  const { generateResponseTrace, loadActivity } = createPageController("createActivityView", {
    $,
    api: (...args) => api(...args),
    copyText: (...args) => copyText(...args),
    escapeHtml,
    fullTime: (...args) => fullTime(...args),
    reportError: (...args) => reportError(...args),
    state,
  });

  const { pollUpdateStatus, refreshUpdate, scheduleAutomaticUpdateCheck } = createPageController("createUpdateNotifications", {
    $,
    api: (...args) => api(...args),
    confirmAction: (...args) => confirmAction(...args),
    formatBytes: (...args) => formatBytes(...args),
    renderStatus: (...args) => renderStatus(...args),
    reportError: (...args) => reportError(...args),
    state,
    toast: (...args) => toast(...args),
  });

  const { previewApi } = createPageController("createPreviewApi", {
    $,
    HISTORY_PAGE_SIZE,
    SCHEDULED_ACTIONS,
    api: (...args) => api(...args),
    inferProviderModalities: (...args) => inferProviderModalities(...args),
    providerCapabilityLabel: (...args) => providerCapabilityLabel(...args),
    providerThinkingSupported: (...args) => providerThinkingSupported(...args),
    scheduledTimeText: (...args) => scheduledTimeText(...args),
    state,
  });

  if (startupFailure) return;

  if (new URLSearchParams(window.location.search).has("preview")) {
    window.pywebview = {api: previewApi()};
    window.addEventListener("DOMContentLoaded", initialize, {once: true});
  } else {
    window.addEventListener("pywebviewready", initialize, {once: true});
  }
})();
