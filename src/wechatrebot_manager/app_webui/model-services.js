/* Model services controller; the shell supplies shared services explicitly. */
window.createModelServices = function (helpers) {
  "use strict";
  const {
    $,
    $$,
    api,
    confirmAction,
    escapeHtml,
    icon,
    renderHomeSetupAlerts,
    reportError,
    requiredValues,
    state,
    toast,
    getCoreSettings,
  } = helpers;

  const providerIconFiles = {
    deepseek: "deepseek.ico",
    glm: "glm.png",
    xiaomi_mimo: "xiaomi_mimo.ico",
    qwen: "qwen.png",
    volcengine: "volcengine.png",
    hunyuan: "hunyuan.png",
    gemini_proxy: "gemini_proxy.png",
  };

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
    syncProviderIcon();
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
      message: "Base URL、模型名称或 API Key / Token Plan 还没有保存。返回后这些修改会被清空。",
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
      if (values.service_kind === "chat") {
        values.thinking_enabled = form.elements.thinking_enabled.checked;
      } else {
        delete values.thinking_enabled;
      }
      const preset = (state.bootstrap?.provider_presets || []).find(
        (item) => item.id === form.elements.preset.value,
      );
      if (values.service_kind === "chat") {
        values.modalities = "text,image,audio";
      } else if (Array.isArray(preset?.modalities)) {
        values.modalities = preset.modalities.join(",");
      }
      const data = await api("save_provider", values);
      state.bootstrap.providers = await api("get_providers");
      state.bootstrap.provider = state.bootstrap.providers.find((item) => item.is_default) || data;
      state.featureDetails = {};
      getCoreSettings().invalidate();
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
    if (!service) return "deepseek";
    if (service.service_kind === "embedding") return "embedding_custom";
    if (service.service_kind === "image") return "image_custom";
    if (service.service_kind === "voice") return "voice_custom";
    // Model names are also used by relays; only an official endpoint gets a brand.
    const host = providerEndpointHost(service.api_base);
    return (state.bootstrap?.provider_presets || []).find((preset) =>
      host && providerIconFiles[preset.id] && providerEndpointHost(preset.api_base) === host,
    )?.id || "custom";
  }

  function providerEndpointHost(apiBase) {
    try {
      const endpoint = new URL(String(apiBase || ""));
      return endpoint.protocol === "https:" && !endpoint.username && !endpoint.password
        ? endpoint.host.toLowerCase() : "";
    } catch (_) { return ""; }
  }

  function providerIconMarkup(presetId) {
    const file = providerIconFiles[presetId];
    return file
      ? `<img src="assets/provider-${file}" alt="" width="24" height="24">`
      : icon("i-grid");
  }

  function syncProviderIcon() {
    const form = $("#providerForm");
    const target = $("#providerPresetIcon");
    if (!form || !target) return;
    const selected = form.elements.preset.value;
    const actual = providerPresetFor({
      api_base: form.elements.api_base.value,
      service_kind: form.elements.service_kind.value,
    });
    const preset = selected === actual ? selected : "custom";
    if (target.dataset.preset === preset) return;
    target.dataset.preset = preset;
    target.innerHTML = providerIconMarkup(preset);
  }

  function providerCardIdentity(service) {
    const preset = providerPresetFor(service);
    const definition = (state.bootstrap?.provider_presets || []).find((item) => item.id === preset);
    const label = {embedding: "文本向量", image: "图片模型", voice: "语音模型"}[service.service_kind]
      || (providerIconFiles[preset] ? definition?.name : "兼容服务");
    return {mark: providerIconMarkup(preset), label, className: "provider-brand-logo"};
  }

  function providerChatModalityValue(modalities) {
    const source = Array.isArray(modalities) ? modalities : String(modalities || "").split(",");
    const values = new Set(source.map((item) => String(item).trim().toLowerCase()));
    return ["text", "image", "audio"].filter((item) => values.has(item)).join(",");
  }

  function providerCapabilityLabel(serviceKind) {
    return {embedding: "向量", image: "图片", voice: "语音", rerank: "重排序"}[serviceKind] || "对话";
  }

  function inferProviderModalities(serviceKind, model, declared) {
    if (serviceKind === "embedding") return ["embedding"];
    if (serviceKind === "image") return ["image"];
    if (serviceKind === "voice") return ["audio"];
    const name = String(model || "").trim().toLowerCase();
    const declaredValue = providerChatModalityValue(declared);
    if (declaredValue) return declaredValue.split(",");
    if (name === "grok-4.6") return ["text", "image", "audio"];
    const leaf = name.split("/").pop() || "";
    if (["-v", "vision", "vl", "gemini", "doubao-seed"].some((marker) => leaf.includes(marker))) {
      return leaf.includes("gemini") || leaf.includes("doubao")
        ? ["text", "image", "audio"]
        : ["text", "image"];
    }
    return ["text", "image", "audio"];
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
        || (on("openrouter.ai") && (qwen || deepseek || glm || kimi || openai || /^gemini-(?:2\.5|3\.8)-flash(?:-lite)?/.test(leaf) || claude))
        || (on("together.xyz", "together.ai") && /^(qwen3\.5|qwen-3\.5|deepseek-v4)/.test(leaf))
        || (on("fireworks.ai") && (qwen || deepseek || glm))
        || (on("groq.com") && /^(qwen3\.6-27b|qwen3\.8-27b)/.test(leaf))
        || (on("cerebras.ai") && leaf === "zai-glm-4.7")
        || (ollama && !leaf.includes("gpt-oss") && /^(qwen3|qwen-3|deepseek-v3\.1|deepseek-r1|glm-4\.7)/.test(leaf))
        || (on("x.ai", "xiuxianjyj.xin") && /^(grok-4\.5|grok-4\.6)/.test(leaf))
        || (on("googleapis.com") && /^gemini-(?:2\.5|3\.8)-flash(?:-lite)?/.test(leaf))
        || (on("openai.com", "openai.azure.com", "services.ai.azure.com", "models.inference.ai.azure.com") && openai);
    } catch (_) { return false; }
  }

  function syncProviderThinkingControl(initialValue = null) {
    const form = $("#providerForm");
    const input = form.elements.thinking_enabled;
    if (initialValue !== null) input.checked = Boolean(initialValue);
    input.disabled = false;
    $("#providerThinkingValue").textContent = input.checked ? "已开启" : "已关闭";
  }

  function applyProviderPreset(presetId) {
    const form = $("#providerForm");
    const preset = (state.bootstrap?.provider_presets || []).find((item) => item.id === presetId);
    if (!preset) return;
    form.elements.api_base.value = preset.api_base || "";
    form.elements.model.value = preset.model || "";
    form.elements.service_kind.value = preset.service_kind || "chat";
    form.elements.service_kind.disabled = false;
    syncProviderIcon();
    $("#providerThinkingRow").hidden = (preset.service_kind || "chat") !== "chat";
    clearProviderModels();
    $("#providerModelHint").textContent = "";
    syncProviderThinkingControl();
    renderProviderSaveIntent(
      providerWillBeDefault(preset.service_kind || "chat", form.elements.provider_id.value),
      preset.service_kind || "chat",
    );
  }

  function isFactoryProvider(service) {
    return Boolean(
      service?.is_recommended
      && String(service.model || "").toLowerCase() === "grok-4.6"
    );
  }

  function visibleProviderServices() {
    return (state.bootstrap?.providers || []).filter((item) => !isFactoryProvider(item));
  }

  function providerWillBeDefault(serviceKind, providerId = "") {
    if (serviceKind !== "chat") return false;
    const services = visibleProviderServices();
    const selected = services.find((item) => item.id === providerId);
    return !services.some((item) => (item.service_kind || "chat") === "chat")
      || Boolean(selected?.is_default);
  }

  function renderProviderSaveIntent(makeDefault, serviceKind = "chat") {
    if (serviceKind !== "chat") {
      const label = serviceKind === "image" ? "图片模型" : serviceKind === "voice" ? "语音模型" : "向量模型";
      $("#saveProvider").textContent = `保存${label}`;
      return;
    }
    $("#saveProvider").textContent = makeDefault ? "保存并设为回复模型" : "保存模型服务";
  }

  function beginNewProvider(presetId = "deepseek") {
    const form = $("#providerForm");
    form.reset();
    form.elements.provider_id.value = "";
    form.elements.preset.value = presetId;
    applyProviderPreset(presetId);
    $("#providerEditorTitle").textContent = "新建模型服务";
    $("#apiKeyHint").textContent = "API Key / Token Plan 只保存在本机，不会回显";
    $("#testProvider").hidden = true;
    $("#deleteProvider").hidden = true;
    $("#providerNote").textContent = "";
    renderProviderSaveIntent(
      providerWillBeDefault(form.elements.service_kind.value, ""),
      form.elements.service_kind.value,
    );
    markProviderClean("尚未保存");
    showProviderEditor();
  }

  function editProvider(providerId) {
    const service = (state.bootstrap?.providers || []).find((item) => item.id === providerId);
    if (!service) { beginNewProvider(); return; }
    const form = $("#providerForm");
    form.elements.provider_id.value = service.id;
    form.elements.preset.value = providerPresetFor(service);
    form.elements.service_kind.value = service.service_kind || "chat";
    $("#providerThinkingRow").hidden = (service.service_kind || "chat") !== "chat";
    form.elements.api_base.value = service.api_base || "";
    form.elements.model.value = service.model || "";
    form.elements.api_key.value = "";
    syncProviderIcon();
    syncProviderThinkingControl(service.thinking_enabled !== false);
    clearProviderModels();
    $("#providerModelHint").textContent = "";
    $("#providerEditorTitle").textContent = service.name;
    $("#apiKeyHint").textContent = service.api_key_configured ? "已安全保存；留空不会覆盖" : "尚未配置 API Key / Token Plan";
    $("#testProvider").hidden = !service.configured;
    $("#deleteProvider").hidden = false;
    $("#providerNote").textContent = "";
    renderProviderSaveIntent(Boolean(service.is_default), service.service_kind || "chat");
    markProviderClean("已保存");
    showProviderEditor();
  }

  function renderProviderServices() {
    const items = visibleProviderServices();
    const displayItems = [...items].sort((left, right) => Number(right.is_default) - Number(left.is_default));
    const status = $("#providerStatus");
    const defaultProvider = items.find((item) => item.is_default);
    status.className = `status-badge ${defaultProvider ? "success" : "checking"}`;
    status.textContent = `${items.length} 项服务`;
    $("#providerServiceList").innerHTML = items.length ? `
      <div class="provider-list-toolbar"><div><strong>${escapeHtml(defaultProvider ? `${defaultProvider.name} 正在回复` : "请选择回复模型")}</strong><span>机器人和相关功能共用这些配置，API Key / Token Plan 仅保存在当前设备。</span></div></div>
      <div class="provider-service-cards">${displayItems.map((item) => {
        const identity = providerCardIdentity(item);
        const functionalService = item.service_kind !== "chat";
        const statusText = item.is_default
          ? "使用中"
          : !item.configured
            ? "待完善"
            : functionalService ? "功能可选" : "备用";
        const description = item.is_default
          ? "当前回复模型"
          : !item.configured
            ? "补充 API Key / Token Plan 后即可使用"
            : functionalService ? "供对应功能选择" : "可切换为回复模型";
        const capability = item.service_kind === "image"
          ? {title: "图片能力", detail: "供功能中心图片生成选择"}
          : item.service_kind === "voice"
            ? {title: "语音能力", detail: "供语音功能选择"}
            : {title: "向量能力", detail: "供长期记忆等检索功能选择"};
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
                  ${item.is_default || item.service_kind !== "chat" ? "" : `<button type="button" role="menuitem" data-action-bound disabled data-provider-default="${escapeHtml(item.id)}">设为回复模型</button>`}
                  <button class="danger" type="button" role="menuitem" data-action-bound disabled data-provider-delete="${escapeHtml(item.id)}">删除服务</button>
                </div>
              </details>
            </div>
          </header>
          <div class="provider-service-body">
            <div class="provider-card-tags">
              <span>${escapeHtml(item.modality_label || providerCapabilityLabel(item.service_kind))}</span>
              <span>${item.configured ? "已配置" : "缺少 API Key / Token Plan"}</span>
            </div>
            <p>${escapeHtml(description)}</p>
            ${item.service_kind === "chat" ? `<div class="provider-thinking-control">
              <span><strong>思考模式</strong></span>
              <button class="provider-thinking-switch" type="button" role="switch" aria-label="${escapeHtml(item.name)}思考模式" aria-checked="${Boolean(item.thinking_enabled)}" data-action-bound disabled data-provider-thinking="${escapeHtml(item.id)}"><span aria-hidden="true"></span></button>
            </div>` : `<div class="provider-thinking-control"><span><strong>${capability.title}</strong><small>${capability.detail}</small></span></div>`}
          </div>
          <footer class="provider-service-actions">
            <button class="provider-card-action" type="button" data-action-bound disabled data-provider-edit="${escapeHtml(item.id)}">编辑</button>
            ${item.is_default ? '<span class="provider-card-current">正在用于回复</span>' : item.service_kind === "chat" ? `<button class="provider-card-action primary" type="button" data-action-bound disabled data-provider-default="${escapeHtml(item.id)}">切换使用</button>` : `<span class="provider-card-current">供相关功能选择</span>`}
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

        <h3>还没有模型服务</h3>

        <button class="button primary" type="button" data-action-bound disabled data-provider-create>新建模型服务</button>

      </div>`;
    $$('[data-action-bound]', $("#providerServiceList")).forEach((button) => { button.disabled = false; });
    renderHomeSetupAlerts();
  }

  async function setProviderDefault(providerId, button) {
    const original = button.textContent;
    button.disabled = true;
    button.textContent = "切换中…";
    try {
      const result = await api("set_default_provider", providerId);
      state.bootstrap.providers = await api("get_providers");
      state.featureDetails = {};
      getCoreSettings().invalidate();
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
    const replacementKind = service.is_default ? "chat" : (service.service_kind || "chat");
    const alternatives = (state.bootstrap?.providers || []).filter((item) => item.id !== providerId && item.configured && (item.service_kind || "chat") === replacementKind);
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
        getCoreSettings().invalidate();
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
    button.setAttribute("aria-busy", "true");
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

  return {
    applyProviderPreset,
    beginNewProvider,
    bindProviderModelCombobox,
    deleteProvider,
    deleteSelectedProvider,
    discoverProviderModels,
    editProvider,
    guardProviderDraft,
    inferProviderModalities,
    providerCapabilityLabel,
    providerThinkingSupported,
    renderProviderServices,
    requestProviderOverview,
    saveProvider,
    setProviderDefault,
    setProviderThinking,
    showProviderOverview,
    syncProviderThinkingControl,
    testSelectedProvider,
    updateProviderDirty,
  };
};
