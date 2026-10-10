/* Features controller; the shell supplies shared services explicitly. */
window.createFeatureController = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    api,
    confirmAction,
    escapeHtml,
    guardProviderDraft,
    icon,
    loadGroups,
    navigate,
    openScheduledFromFeature,
    reportError,
    setSettingsSection,
    showProviderOverview,
    state,
    syncEmbeddedTheme,
    toast,
    getCoreSettings,
  } = helpers;

  async function loadFeatures() {
    const list = $("#featureList");
    list.innerHTML = '<div class="loading-row">正在读取功能…</div>';
    try {
      const data = await api("get_features");
      state.features = data.plugins || [];
      state.featuresEngineOnline = Boolean(data.engine_online);
      state.featuresLoaded = true;
      $("#featureSummary").textContent = `${state.features.length} 项功能`;
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
    getCoreSettings().invalidate();
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
    $("#featureSummary").textContent = `${items.length} / ${state.features.length} 项功能`;
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

  function openImageModelServices() {
    if (state.featureSaving || state.scheduledBusy) return;
    if (state.featureDirty) {
      discardFeatureChanges(openImageModelServices);
      return;
    }
    if (guardProviderDraft(() => {
      showProviderOverview();
      openImageModelServices();
    })) return;
    if (getCoreSettings().guard(openImageModelServices)) return;
    state.imageServiceChoicesStale = true;
    state.settingsSection = "model";
    if (state.providerScreen === "editor") showProviderOverview();
    void navigate("settings").then(() => {
      if (state.activePage === "settings") setSettingsSection("model", true);
    });
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
      state.featureTab = Object.keys(schemas.basic).length ? "basic" : Object.keys(schemas.models).length ? "models" : "advanced";
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
    const cacheNeedsRefresh = Boolean(cached && state.status?.online && !cached.engine_online);
    if (cached) {
      useFeatureDetail(cached, afterSave, preserveTab);
      renderFeatureDetail();
      const note = $("#featureNote");
      if (note && (force || cacheNeedsRefresh)) note.textContent = "正在重新读取最新设置…";
      // Never expose a cached revision for edit when a fresh read is required.
      // Saving and the explicit refresh button pass force=true. An offline
      // cache is also stale once the run center reports that the robot is online.
      if (!force && !cacheNeedsRefresh) return;
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
        useFeatureDetail(cached, afterSave, preserveTab);
        renderFeatureDetail();
        $$("#featureForm input, #featureForm select, #featureForm textarea, #featureForm button").forEach((input) => { input.disabled = true; });
        const save = $("#saveFeature");
        if (save) save.disabled = true;
        const note = $("#featureNote");
        if (note) note.textContent = "最新设置读取失败；当前仅显示上次内容，请重新读取后再修改。";
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
    const encodedPath = escapeHtml(JSON.stringify(path));
    const encodedLabel = escapeHtml(label);
    const fieldId = namespace + "config-" + path.map((part) => encodeURIComponent(part)).join("--");
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
      control = `<select class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-required="${field._required === true}" data-type="${escapeHtml(type)}">${unlisted ? `<option value="${escapeHtml(current)}" selected>${escapeHtml(current || "未设置")}（当前值）</option>` : ""}${field.options.map((option, index) => `<option value="${escapeHtml(option)}" ${current === String(option) ? "selected" : ""}>${escapeHtml(optionNames[index] ?? option)}</option>`).join("")}</select>`;
    } else if (["list", "template_list"].includes(type)) {
      const empty = !value || (Array.isArray(value) && !value.length);
      control = `<textarea class="config-input" data-path="${encodedPath}" data-label="${encodedLabel}" data-type="json" rows="5" placeholder="${escapeHtml(field.placeholder || "可选，留空表示不设置")}">${escapeHtml(empty ? "" : JSON.stringify(value, null, 2))}</textarea>`;
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
    control = control.replace(/<(input|select|textarea) /, '<$1 id="' + fieldId + '" ');
    if (field._report_previews) {
      const preview = field._report_previews[String(value)] || "";
      control += `<figure class="report-style-preview" data-report-preview-for="${fieldId}" data-report-previews="${escapeHtml(JSON.stringify(field._report_previews))}"><button type="button" class="report-preview-trigger" aria-label="查看完整报告样式预览" ${preview ? "" : "hidden"}><img ${preview ? `src="${escapeHtml(preview)}"` : ""} alt="报告样式预览（示例数据）" loading="lazy"></button><figcaption>${preview ? "示例数据 · 点击查看完整长图" : "此自定义样式暂无预览"}</figcaption></figure>`;
    }
    if (field.secret) control += '<button type="button" class="secret-reveal button ghost" aria-controls="' + fieldId + '" aria-pressed="false">显示密钥</button>';
    if (field._group_selector) control += '<button type="button" class="group-field-button button secondary small" data-target="' + fieldId + '">' + icon("i-users") + '从群聊列表添加</button>';
    const dependsOn = field._ui_depends_on ? ` data-depends-on="${escapeHtml(JSON.stringify([...path.slice(0, -1), field._ui_depends_on]))}"` : "";
    const title = type === "bool"
      ? `<span class="field-label">${escapeHtml(label)}</span>`
      : `<label for="${fieldId}">${escapeHtml(label)}</label>`;
    return `<div class="form-field ${field._ui_tint ? "tinted-list" : ""} ${field._ui_disclosure ? "disclosure-controller" : ""} ${field._ui_full || ["text", "list", "template_list"].includes(type) || field.editor_mode ? "full" : ""}"${dependsOn}>${title}${control}<small class="field-error" role="alert" hidden></small></div>`;
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
        ? `<div class="optional-group-heading"><div><h3>${escapeHtml(groupTitle)}</h3></div><button class="button secondary small" type="button" data-optional-group-toggle data-path="${escapeHtml(JSON.stringify(disclosure.path))}" aria-expanded="${disclosure.enabled}">${disclosure.enabled ? "不使用备用模型" : escapeHtml(disclosure.label)}</button></div>`
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

  function featureCommandScheduleHint(command) {
    const text = String(command || "").replace(/^[/\\]/, "").trim();
    if (/(群分析|天气|新闻|资讯|热点|油价|kfc|疯狂星期四|星期四文案|短剧推荐|影视推荐|提醒)/i.test(text)) {
      return {label: "支持定时", status: "supported"};
    }
    if (/(签到|订阅动态|b站动态|epic|喜加一|免费游戏)/i.test(text)) {
      return {label: "需要补充条件", status: "needs-condition"};
    }
    return {label: "不支持定时", status: "unsupported"};
  }

  function featureReferenceMarkup(detail) {
    if (["长期记忆", "语音克隆", "推特推送", "表情管理"].includes(detail.display_name)) return "";
    const groups = Array.isArray(detail.guide?.commands) ? detail.guide.commands : [];
    const commands = groups.map((group) => `<div class="feature-command-group">${groups.length > 1 ? `<strong>${escapeHtml(group.title || "指令")}</strong>` : ""}<div class="feature-command-items">${(Array.isArray(group.items) ? group.items : []).map(([command, description]) => {
      const hint = group.title === "使用方式" ? null : featureCommandScheduleHint(command);
      return `<div class="feature-command-item"><code>${escapeHtml(command || "")}</code><span>${escapeHtml(description || "")}${hint?.status === "supported" ? `<small class="command-schedule-status supported">${hint.label}</small>` : ""}</span></div>`;
    }).join("")}</div></div>`).join("");
    const scheduleButton = detail.scheduled_action === "group_analysis" ? "配置自动日报" : "创建定时任务";
    return `<details class="feature-quick-reference" id="featureQuickReference" data-feature-id="${escapeHtml(detail.id)}" ${state.featureReferenceOpen[detail.id] !== false ? "open" : ""}>
      <summary><span>快速参考</span><span class="reference-toggle-label" aria-hidden="true"><span class="reference-collapse-label">收起</span><span class="reference-expand-label">展开</span></span></summary>
      <div class="feature-reference-body">${commands ? `<div class="feature-command-groups">${commands}</div>` : ""}
      <div class="feature-reference-actions">${detail.scheduled_action ? `<button class="button secondary small" id="featureScheduledTask" type="button" data-action-bound disabled>${scheduleButton}</button>` : ""}
      ${detail.configuration_profile === "image-service-selection" ? '<button class="button secondary small" id="manageImageModels" type="button" data-action-bound disabled>管理图片模型</button>' : ""}
      <button class="button ghost small" id="openFeatureHelp" type="button">帮助</button></div>
      ${detail.has_legacy_image_model ? '<p class="feature-prerequisite">旧图片配置已保留；选择模型服务后迁移。</p>' : ""}</div>
    </details>`;
  }

  function renderFeatureDetail() {
    const detail = state.featureDetail;
    if (!detail) return;
    const previousReference = $("#featureQuickReference");
    if (previousReference) state.featureReferenceOpen[previousReference.dataset.featureId] = previousReference.open;
    $("#featureDetail").dataset.featureId = detail.id || "";
    $("#featureDetail").dataset.featureName = detail.display_name || "";
    const schemas = {basic: detail.basic_schema || {}, models: detail.model_schema || {}, advanced: detail.advanced_schema || detail.schema || {}};
    const currentSchema = schemas[state.featureTab] || schemas.basic;
    const referenceMarkup = featureReferenceMarkup(detail);
    const defaultNote = !detail.engine_online ? "未连接，仅保存本机；下次启动生效，或重连后重新保存。" : "保存并应用可能中断正在执行的任务。";
    const pageOnly = Boolean(detail.pages?.length);
    $("#featureDetail").classList.toggle("native-page", pageOnly);
    $("#featureDetail").classList.toggle("has-reference", Boolean(referenceMarkup));
    $("#featureDetail").innerHTML = `
      <div class="feature-detail-head">
        <h2>${escapeHtml(detail.display_name)}</h2>
      </div>
      ${referenceMarkup}
      ${pageOnly ? `
        <section class="feature-page-panel" aria-label="${escapeHtml(detail.display_name)}专属配置">
          <div class="feature-page-status" id="featurePageStatus" role="status">正在打开专属配置页…</div>
          <iframe id="featurePageFrame" title="${escapeHtml(detail.display_name)}专属配置页" hidden></iframe>
        </section>` : detail.configurable ? `
        <div class="config-tabs" aria-label="设置分类">${[["basic", "常用设置"], ["models", "模型设置"], ["advanced", "专业设置（全部）"]].filter(([key]) => Object.keys(schemas[key]).length).map(([key,label]) => `<button type="button" data-tab="${key}" aria-pressed="${state.featureTab === key}" class="${state.featureTab === key ? "active" : ""}">${label}</button>`).join("")}</div>
        <form id="featureForm" class="feature-form" novalidate>${schemaMarkup(currentSchema, detail.config)}${!Object.keys(currentSchema).length ? '<div class="empty-state compact"><p>此分类无需配置</p></div>' : ""}</form>
        <div class="feature-savebar"><span class="form-note" id="featureNote" role="status">${escapeHtml(state.featureFeedback || (state.featureDirty ? "有未保存的修改。" : defaultNote))}</span><div class="feature-save-actions">
        ${detail.pages?.length ? `<button class="button secondary" id="openFeatureCenter" type="button" ${!detail.engine_online ? 'disabled title="请先启动机器人"' : ""}>打开专属配置页</button>` : ""}
        <button class="button ghost" id="refreshFeature" type="button" data-action-bound disabled>重新读取</button>
        <button class="button primary" id="saveFeature" type="button" data-action-bound disabled>${detail.engine_online ? "保存并应用" : "保存设置"}</button></div></div>` :
        ""}`;
    const reference = $("#featureQuickReference");
    reference?.addEventListener("toggle", () => {
      if (reference.isConnected) state.featureReferenceOpen[detail.id] = reference.open;
    });
    $("#featureScheduledTask")?.addEventListener("click", () => openScheduledFromFeature());
    const manageImageModels = $("#manageImageModels");
    if (manageImageModels) {
      manageImageModels.addEventListener("click", openImageModelServices);
      manageImageModels.disabled = state.featureSaving;
    }
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
    (featureForm ? $$('[data-report-preview-for]', featureForm) : []).forEach((figure) => {
      const select = document.getElementById(figure.dataset.reportPreviewFor);
      const previews = JSON.parse(figure.dataset.reportPreviews);
      select?.addEventListener("change", () => {
        const src = previews[select.value];
        const trigger = figure.querySelector("button"), img = figure.querySelector("img");
        trigger.hidden = !src;
        figure.querySelector("figcaption").textContent = src ? "示例数据 · 点击查看完整长图" : "此自定义样式暂无预览";
        if (src) img.src = src;
        else img.removeAttribute("src");
      });
      figure.querySelector("button").addEventListener("click", () => {
        window.openReportPreview(previews[select.value], select.selectedOptions[0]?.textContent);
      });
      figure.querySelector("img").addEventListener("error", () => {
        figure.querySelector("button").hidden = true;
        figure.querySelector("figcaption").textContent = "样式预览暂时无法加载，请重新读取。";
      });
    });
    featureForm?.addEventListener("input", () => {
      state.featureDirty = true; state.featureFeedback = "";
      $("#featureNote").textContent = "有未保存的修改。";
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
        $("#featureNote").textContent = "有未保存的修改。";
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
    if (pageOnly) void openEmbeddedFeaturePage(detail);
  }

  async function openEmbeddedFeaturePage(detail) {
    const loadId = ++state.featurePageLoad;
    const status = $("#featurePageStatus");
    const frame = $("#featurePageFrame");
    if (!status || !frame || state.selectedFeature !== detail.id) return;
    if (!detail.engine_online && !state.status?.online) {
      status.textContent = "专属配置页需要机器人在线，请先在运行中心启动机器人。";
      return;
    }
    try {
      const page = await api("get_feature_page", detail.id);
      if (loadId !== state.featurePageLoad || state.selectedFeature !== detail.id) return;
      frame.hidden = true;
      status.hidden = false;
      frame.onload = () => syncEmbeddedTheme(frame, () => {
        if (loadId !== state.featurePageLoad || state.selectedFeature !== detail.id) return;
        frame.hidden = false;
        status.hidden = true;
      });
      frame.src = page.url;
      syncEmbeddedTheme(frame);
    } catch (error) {
      if (loadId !== state.featurePageLoad) return;
      status.textContent = error?.message || "专属配置页暂时无法打开，请稍后重试。";
      reportError(error);
    }
  }

  const featurePageChannel = "astrbot-plugin-page";
  async function handleEmbeddedFeaturePageRequest(message) {
    const detail = state.featureDetail;
    if (!detail || detail.configuration !== "page" || !state.selectedFeature) {
      throw new Error("当前功能没有可用的专属配置页。 ");
    }
    if (message.action === "api:get") {
      return api("request_feature_page", state.selectedFeature, "GET", message.endpoint, null, message.params ?? null);
    }
    if (message.action === "api:post") {
      return api("request_feature_page", state.selectedFeature, "POST", message.endpoint, message.body ?? {});
    }
    throw new Error("当前专属配置页使用了尚未支持的操作。 ");
  }

  window.addEventListener("message", async (event) => {
    const frame = $("#featurePageFrame");
    const message = event.data;
    if (!frame || event.source !== frame.contentWindow || event.origin !== window.location.origin || !message
      || message.channel !== featurePageChannel) return;
    if (message.kind === "ready") {
      window.sendFeaturePageContext(frame);
      return;
    }
    if (message.kind !== "request" || typeof message.requestId !== "string") return;
    try {
      const data = await handleEmbeddedFeaturePageRequest(message);
      event.source.postMessage({channel: featurePageChannel, kind: "response", requestId: message.requestId, ok: true, data}, "*");
    } catch (error) {
      event.source.postMessage({channel: featurePageChannel, kind: "response", requestId: message.requestId, ok: false, error: error?.message || "专属配置页请求失败。"}, "*");
    }
  });

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
    const detail = typeof error?.product?.detail === "string" ? error.product.detail.trim() : "";
    if (note) note.textContent = detail || error.message;
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
      if (input.dataset.required === "true" && !String(value ?? "").trim()) {
        throw new Error(`${input.dataset.label || "此配置"}不能为空，请先选择可用模型。`);
      }
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
      const data = await api("save_feature", state.selectedFeature, config, state.featureDetail.revision || "", state.featureBaseline || {});
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
    // Native pages own their drafts and navigation. Rebuilding their iframe on
    // window focus would discard that state, even when our schema form is clean.
    if ($("#featurePageFrame")?.getAttribute("src")) return;
    await openFeature(state.selectedFeature, false, true);
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

  return {
    configFromForm,
    discardFeatureChanges,
    growTextarea,
    loadFeatures,
    openFeature,
    refreshFeatures,
    refreshVisibleFeatureState,
    renderFeatureList,
    renderHelp,
    schemaMarkup,
  };
};
