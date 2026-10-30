/* Runtime reminders use manager-owned session and durable preferences. */
window.createRuntimeAlerts = function (helpers) {
  "use strict";
  const { $, api, confirmAction, copyText, escapeHtml, navigate, openModels, refreshRuntime, reportError, state } = helpers;
  const refreshInterval = 15000;
  const operationTime = new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  });
  let snapshot = null;
  let loading = false;
  let saving = false;
  let bound = false;
  let lastRead = 0;
  let listMarkup = "";
  let preferenceMarkup = "";
  let preferenceRevision = 0;

  function timeText(value) {
    if (!value) return "时间未记录";
    const date = typeof value === "number" ? new Date(value * 1000) : new Date(value);
    return Number.isNaN(date.getTime()) ? "时间未记录" : operationTime.format(date);
  }

  async function request(method, ...args) {
    let timer;
    try {
      return await Promise.race([
        api(method, ...args),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error("运行提醒接口等待超时，请刷新核对。")), 12000); }),
      ]);
    } finally { clearTimeout(timer); }
  }

  function applySnapshot(value) {
    if (!value || !Array.isArray(value.items) || !Array.isArray(value.categories) || !Array.isArray(value.closed)) {
      throw new Error("运行提醒返回格式无效，请刷新重试。");
    }
    snapshot = value;
    render();
  }

  function itemMarkup(item) {
    const id = escapeHtml(item.id);
    const category = escapeHtml(item.category);
    const level = item.level === "error" ? "error" : "warning";
    const active = item.state === "active";
    return `<article class="runtime-reminder ${level}" data-runtime-alert="${id}">
      <button class="runtime-reminder-close" type="button" data-runtime-reminder-action="close" data-alert-id="${id}" data-category="${category}" aria-label="关闭本次提醒：${escapeHtml(item.title)}" title="本次关闭，完全退出并重新打开软件后恢复提醒">×</button>
      <div class="runtime-reminder-main">
        <span class="runtime-reminder-icon" aria-hidden="true">!</span>
        <div class="runtime-reminder-copy"><div class="runtime-reminder-title"><h3>${escapeHtml(item.title)}</h3><span>${level === "error" ? "运行异常" : "功能异常"}</span></div>
          <p>${escapeHtml(item.message)}</p>
          <div class="runtime-reminder-meta"><time>最近发生 ${escapeHtml(timeText(item.timestamp ?? item.time))}</time><span>同类记录 ${escapeHtml(item.count || 1)} 次</span><span>${active ? "需要处理" : "恢复待确认"}</span></div>
        </div>
        <div class="runtime-reminder-actions"><button class="button secondary small" type="button" data-runtime-reminder-action="handle" data-alert-id="${id}">${escapeHtml(item.action_label || "查看运行记录")}</button><button class="runtime-reminder-mute" type="button" data-runtime-reminder-action="mute" data-alert-id="${id}" data-category="${category}" title="永久关闭此类提醒，可在提醒设置中恢复">不再提醒 <span>永久</span></button></div>
      </div>
      <details class="runtime-reminder-detail" data-runtime-detail="${id}"><summary>查看详情</summary><div><p>${escapeHtml(item.detail || item.message)}</p>${!active ? "<p>该条来自近期异常记录，当前是否恢复仍待确认。</p>" : ""}<button class="button secondary small" type="button" data-runtime-reminder-action="copy" data-alert-id="${id}">复制问题信息</button><pre tabindex="0">${escapeHtml(item.report || item.detail || item.message)}</pre></div></details>
    </article>`;
  }

  function preferenceRows() {
    const titles = new Map(snapshot.categories.map((category) => [category.id, category.title]));
    const temporary = snapshot.closed.map((item) => `<article class="runtime-reminder-choice"><div><strong>${escapeHtml(titles.get(item.category) || "运行提醒")}</strong><p>关闭于 ${escapeHtml(timeText(item.closed_at))}（北京时间）</p><p>本次软件会话内有效；完全退出并重新打开后，仍需关注的问题再次提醒。</p></div><button class="button secondary small" type="button" data-runtime-reminder-action="reopen" data-category="${escapeHtml(item.category)}" data-alert-id="${escapeHtml(item.id)}">撤销本次关闭</button></article>`);
    const permanent = snapshot.categories.filter((category) => category.muted_at).map((category) => `<article class="runtime-reminder-choice"><div><strong>${escapeHtml(category.title)}</strong><p>设置于 ${escapeHtml(timeText(category.muted_at))}（北京时间）</p><p>永久不再提醒；之后发生同类问题也不提醒，直到手动恢复。</p></div><button class="button secondary small" type="button" data-runtime-reminder-action="restore" data-category="${escapeHtml(category.id)}">恢复此类提醒</button></article>`);
    return [...temporary, ...permanent].join("") || '<p class="runtime-reminder-empty">当前没有已关闭或永久屏蔽的提醒。</p>';
  }

  function render() {
    if (!snapshot) return;
    const visible = snapshot.items.filter((item) => !item.hidden);
    const hidden = snapshot.items.length - visible.length;
    const muted = snapshot.categories.filter((category) => category.muted_at).length;
    $("#runtimeAlerts").hidden = !snapshot.items.length && !snapshot.closed.length && !muted;
    $("#runtimeAlertCount").textContent = visible.length ? `需关注 ${visible.length}` : "暂无可见提醒";
    $("#runtimeAlertCount").classList.toggle("is-muted", !visible.length);
    $("#runtimeAlertWindow").textContent = `最近 ${Math.max(1, Math.round((snapshot.window_seconds || 3600) / 3600))} 小时异常`;
    const markup = visible.map(itemMarkup).join("");
    if (markup !== listMarkup) {
      const list = $("#runtimeAlertList");
      const openIds = new Set([...list.querySelectorAll("details[open]")].map((node) => node.dataset.runtimeDetail));
      const content = $(".content-scroll");
      const scrollTop = content?.scrollTop;
      list.innerHTML = markup;
      for (const detail of list.querySelectorAll("details")) detail.open = openIds.has(detail.dataset.runtimeDetail);
      if (content && scrollTop !== undefined) content.scrollTop = scrollTop;
      listMarkup = markup;
    }
    $("#runtimeAlertHidden").hidden = !hidden && !snapshot.closed.length && !muted;
    $("#runtimeAlertHidden").textContent = `${hidden ? `已隐藏 ${hidden} 条当前提醒。` : ""}${snapshot.closed.length ? `本次关闭 ${snapshot.closed.length} 条；` : ""}${muted ? `永久屏蔽 ${muted} 类；` : ""}可在提醒设置中查看操作时间和恢复提醒。异常记录继续保留。`;
    const rows = preferenceRows();
    if (rows !== preferenceMarkup) {
      $("#runtimeAlertPreferenceList").innerHTML = rows;
      preferenceMarkup = rows;
    }
    setBusy(saving);
  }

  function setBusy(value) {
    saving = value;
    for (const button of $("#runtimeAlerts").querySelectorAll("button")) button.disabled = value;
  }

  function feedback(message, error = false) {
    const node = $("#runtimeAlertFeedback");
    node.textContent = message;
    node.hidden = !message;
    node.classList.toggle("error", error);
    if (error) $("#runtimeAlerts").hidden = false;
  }

  async function save(category, action, id = "") {
    if (saving) throw new Error("提醒设置正在保存，请稍候。");
    preferenceRevision++;
    setBusy(true);
    try {
      applySnapshot(await request("set_runtime_alert_preference", category, action, id));
      const stillMuted = snapshot.categories.some((entry) => entry.id === category && entry.muted_at);
      const stillClosed = snapshot.closed.some((entry) => entry.category === category);
      feedback({close: "已关闭本次提醒；完全退出并重新打开软件后，仍需关注的问题再次提醒。", mute: "已永久关闭此类提醒，可在提醒设置中恢复。", reopen: `已撤销本次关闭。${stillMuted ? "此类问题仍遵循永久屏蔽设置。" : ""}`, restore: `已恢复此类提醒。${stillClosed ? "已关闭的本次提醒仍在本次软件会话内隐藏。" : ""}`}[action]);
    } finally { setBusy(false); }
  }

  async function handleItem(item) {
    if (item.action === "models") await openModels();
    else if (item.action === "schedules") await navigate("schedules");
    else if (item.action === "status") { await refreshRuntime(); $("#stateTitle").scrollIntoView({block: "nearest"}); }
    else await navigate("activity");
  }

  async function onAction(event) {
    const button = event.target.closest("[data-runtime-reminder-action]");
    if (!button || saving) return;
    const { category, alertId = "", runtimeReminderAction: action } = button.dataset;
    const item = snapshot?.items.find((entry) => entry.id === alertId);
    if (action === "mute") {
      confirmAction({
        title: "不再提醒此类问题？", subject: {label: "提醒类别", value: snapshot.categories.find((entry) => entry.id === category)?.title || item?.title || "运行提醒"},
        message: "今后发生同类问题也不再提醒，重新打开软件后仍然有效。异常记录继续保留，可在运行中心的“提醒设置”中恢复。",
        confirmText: "永久不再提醒", pendingText: "正在保存…", intent: "primary", onConfirm: async () => {
          await save(category, "mute", alertId);
          setTimeout(() => $("#runtimeAlertSettingsToggle").focus(), 0);
        },
      });
      return;
    }
    try {
      if (["close", "reopen", "restore"].includes(action)) {
        await save(category, action, alertId);
        $("#runtimeAlertSettingsToggle").focus();
      } else if (action === "handle" && item) await handleItem(item);
      else if (action === "copy" && item) {
        const copied = await copyText(item.report || item.detail || item.message, "问题信息");
        feedback(copied === false ? "复制失败，请展开详情手动选择问题信息。" : "问题信息已复制。", copied === false);
      }
    } catch (error) {
      feedback(error.product?.message || error.message || "提醒设置未能保存，请刷新核对。", true);
      reportError(error);
    }
  }

  function bind() {
    if (bound) return;
    bound = true;
    $("#runtimeAlerts").addEventListener("click", onAction);
    $("#runtimeAlertSettingsToggle").addEventListener("click", () => {
      const panel = $("#runtimeAlertPreferences");
      panel.hidden = !panel.hidden;
      $("#runtimeAlertSettingsToggle").setAttribute("aria-expanded", String(!panel.hidden));
    });
    $("#runtimeAlertRecords").addEventListener("click", () => { void navigate("activity").catch(reportError); });
    $("#runtimeAlertRefresh").addEventListener("click", () => { void loadRuntimeAlerts({force: true}); });
  }

  async function loadRuntimeAlerts(options = {}) {
    bind();
    if (loading || saving || (!options.force && lastRead && Date.now() - lastRead < refreshInterval)) return;
    if (state.activePage !== "home" || document.visibilityState === "hidden") return;
    loading = true;
    const revision = preferenceRevision;
    lastRead = Date.now();
    try {
      const value = await request("get_runtime_alerts");
      if (revision !== preferenceRevision) return;
      applySnapshot(value);
      if ($("#runtimeAlertFeedback").classList.contains("error")) feedback("");
    } catch (error) {
      if (revision !== preferenceRevision) return;
      feedback("运行提醒暂时无法读取，已有提醒仍保留。可刷新重试。", true);
    } finally { loading = false; }
  }

  return { loadRuntimeAlerts, pollRuntimeAlerts: () => loadRuntimeAlerts() };
};
