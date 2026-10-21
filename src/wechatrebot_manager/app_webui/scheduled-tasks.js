/* Scheduled tasks controller; the shell supplies shared services explicitly. */
window.createScheduledTasks = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    api,
    confirmAction,
    debounce,
    discardFeatureChanges,
    escapeHtml,
    icon,
    navigate,
    openFeature,
    reportError,
    shortTime,
    state,
    toast,
  } = helpers;

  async function loadSchedules() {
    const generation = ++state.scheduledLoad;
    const button = $("#refreshSchedules");
    button.disabled = true;
    $$("button", $("#scheduleRows")).forEach((control) => { control.disabled = true; });
    const request = loadScheduledData(generation);
    state.scheduledContextRequest = request;
    try { await request; }
    finally {
      if (generation === state.scheduledLoad) {
        state.scheduledContextRequest = null;
        button.disabled = false;
      }
    }
  }

  function scheduledAccountMatches(account) {
    const runtime = state.status?.runtime;
    return (!runtime || runtime.account_status_available !== false)
      && (state.historyAccount === undefined || state.historyAccount === account);
  }

  function invalidateScheduledContext() {
    state.scheduledLoad += 1;
    state.scheduledContextRequest = null;
    state.scheduledContext = null;
    state.scheduledCommandCatalog = [];
    state.schedules = [];
    state.legacyMigrations = [];
    $("#refreshSchedules").disabled = false;
    $("#scheduleContextMessage").textContent = "微信账号已变化，请重新读取任务列表。";
    $("#scheduleContextMessage").hidden = false;
    renderSchedules();
    renderGroupAnalysisMigration();
    renderScheduledTasks();
    if (state.scheduledEditor && !$("#scheduledTaskEditor").hidden) updateScheduledSummary();
  }

  async function loadScheduledData(generation) {
    try {
      let initialAccount = "";
      try {
        const initial = await api("get_scheduled_task_context", true);
        if (generation !== state.scheduledLoad) return;
        initialAccount = initial.account_id || "";
        state.scheduledContext = scheduledAccountMatches(initialAccount) ? initial : {account_id: "", items: [], loading: true};
        state.schedules = [];
        state.legacyMigrations = [];
        $("#scheduleContextMessage").textContent = initial.message || "正在读取发送对象与运行状态…";
        $("#scheduleContextMessage").hidden = false;
        renderSchedules();
        renderGroupAnalysisMigration();
        renderScheduledTasks();
        if (state.scheduledEditor && !$("#scheduledTaskEditor").hidden) updateScheduledSummary();
      } catch (_error) {
        if (generation !== state.scheduledLoad) return;
        state.scheduledContext = {account_id: "", items: [], loading: true};
        state.schedules = [];
        state.legacyMigrations = [];
        renderSchedules();
        renderGroupAnalysisMigration();
        renderScheduledTasks();
        if (state.scheduledEditor && !$("#scheduledTaskEditor").hidden) updateScheduledSummary();
      }
      const [context, legacy, migrations] = await Promise.allSettled([
        api("get_scheduled_task_context"),
        api("get_schedules"),
        api("get_legacy_schedule_migrations"),
      ]);
      if (generation !== state.scheduledLoad) return;
      let contextData = context.status === "fulfilled" ? context.value : {account_id: "", items: [], message: context.reason?.message || "任务列表暂时无法读取，请刷新重试。"};
      const accountChanged = Boolean(contextData.account_id && ((!scheduledAccountMatches(contextData.account_id))
        || (initialAccount && contextData.account_id !== initialAccount)));
      if (accountChanged) {
        contextData = {account_id: "", items: [], message: "微信账号已变化，请刷新任务列表。"};
      }
      state.scheduledContext = contextData;
      state.scheduledCommandCatalog = Array.isArray(contextData.command_catalog) ? contextData.command_catalog : [];
      const migrationItems = migrations.status === "fulfilled" ? migrations.value.items || [] : [];
      state.legacyMigrations = accountChanged ? [] : migrationItems.filter((item) => !contextData.account_id || item.account_id === contextData.account_id);
      const legacyAccountMatches = Boolean(contextData.account_id) && legacy.status === "fulfilled" && legacy.value.account_id === contextData.account_id;
      state.schedules = legacyAccountMatches && migrations.status === "fulfilled" ? (legacy.value.items || []).filter((item) => !item.action && !state.legacyMigrations.some((migration) => migration.job_id === item.id)) : [];
      const message = $("#scheduleContextMessage");
      message.textContent = contextData.message || (!contextData.account_id ? "请先登录机器人微信账号，再选择发送对象。可以先填写任务草稿。" : "");
      if (context.status === "rejected" && state.legacyMigrations.length) message.textContent += " 可继续完成下方已确认的旧任务迁移。";
      if (legacy.status === "rejected") message.textContent += " 已有功能任务暂时无法读取，请刷新重试。";
      if (legacy.status === "fulfilled" && legacy.value.message) message.textContent += ` ${legacy.value.message}`;
      if (migrations.status === "rejected") message.textContent += " 旧任务迁移记录暂时无法读取，请检查后重试。";
      if ((contextData.account_id && legacy.status === "fulfilled" && legacy.value.account_id && !legacyAccountMatches) || state.legacyMigrations.length !== migrationItems.length) message.textContent += " 微信账号已变化，其他账号的旧任务已隐藏，请刷新重试。";
      message.hidden = !message.textContent;
      renderSchedules();
      renderGroupAnalysisMigration();
      renderScheduledTasks();
      if (state.scheduledEditor && !$("#scheduledTaskEditor").hidden) updateScheduledSummary();
    } catch (error) {
      if (generation !== state.scheduledLoad) return;
      state.scheduledContext = null;
      state.scheduledCommandCatalog = [];
      state.schedules = [];
      state.legacyMigrations = [];
      renderSchedules();
      renderGroupAnalysisMigration();
      renderScheduledTasks();
      $("#scheduleContextMessage").hidden = false;
      $("#scheduleContextMessage").textContent = error.message || "任务暂时无法读取，请刷新重试。";
      if (state.scheduledEditor && !$("#scheduledTaskEditor").hidden) updateScheduledSummary();
      reportError(error);
    }
  }

  function groupAnalysisMigrationAccount(context = {}) {
    const legacy = context.legacy_group_analysis;
    if (!["confirmed", "prepared"].includes(legacy?.state)) return context.account_id || "";
    if (context.account_id && legacy.account_id && context.account_id !== legacy.account_id) return "";
    return legacy.account_id || context.account_id || "";
  }

  function renderGroupAnalysisMigration() {
    const root = $("#groupAnalysisMigration");
    if (!root) return;
    const context = state.scheduledContext || {};
    const legacy = context.legacy_group_analysis;
    root.hidden = !legacy || ["none", "complete"].includes(legacy.state);
    if (root.hidden) { root.replaceChildren(); return; }
    const groups = context.groups || [];
    const resuming = ["confirmed", "prepared"].includes(legacy.state);
    const sourceDays = typeof legacy.days === "number" || typeof legacy.days === "string" ? Number(legacy.days) : legacy.days === undefined ? 1 : NaN;
    const validDays = Number.isInteger(sourceDays) && sourceDays >= 1 && sourceDays <= 7;
    root.innerHTML = `<strong>已有群聊分析日报</strong><p>${escapeHtml(legacy.message || "确认迁移后，旧自动日报将停用，并按每个时间保存为暂停草稿。")}</p>
      ${!resuming && legacy.state === "available" ? `<label class="form-field">迁移后的统计范围<select id="analysisMigrationDays">${!validDays ? '<option value="">请选择 1–7 天</option>' : ""}${[1, 2, 3, 4, 5, 6, 7].map((days) => `<option value="${days}" ${validDays && days === sourceDays ? "selected" : ""}>近 ${days} 天</option>`).join("")}</select>${!validDays ? `<small>旧范围为 ${escapeHtml(legacy.days ?? "未确认")} 天，请为新任务选择 1–7 天。</small>` : ""}</label>` : ""}
      ${legacy.requires_target_selection && !resuming ? `<details open><summary>选择迁移后的日报群</summary><p>旧规则没有明确包含的群，请重新选择需要生成日报的群聊。</p><div class="scheduled-target-choices">${groups.map((group) => `<label class="scheduled-target-option"><input type="checkbox" data-analysis-migration-target value="${escapeHtml(group.id)}"><span>${escapeHtml(group.name || group.id)}</span></label>`).join("")}</div></details>` : ""}
      <button type="button" class="button secondary small" id="previewGroupAnalysisMigration" ${!groupAnalysisMigrationAccount(context) || legacy.state === "blocked" ? "disabled" : ""}>${resuming ? "继续迁移" : "预览迁移"}</button>
      ${legacy.state === "confirmed" ? `<button type="button" class="row-action" id="cancelGroupAnalysisMigration" ${!groupAnalysisMigrationAccount(context) ? "disabled" : ""}>取消迁移</button>` : ""}`;
    $("#previewGroupAnalysisMigration").addEventListener("click", previewGroupAnalysisMigration);
    $("#cancelGroupAnalysisMigration")?.addEventListener("click", cancelGroupAnalysisMigration);
  }

  async function previewGroupAnalysisMigration() {
    if (state.legacyMigrationLoading) return;
    const context = state.scheduledContext || {};
    const legacy = context.legacy_group_analysis;
    const button = $("#previewGroupAnalysisMigration");
    const account = groupAnalysisMigrationAccount(context);
    if (!account || !legacy || legacy.state === "blocked") return;
    const payload = {account_id: account};
    if (!["confirmed", "prepared"].includes(legacy.state)) {
      payload.days = Number($("#analysisMigrationDays")?.value);
      if (!Number.isInteger(payload.days) || payload.days < 1 || payload.days > 7) { toast("请为迁移后的任务选择 1–7 天统计范围。", "error"); return; }
    }
    if (legacy.requires_target_selection && !["confirmed", "prepared"].includes(legacy.state)) {
      payload.targets = $$('[data-analysis-migration-target]:checked').map((input) => ({type: "group", id: input.value}));
      if (!payload.targets.length) { toast("请先选择迁移后的日报群。", "error"); return; }
    }
    state.legacyMigrationLoading = true;
    button.disabled = true;
    try {
      const preview = await api("preview_group_analysis_migration", payload);
      if (groupAnalysisMigrationAccount(state.scheduledContext || {}) !== payload.account_id) throw new Error("微信账号已变化，请刷新后重新预览。");
      if (!preview.token || !Array.isArray(preview.items)) throw new Error("迁移预览不完整，请刷新后重试。");
      const plans = preview.items.map((item) => `${item.name || "群聊分析"}：${item.schedule_text || scheduledTimeText(item.schedule)}，${scheduledTargetText(item.targets)}`).join("；");
      confirmAction({title: "迁移已有群聊分析日报？", message: `${preview.message || "停止机器人后，旧日报将改为暂停草稿。"} ${plans} 确认后仍需逐项检查并启用。`, confirmText: "确认迁移为草稿", pendingText: "正在迁移…", onConfirm: async () => {
        if (groupAnalysisMigrationAccount(state.scheduledContext || {}) !== payload.account_id) throw new Error("微信账号已变化，请重新预览。");
        const result = await api("migrate_group_analysis", preview.token);
        await loadSchedules();
        toast(result.message || (result.pending ? "请关闭机器人微信，停止完成后继续迁移。" : "迁移完成，请检查草稿后启用。"));
      }});
    } catch (error) { reportError(error); }
    finally { state.legacyMigrationLoading = false; if (button.isConnected) button.disabled = !groupAnalysisMigrationAccount(state.scheduledContext || {}); }
  }

  function cancelGroupAnalysisMigration() {
    const context = state.scheduledContext || {};
    const account = groupAnalysisMigrationAccount(context);
    if (state.legacyMigrationLoading || !account || context.legacy_group_analysis?.state !== "confirmed") return;
    confirmAction({title: "取消群聊分析迁移？", message: "旧计划尚未变更。取消后保留原有日报设置，机器人不会自动启动。", confirmText: "取消迁移", onConfirm: async () => {
      if (groupAnalysisMigrationAccount(state.scheduledContext || {}) !== account) throw new Error("微信账号已变化，请刷新后重试。");
      const result = await api("cancel_group_analysis_migration", account);
      await loadSchedules();
      toast(result.message || "已取消迁移，保留原有日报设置。");
    }});
  }

  const SCHEDULED_ACTIONS = {weather: "天气预报", group_analysis: "群聊分析", news: "热点资讯", oilprice: "油价提醒", kfc: "星期四文案", short_drama: "影视推荐", text: "文字提醒"};
  const SCHEDULED_DEFAULTS = {weather: {city: "", day: "today", output: "image"}, group_analysis: {days: 1}, news: {count: 10}, oilprice: {province: ""}, kfc: {}, short_drama: {media_type: "短剧", limit: 12}, text: {text: ""}};
  const SCHEDULED_DAYS = ["一", "二", "三", "四", "五", "六", "日"];
  const SCHEDULED_WEEK_PRESETS = {thursday: [3], weekdays: [0, 1, 2, 3, 4], weekends: [5, 6]};
  const SCHEDULED_RESULTS = {submitted: "已提交微信", failed: "发送失败", unknown: "结果待确认", skipped: "已跳过", pending: "等待发送", running: "正在执行", completed: "执行完成", partial: "部分失败"};

  function scheduledCommandDefinition(action) {
    return state.scheduledCommandCatalog.find((entry) => entry.action === action) || null;
  }

  function scheduledCommandStatusText(definition) {
    if (!definition) return "暂未声明可定时化信息";
    if (definition.status === "supported") return "支持定时";
    if (definition.status === "needs_condition") return "需要补充条件";
    return "不支持定时";
  }

  function scheduledCommandCatalogText() {
    const catalog = state.scheduledCommandCatalog;
    if (!catalog.length) return "命令定时化信息将在读取任务能力后显示。";
    const supported = catalog.filter((entry) => entry.status === "supported").map((entry) => entry.display_name);
    const conditional = catalog.filter((entry) => entry.status === "needs_condition").map((entry) => entry.display_name);
    return [
      supported.length ? `支持定时：${supported.join("、")}` : "",
      conditional.length ? `需要补充条件：${conditional.join("、")}` : "",
      "不支持定时的交互命令会显示原因和替代入口。",
    ].filter(Boolean).join("；");
  }

  function scheduledTaskItems() {
    return state.scheduledContext?.items || [];
  }

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
    return item.action === "weather" ? `${args.city || "城市待填写"}${args.day === "tomorrow" ? "明天" : "当天"}天气` : item.action === "group_analysis" ? "群分析自动日报" : item.action === "oilprice" ? `${args.province || "省份待填写"}油价` : item.action === "short_drama" ? `${args.media_type || "短剧"}推荐` : SCHEDULED_ACTIONS[item.action] || "定时任务";
  }

  function scheduledStatus(item) {
    if (item.draft || item.status === "draft") return "草稿";
    return ({enabled: "已启用", paused: "已暂停", completed: "已完成", running: "正在执行", checking: "正在确认", blocked: "待处理", needs_review: "需要检查", waiting_account: "等待账号登录", waiting_runtime: "等待机器人就绪"})[item.status] || item.status || (item.enabled ? "已启用" : "已暂停");
  }

  function scheduledResultText(value) {
    if (!value) return "尚无执行记录";
    if (typeof value === "string") return SCHEDULED_RESULTS[value] || value;
    return value.message || SCHEDULED_RESULTS[value.status] || "查看执行记录";
  }

  function renderScheduledTasks() {
    const all = scheduledTaskItems();
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
      && (status === "all" || (status === "enabled" && item.enabled) || (status === "draft" && item.draft) || (status === "paused" && !item.enabled && !item.draft && item.status !== "completed") || (status === "attention" && ["blocked", "failed", "needs_review", "waiting_account", "waiting_runtime"].includes(item.status))));
    $("#scheduledTaskHead").hidden = items.length === 0;
    $("#scheduledTaskTable").classList.toggle("is-empty", items.length === 0);
    $("#scheduledTaskRows").innerHTML = items.length ? items.map((item) => `<tr>
      <td><button class="row-action scheduled-name" data-task-action="edit" data-id="${escapeHtml(item.id)}">${escapeHtml(item.name || scheduledName(item))}</button><small>${escapeHtml(SCHEDULED_ACTIONS[item.action] || "定时任务")}</small></td>
      <td><span>${escapeHtml(scheduledTargetText(item.targets))}</span><small class="schedule-target-names">${escapeHtml((item.targets || []).map((entry) => entry.name || "待确认对象").join("、"))}</small></td>
      <td>${escapeHtml(item.schedule_text || scheduledTimeText(item.schedule))}<small>下次：${escapeHtml(scheduledTime(item.next_run_time))}</small></td>
      <td><span class="task-status ${["enabled", "running"].includes(item.status) ? "running" : ["blocked", "needs_review", "waiting_account", "waiting_runtime"].includes(item.status) ? "warning" : ""}">${escapeHtml(scheduledStatus(item))}</span><small>${escapeHtml(item.status_message || scheduledResultText(item.last_result))}</small></td>
      <td><div class="row-actions"><button class="row-action" data-task-action="${item.draft ? "edit" : "toggle"}" data-id="${escapeHtml(item.id)}">${item.draft ? "继续编辑" : item.enabled ? "暂停" : "启用"}</button><button class="row-action" data-task-action="runs" data-id="${escapeHtml(item.id)}">记录</button><button class="row-action" data-task-action="copy" data-id="${escapeHtml(item.id)}">复制</button><button class="row-action delete" data-task-action="delete" data-id="${escapeHtml(item.id)}">删除</button></div></td></tr>`).join("") : `<tr><td colspan="5" class="schedule-empty-cell"><div class="schedule-empty-state"><div class="schedule-empty-icon" aria-hidden="true">${icon("i-clock")}</div><h2>${all.length ? "没有符合条件的任务" : "暂无定时任务"}</h2><p>${all.length ? "调整内容、发送对象或状态筛选，查看其他任务。" : "可创建群聊分析日报、每日天气预报、定时资讯或一句提醒，选择发送对象后自动触发。"}</p><button class="button secondary schedule-empty-cta" id="emptyNewScheduledTask">${all.length ? "+ 新建任务" : "+ 创建第一个定时任务"}</button></div></td></tr>`;
    $$('[data-task-action]', $("#scheduledTaskRows")).forEach((button) => {
      button.disabled = state.scheduledContext?.loading === true;
      button.addEventListener("click", () => scheduledTaskAction(button.dataset.taskAction, button.dataset.id));
    });
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
    $("#scheduleHeading").textContent = "定时任务";
    state.scheduledTimeLoad += 1;
  }

  async function openScheduledEditor(item = null, copy = false, action = "weather") {
    if (state.scheduledBusy || guardScheduledDraft(() => openScheduledEditor(item, copy, action))) return;
    const generation = state.scheduledLoad;
    if (state.scheduledContextRequest) await state.scheduledContextRequest;
    else if (!state.scheduledContext || state.scheduledContext.loading) await loadSchedules();
    if (state.activePage !== "schedules" || !state.scheduledContext || state.scheduledContext.loading
        || (generation !== state.scheduledLoad && state.scheduledContextRequest)
        || (state.scheduledDirty && state.scheduledEditor)
        || !scheduledAccountMatches(state.scheduledContext.account_id || "")) return;
    if (item) {
      item = scheduledTaskItems().find((current) => current.id === item.id && current.account_id === item.account_id);
      if (!item || item.account_id !== state.scheduledContext.account_id) { toast("微信账号或任务已变化，请刷新后重试。", "error"); return; }
    }
    const editor = item ? structuredClone(item) : {id: crypto.randomUUID(), account_id: state.scheduledContext?.account_id || "", action, arguments: structuredClone(SCHEDULED_DEFAULTS[action] || {}), targets: state.scheduledChatFilter ? [structuredClone(state.scheduledChatFilter)] : [], schedule: {kind: "daily", time: action === "group_analysis" ? "23:00" : "08:00", timezone: "Asia/Shanghai"}, enabled: false, draft: true, name: ""};
    if (copy) { editor.id = crypto.randomUUID(); delete editor.revision; editor.name = `${item.name || scheduledName(item)}（副本）`; editor.enabled = false; editor.draft = true; editor.status = "draft"; }
    editor.targets ||= [];
    if (editor.action === "group_analysis") editor.targets = editor.targets.filter((target) => target.type === "group");
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
    if (editor.action === "weather") return field("city", "城市", `<input id="scheduledArg-city" data-task-arg="city" value="${escapeHtml(args.city || "")}" placeholder="例如：青岛" maxlength="80">`) + field("day", "天气日期", `<select id="scheduledArg-day" data-task-arg="day"><option value="today" ${args.day !== "tomorrow" ? "selected" : ""}>执行当天</option><option value="tomorrow" ${args.day === "tomorrow" ? "selected" : ""}>执行日的明天</option></select>`);
    if (editor.action === "group_analysis") return `<div class="scheduled-analysis-intro"><strong>群聊分析报告</strong><div class="form-field"><label for="scheduledAnalysisDays">统计范围</label><select id="scheduledAnalysisDays">${[1, 2, 3, 4, 5, 6, 7].map((days) => `<option value="${days}" ${days === Number(editor.arguments.days || 1) ? "selected" : ""}>近 ${days} 天</option>`).join("")}</select></div><p class="subtle">内容预览会使用模型并保存分析记录，不会发送消息。</p></div>`;
    if (editor.action === "news") return field("count", "资讯条数", `<input id="scheduledArg-count" data-task-arg="count" type="number" min="1" max="30" value="${escapeHtml(args.count ?? 10)}">`);
    if (editor.action === "oilprice") return field("province", "省份", `<input id="scheduledArg-province" data-task-arg="province" value="${escapeHtml(args.province || "")}" maxlength="80" placeholder="例如：山东">`);
    if (editor.action === "short_drama") return field("media_type", "推荐分类", `<select id="scheduledArg-media_type" data-task-arg="media_type">${["短剧", "电视剧", "电影", "动漫", "综艺", "体育", "全部"].map((value) => `<option value="${value}" ${args.media_type === value ? "selected" : ""}>${value === "体育" ? "体育回放" : value === "全部" ? "全部分类" : value}</option>`).join("")}</select>`) + field("limit", "每次推荐", `<input id="scheduledArg-limit" data-task-arg="limit" type="number" min="1" max="20" value="${escapeHtml(args.limit ?? 12)}">`);
    if (editor.action === "text") return field("text", "提醒文字", `<textarea id="scheduledArg-text" data-task-arg="text" rows="4" maxlength="4000" placeholder="填写希望按时发送的文字">${escapeHtml(args.text || "")}</textarea>`);
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
    $("#scheduleHeading").textContent = "安排定时消息";
    const analysisTask = editor.action === "group_analysis";
    const targetIntro = analysisTask ? "选择需要生成日报的群聊。" : "固定选择当前账号的聊天，可同时选择群聊和私聊。";
    const targetTabs = analysisTask ? `<div class="inspector-tabs" aria-label="发送对象类型"><button type="button" data-scheduled-target-tab="group" aria-pressed="true" class="active">群聊</button></div>` : `<div class="inspector-tabs" aria-label="发送对象类型"><button type="button" data-scheduled-target-tab="group" aria-pressed="true" class="active">群聊</button><button type="button" data-scheduled-target-tab="private" aria-pressed="false">私聊</button></div>`;
    root.innerHTML = `<div class="schedule-editor-heading"><button class="button ghost small" id="scheduledBack" type="button">返回任务列表</button><h2 id="scheduledEditorHeading" tabindex="-1">${editor.revision ? "编辑任务" : "新建任务"}</h2></div>
      <div class="schedule-editor-layout"><form id="scheduledTaskForm" novalidate>
        <section class="panel scheduled-form-section"><h3><span>1</span>做什么</h3><div class="form-field"><label for="scheduledName">任务名称</label><input id="scheduledName" value="${escapeHtml(editor.name || "")}" placeholder="自动使用内容名称，也可以自己命名" maxlength="80"></div>
        <div class="form-field"><label for="scheduledAction">发送内容</label><select id="scheduledAction">${Object.entries(SCHEDULED_ACTIONS).map(([value, name]) => `<option value="${value}" ${value === editor.action ? "selected" : ""}>${name}</option>`).join("")}</select></div><div id="scheduledArguments" class="scheduled-arguments">${scheduledArgumentsMarkup(editor)}</div>
        <div id="scheduledReadiness" class="schedule-notice"></div><button class="row-action" id="scheduledPrepareFeature" type="button" hidden>去准备功能，保留当前草稿</button>
        ${analysisTask ? "" : `<details class="schedule-command"><summary>从已有命令填写</summary><label for="scheduledCommand">命令</label><div><input id="scheduledCommand" placeholder="例如：/青岛天气"><button class="button secondary small" id="scheduledParseCommand" type="button">识别并填写</button></div><small>只识别并填写，不发送消息。</small><p class="schedule-command-catalog">${escapeHtml(scheduledCommandCatalogText())}</p></details>`}</section>
        <section class="panel scheduled-form-section"><h3><span>2</span>发给谁</h3><p class="subtle">${targetIntro}</p><div class="scheduled-selected" id="scheduledSelected"></div>${targetTabs}<div class="form-field"><label for="scheduledTargetSearch">搜索聊天</label><label class="search-box wide" id="scheduledTargetSearchBox"><svg><use href="#i-search"/></svg><input id="scheduledTargetSearch" type="search" placeholder="搜索群名、昵称或备注" autocomplete="off"><button type="button" class="clear-search" aria-label="清空聊天搜索">×</button></label></div><div id="scheduledTargetChoices" class="scheduled-target-choices"></div></section>
        <section class="panel scheduled-form-section"><h3><span>3</span>什么时候</h3>${scheduledTimingMarkup(editor.schedule)}</section>
      </form><aside class="schedule-summary panel"><span class="eyebrow">确认你的安排</span><h3 id="scheduledSummaryName"></h3><p id="scheduledSummaryText" aria-live="polite"></p><div class="schedule-next-times"><strong>未来执行时间 · 北京时间</strong><ol id="scheduledNextTimes"><li>正在计算…</li></ol></div><p class="subtle">${analysisTask ? "日报需通过群聊分析权限。" : "发送仍受聊天权限、安静时段和每日上限限制。"}</p><div id="scheduledPreview" class="scheduled-preview" hidden></div><div id="scheduledTaskError" class="modal-error" role="alert" hidden></div><div class="scheduled-editor-actions"><button class="button secondary" id="scheduledPreviewButton" type="button">预览内容</button><button class="button secondary" id="scheduledTestButton" type="button">发送测试消息</button><button class="button ghost" id="scheduledSaveDraft" type="button">保存草稿</button><button class="button primary" id="scheduledSaveEnabled" type="button">${editor.revision ? "保存并启用" : "创建并启用"}</button></div><small>预览只在这里显示；测试发送必须再明确选择一个聊天。</small></aside></div>`;
    $("#scheduledBack").addEventListener("click", () => { if (!guardScheduledDraft(showScheduledList)) { state.scheduledEditor = null; showScheduledList(); } });
    $("#scheduledTaskForm").addEventListener("submit", (event) => event.preventDefault());
    $("#scheduledName").addEventListener("input", (event) => { editor.name = event.target.value; scheduledChanged(false); });
    $("#scheduledAction").addEventListener("change", (event) => {
      const nextAction = event.target.value;
      editor.action = nextAction;
      editor.arguments = structuredClone(SCHEDULED_DEFAULTS[nextAction] || {});
      if (nextAction === "group_analysis") {
        editor.targets = editor.targets.filter((target) => target.type === "group");
        state.scheduledTargetTab = "group";
      }
      state.scheduledDirty = true; renderScheduledEditor(); $("#scheduledAction").focus();
    });
    $$('[data-task-arg]', root).forEach((input) => input.addEventListener("input", () => { editor.arguments[input.dataset.taskArg] = input.type === "number" ? Number(input.value) : input.value; scheduledChanged(false); }));
    $$('[data-scheduled-target-tab]', root).forEach((button) => button.addEventListener("click", () => {
      const changed = state.scheduledTargetTab !== button.dataset.scheduledTargetTab;
      state.scheduledTargetTab = button.dataset.scheduledTargetTab;
      $$('[data-scheduled-target-tab]', root).forEach((tab) => {
        tab.classList.toggle("active", tab === button);
        tab.setAttribute("aria-pressed", String(tab === button));
      });
      renderScheduledTargets();
      if (changed) window.managerMotion?.enter($("#scheduledTargetChoices"));
    }));
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
    if (analysisTask) $("#scheduledAnalysisDays").addEventListener("change", (event) => {
      editor.arguments.days = Number(event.target.value); scheduledChanged(false);
    });
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
    $("#scheduledParseCommand")?.addEventListener("click", parseScheduledCommand);
    $("#scheduledPrepareFeature").addEventListener("click", prepareScheduledFeature);
    $("#scheduledPreviewButton")?.addEventListener("click", previewScheduledContent);
    $("#scheduledTestButton")?.addEventListener("click", testScheduledContent);
    $("#scheduledSaveDraft")?.addEventListener("click", () => saveScheduledTask(true));
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
    const type = editor.action === "group_analysis" ? "group" : state.scheduledTargetTab;
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
    const previewGroup = $("#scheduledPreviewGroup")?.value;
    const editor = state.scheduledEditor;
    const context = state.scheduledContext || {};
    $("#scheduledSelected").innerHTML = `<strong>${escapeHtml(scheduledTargetText(editor.targets))}</strong><div class="schedule-target-chips">${editor.targets.map((entry, index) => { const known = editor.account_id === context.account_id && ((entry.type === "group" ? context.groups : context.contacts) || []).some((item) => item.id === entry.id); return `<button class="schedule-target-chip" type="button" data-remove-target="${index}" aria-label="移除${escapeHtml(entry.name || "对象")}" disabled>${escapeHtml(entry.name || "未命名聊天")}${known ? "" : "（待确认）"} ×</button>`; }).join("")}</div>`;
    if (editor.action === "group_analysis") {
      const value = previewGroup;
      const choices = editor.targets.filter((entry) => entry.type === "group");
      const groupPicker = document.createElement("label");
      groupPicker.className = "form-field";
      groupPicker.innerHTML = `预览哪个群<select id="scheduledPreviewGroup"><option value="">请选择</option>${choices.map((entry) => `<option value="${escapeHtml(entry.id)}">${escapeHtml(entry.name || entry.id)}</option>`).join("")}</select>`;
      $("#scheduledSelected").append(groupPicker);
      $("#scheduledPreviewGroup").value = choices.some((entry) => entry.id === value) ? value : choices.length === 1 ? choices[0].id : "";
    }
    $$('[data-remove-target]').forEach((button) => { button.disabled = false; button.addEventListener("click", () => { editor.targets.splice(Number(button.dataset.removeTarget), 1); renderScheduledTargets(); scheduledChanged(false); }); });
  }

  function scheduledReadiness() {
    const editor = state.scheduledEditor;
    const context = state.scheduledContext || {};
    if (!editor) return {ready: false, message: "任务尚未准备好，请刷新后重试。"};
    const capability = (context.capabilities || []).find((entry) => entry.id === editor.action);
    if (!editor.account_id || context.account_id !== editor.account_id) return {ready: false, message: "尚未确认任务绑定的微信账号。请登录对应账号后再保存；当前填写内容会保留。"};
    if (editor.action === "group_analysis") {
      if (editor.targets.some((target) => target.type !== "group")) return {ready: false, message: "群聊分析只支持群聊。"};
      const legacy = context.legacy_group_analysis;
      if (legacy && ["available", "confirmed", "prepared", "blocked"].includes(legacy.state)) return {ready: false, message: "请先在任务列表迁移旧日报；当前内容可保存为草稿。"};
    }
    if (!capability || !capability.ready) return {ready: false, message: capability?.message || "所需功能尚未就绪，可以先保存草稿。", feature: true};
    if (!context.runtime_ready) return {ready: false, message: "定时发送能力尚未就绪，可以先保存草稿。"};
    return {ready: true, message: context.online ? "内容能力已就绪。启用后按所选时间执行。" : "可以保存计划，等待机器人上线后执行；错过的时间不会补发。"};
  }

  function updateScheduledSummary() {
    const editor = state.scheduledEditor;
    if (!editor || !$("#scheduledSummaryText")) return;
    const ready = scheduledReadiness();
    const timeText = scheduledTimeText(editor.schedule);
    $("#scheduledSummaryName").textContent = editor.name || scheduledName(editor);
    $("#scheduledSummaryText").textContent = `${timeText}，发送${scheduledName(editor)}${editor.action === "weather" ? "图片卡片" : editor.action === "short_drama" ? "播放卡片" : ""}，给${scheduledTargetText(editor.targets)}。`;
    $("#scheduledReadiness").textContent = ready.message;
    $("#scheduledReadiness").classList.toggle("warning", !ready.ready);
    $("#scheduledPrepareFeature").hidden = !ready.feature;
    $("#scheduledSaveEnabled").disabled = !ready.ready || state.scheduledBusy;
    const saveDraft = $("#scheduledSaveDraft");
    const previewButton = $("#scheduledPreviewButton");
    const testButton = $("#scheduledTestButton");
    if (saveDraft) saveDraft.disabled = !editor.account_id || editor.account_id !== state.scheduledContext?.account_id || state.scheduledBusy;
    if (previewButton) previewButton.disabled = state.scheduledBusy;
    if (testButton) testButton.disabled = !ready.ready || !editor.targets.length || state.scheduledBusy;
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
    payload.name = String(payload.name || "").trim() || scheduledName(payload);
    payload.draft = draft;
    payload.enabled = !draft;
    payload.schedule.timezone = "Asia/Shanghai";
    if (payload.action === "weather") payload.arguments.output = "image";
    payload.definition_version = Number(state.scheduledContext?.command_definition_version || 1);
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
      const payload = scheduledPayload(true);
      if (payload.action === "group_analysis") {
        const target = payload.targets.find((item) => item.id === $("#scheduledPreviewGroup")?.value) || (payload.targets.length === 1 ? payload.targets[0] : null);
        if (!target) throw new Error("请先选择要预览的群聊。");
        payload.preview_target = {type: "group", id: target.id};
      }
      const data = await api("preview_scheduled_task", payload);
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
      if (parsed.status !== "supported" || !SCHEDULED_ACTIONS[parsed.action]) {
        const reason = parsed.reason || "这个命令暂不支持定时发送。";
        throw new Error(`${reason}${parsed.alternative ? ` ${parsed.alternative}` : ""}`);
      }
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
    const scheduledAction = action === "group_analysis" ? "group_analysis" : action === "short_drama" ? "short_drama" : "weather";
    const feature = state.features.find((item) => item.schedule_provider && item.scheduled_action === scheduledAction);
    if (feature) await openFeature(feature.id);
    toast("当前任务草稿已保留，完成准备后回到定时任务继续。 ");
  }

  async function openScheduledFromFeature() {
    if (state.featureDirty) { discardFeatureChanges(openScheduledFromFeature); return; }
    const action = state.featureDetail?.scheduled_action || "weather";
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
    const item = scheduledTaskItems().find((entry) => entry.id === id);
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
    $("#scheduleHeading").textContent = "执行记录";
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
    $("#legacyScheduleSection").hidden = !state.schedules.length && !state.legacyMigrations.length;
    if (state.legacyMigrations.length) $("#legacyScheduleSection").open = true;
    $("#scheduleRows").innerHTML = state.schedules.length ? state.schedules.map((item) => `
      <tr><td><strong>${escapeHtml(item.name)}</strong></td><td>${escapeHtml(item.source)}</td><td>${escapeHtml(item.target)}</td><td>${escapeHtml(item.schedule_text)}</td><td>${escapeHtml(shortTime(item.next_run_time))}</td><td><span class="task-status ${item.enabled ? "running" : ""}">${item.status}</span></td><td><div class="row-actions"><button class="row-action" data-action="toggle" data-id="${escapeHtml(item.id)}">${item.enabled ? "暂停" : "开启"}</button><button class="row-action" data-action="edit" data-id="${escapeHtml(item.id)}">改时间</button><button class="row-action delete" data-action="delete" data-id="${escapeHtml(item.id)}">删除</button></div></td></tr>`).join("") : '<tr><td colspan="7" class="table-empty">当前没有业务定时任务</td></tr>';
    $$("tr", $("#scheduleRows")).forEach((row, index) => {
      const item = state.schedules[index];
      if (item && ["weather", "news", "oilprice", "kfc", "short_drama"].includes(item.source_kind)) {
        const button = document.createElement("button");
        button.className = "row-action"; button.textContent = "迁入任务中心";
        button.addEventListener("click", () => beginLegacyMigration(item.id, "", button));
        row.querySelector(".row-actions").prepend(button);
      }
    });
    if (state.legacyMigrations.length) {
      if (!state.schedules.length) $("#scheduleRows").innerHTML = "";
      $("#scheduleRows").insertAdjacentHTML("beforeend", state.legacyMigrations.map((item) => `<tr><td><strong>${escapeHtml(item.name)}</strong></td><td>旧任务迁移</td><td>${escapeHtml(item.targets.map((target) => target.id).join("、"))}</td><td>${escapeHtml(item.schedule_text)}</td><td>迁入后暂停</td><td>待完成迁移</td><td><div class="row-actions"><button class="row-action" data-migration="${escapeHtml(item.key)}">继续迁移</button>${item.can_cancel ? `<button class="row-action" data-cancel-migration="${escapeHtml(item.key)}">取消迁移</button>` : ""}</div></td></tr>`).join(""));
    }
    $$("[data-action]", $("#scheduleRows")).forEach((button) => button.addEventListener("click", () => scheduleAction(button.dataset.action, button.dataset.id)));
    $$("[data-migration]", $("#scheduleRows")).forEach((button) => button.addEventListener("click", () => beginLegacyMigration("", button.dataset.migration, button)));
    $$("[data-cancel-migration]", $("#scheduleRows")).forEach((button) => button.addEventListener("click", async () => {
      try { const result = await api("cancel_legacy_schedule_migration", button.dataset.cancelMigration); await loadSchedules(); toast(result.message); }
      catch (error) { reportError(error); }
    }));
  }

  async function beginLegacyMigration(id, key = "", button = null) {
    if (state.legacyMigrationLoading || !$("#modalBackdrop").hidden) return;
    state.legacyMigrationLoading = true;
    const generation = state.scheduledLoad;
    if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); }
    try {
      const plan = await api("preview_legacy_schedule_migration", id, key);
      if (state.activePage !== "schedules" || generation !== state.scheduledLoad) return;
      const directory = state.scheduledContext?.account_id === plan.account_id ? [...(state.scheduledContext.groups || []), ...(state.scheduledContext.contacts || [])] : [];
      const targets = plan.targets.map((target) => {
        const entry = directory.find((item) => item.type === target.type && item.id === target.id);
        return entry ? `${entry.name}（${target.id}）` : target.id;
      }).join("、");
      const count = plan.arguments.count || plan.arguments.limit;
      if (button?.isConnected) { button.disabled = false; button.focus(); }
      confirmAction({title: "迁入任务中心", intent: "primary", confirmText: key ? "继续迁移" : "停止并迁移", pendingText: "正在处理…",
        message: `账号：${plan.account_id}。${scheduledName(plan)}${count ? `，每次 ${count} 条` : ""}；${plan.schedule_text}；发送到 ${targets}。原状态：${plan.previously_enabled ? "已启用" : "已暂停"}。${plan.message}`,
        onConfirm: async () => {
          const result = await api("migrate_legacy_schedule", plan.token);
          await loadSchedules();
          toast(result.pending ? result.message : `${result.message}重新登录原账号后，可在任务列表核对草稿。`);
        },
      });
    } catch (error) { reportError(error); }
    finally {
      state.legacyMigrationLoading = false;
      if (button?.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); }
    }
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

  return {
    SCHEDULED_ACTIONS,
    guardScheduledDraft,
    invalidateScheduledContext,
    loadSchedules,
    openScheduledEditor,
    openScheduledForChat,
    openScheduledFromFeature,
    renderScheduledEditor,
    renderScheduledTasks,
    scheduledTimeText,
  };
};
