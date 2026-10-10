/* Settings controller; the shell supplies shared services explicitly. */
window.createManagerSettings = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    SETTINGS_SECTIONS,
    api,
    applyBrand,
    confirmAction,
    escapeHtml,
    fullTime,
    guardProviderDraft,
    loadFeatures,
    navigate,
    openFeature,
    renderProviderServices,
    reportError,
    safeImageUrl,
    showProviderOverview,
    state,
    toast,
    updateToggleCopy,
    getCoreSettings,
  } = helpers;

  function selectSettingsSection(section) {
    if (section !== state.settingsSection && getCoreSettings().guard(() => selectSettingsSection(section))) return;
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
      item.hidden = !matches;
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
    if (section === "core") void getCoreSettings().load();
    if (section === "search" && !state.webSearchDirty) void loadWebSearchSettings();
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
    $("#webSearchDetails").hidden = !state.webSearch.enabled;
    $("#clearWebSearchKey").hidden = !state.webSearch.enabled;
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
    $("#webSearchKeyHint").textContent = item.configured
      ? `当前服务商已保存 ${item.key_count || 1} 个密钥；输入新密钥会替换当前服务商密钥。`
      : "当前服务商尚未配置密钥；密钥仅保存在本机。";
    const website = safeImageUrl(item.website_url) || (String(item.website_url || "").startsWith("https://") ? item.website_url : "");
    $("#webSearchProviderSummary").innerHTML = `<div class="web-search-provider-summary-copy"><span class="eyebrow">当前服务商</span><strong>${escapeHtml(item.name || "未选择")}</strong><small>${website ? ` <a href="${escapeHtml(website)}" target="_blank" rel="noopener noreferrer">注册 / 获取密钥</a>` : ""}</small></div><span class="status-badge ${item.configured ? "online" : ""}">${item.configured ? `已配置 ${item.key_count || 1} 个密钥` : "尚未配置"}</span>`;
  }

  async function loadWebSearchSettings() {
    try {
      const settings = await api("get_web_search_settings");
      if (!state.webSearchDirty) renderWebSearch(settings);
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

  async function openMemoryManager() {
    if (getCoreSettings().guard(openMemoryManager)) return;
    try {
      if (!state.featuresLoaded) await loadFeatures();
      const memory = state.features.find((item) => item.display_name === "长期记忆");
      if (!memory) throw new Error("未找到长期记忆插件。");
      await navigate("features");
      if (state.activePage === "features") await openFeature(memory.id);
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

  async function showOnboarding(force = false) {
    if (!force && state.bootstrap?.experience?.onboarding_complete) return;
    try {
      state.bootstrap.readiness = await api("get_onboarding_readiness");
    } catch (error) {
      // The bootstrap snapshot remains useful when a transient bridge probe
      // fails; show it and let the user retry by reopening the checklist.
      reportError(error);
    }
    const backdrop = $("#onboardingBackdrop");
    renderOnboardingReadiness(state.bootstrap?.readiness);
    backdrop.hidden = false;
    $(".app-shell").inert = true;
    $("#onboardingRecommended").focus();
  }

  function renderOnboardingReadiness(readiness) {
    state.readiness = readiness || null;
    const list = $("#onboardingChecklist");
    if (!list) return;
    const labels = {ready: "可直接使用", needs_config: "还需配置", offline: "外部服务离线", group_disabled: "当前群未启用", risk: "存在风险"};
    const items = Array.isArray(readiness?.items) ? readiness.items : [];
    list.innerHTML = items.length ? items.map((item) => `
      <li class="readiness-${escapeHtml(item.status || "needs_config")}">
        <span class="readiness-dot" aria-hidden="true"></span>
        <div><strong>${escapeHtml(item.label || "检查项")}</strong><small>${escapeHtml(labels[item.status] || "还需配置")} · ${escapeHtml(item.detail || "")}</small></div>
        <em>${escapeHtml(item.next_step || "")}</em>
      </li>`).join("") : "<li>准备状态暂时无法读取，请稍后重试。</li>";
    const hint = $(".onboarding-note");
    if (hint && readiness?.test_hint) hint.textContent = readiness.test_hint + " " + (readiness.message || "");
  }

  function fillSettings(data) {
    applyBrand(data.brand);
    state.experience = data.experience || {};
    renderOnboardingReadiness(data.readiness);
    const presetSelect = $("#providerPreset");
    if (presetSelect && Array.isArray(data.provider_presets)) {
      presetSelect.innerHTML = data.provider_presets.map((preset) => {
        return `<option value="${escapeHtml(preset.id)}">${escapeHtml(preset.name)}</option>`;
      }).join("");
    }
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

  return {
    chooseRestoreBackup,
    closeOnboarding,
    createBackup,
    exportDiagnostics,
    fillSettings,
    formatBytes,
    loadMaintenance,
    openMemoryManager,
    saveWebSearchSettings,
    selectSettingsSection,
    setSettingsSection,
    showOnboarding,
    updateWebSearchProviderCopy,
  };
};
